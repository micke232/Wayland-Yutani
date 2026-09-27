"""SQLite WAL journal with atomic intent+audit writes and a single execution owner."""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import OrderCommand, OrderState, TradeProposal, utcnow


def encode(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False)


class AuditStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            self.db.close()
            raise ValueError("unsupported future database schema")
        with self.transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, correlation TEXT, payload TEXT NOT NULL)"
            )
            self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS intents (intent_id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, created TEXT NOT NULL, proposal TEXT NOT NULL, command TEXT NOT NULL, status TEXT NOT NULL, broker_id TEXT)"
            )
            self.db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, kind: str, payload: Any, correlation: str | None = None) -> None:
        # Callers pass validated domain data, never raw environment/config or SDK exceptions.
        self.db.execute(
            "INSERT INTO events(timestamp,kind,correlation,payload) VALUES(?,?,?,?)",
            (utcnow().isoformat(), kind, correlation, encode(payload)),
        )

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encode(value)),
        )

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def prepare(self, proposal: TradeProposal, command: OrderCommand) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO intents VALUES(?,?,?,?,?,?,NULL)",
                (
                    command.intent_id,
                    proposal.proposal_id,
                    utcnow().isoformat(),
                    proposal.model_dump_json(),
                    command.model_dump_json(),
                    OrderState.PREPARED,
                ),
            )
            self.event("intent.prepared", command.model_dump(mode="json"), command.intent_id)

    def transition(self, intent_id: str, status: OrderState, broker_id: str | None = None) -> None:
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE intents SET status=?, broker_id=COALESCE(?,broker_id) WHERE intent_id=?",
                (status, broker_id, intent_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown intent")
            self.event("intent." + status.lower(), {"broker_id": broker_id}, intent_id)

    def intents(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM intents ORDER BY created")]

    def lookup(self, proposal_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM intents WHERE proposal_id=?", (proposal_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, count: int = 20) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute("SELECT * FROM events ORDER BY sequence DESC LIMIT ?", (count,))
        ]

    def close(self) -> None:
        self.db.close()


class ExecutionLease:
    """A second process may inspect status/activate kill, but cannot execute orders."""

    def __init__(self, path: Path):
        self.file = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("another Wayland execution engine owns this state") from None

    def close(self) -> None:
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()

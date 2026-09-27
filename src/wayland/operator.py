"""Independent operator service; analysis chat has no broker/execution tools."""

import asyncio
import json
import signal
import socketserver
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agents import COORDINATOR_ID, REGISTRY
from .audit import AuditStore, ExecutionLease
from .broker.discovery import observation_account
from .config import Settings, load_settings
from .models import utcnow


class OperatorRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.active: dict[str, threading.Event] = {}
        self.delegated: dict[str, threading.Event] = {}
        self.autonomous_roles: set[str] = set()
        from .broker.discovery import discover
        from .broker.observer import BrokerObserver

        self.broker_observer = BrokerObserver(self, discovery=discover)
        from .connections import Connections

        self.connections = Connections(self)
        with self.store() as store:
            if store.get("ui:threads") is None:
                threads = {}
                for role in REGISTRY:
                    threads[role.agent_id] = self.new_thread(role.agent_id, role.name, role.purpose)
                store.set("ui:threads", threads)
            threads = store.get("ui:threads", {})
            for role in REGISTRY:
                if role.agent_id in threads:
                    threads[role.agent_id]["purpose"] = role.purpose
                    if role.agent_id == COORDINATOR_ID and threads[role.agent_id]["name"] == "Strategist":
                        threads[role.agent_id]["name"] = role.name
            for thread in threads.values():
                if thread.get("status", {}).get("type") == "active":
                    thread["status"] = {"type": "waiting"}
                    thread["items"].append(
                        self.message(
                            "agentMessage",
                            "Analysis was interrupted by a service restart. No order was sent. Send a new prompt to continue.",
                        )
                    )
            store.set("ui:threads", threads)

    def store(self):
        from contextlib import contextmanager

        @contextmanager
        def opened():
            store = AuditStore(self.root / "state/wayland.sqlite")
            try:
                yield store
            finally:
                store.close()

        return opened()

    def settings(self):
        path = self.root / "config/settings.json"
        return load_settings(path) if path.exists() else Settings()

    def new_thread(self, tid, name, purpose):
        return {
            "id": tid,
            "name": name,
            "purpose": purpose,
            "provider": "openai",
            "cwd": str(self.root),
            "status": {"type": "idle"},
            "items": [
                self.message(
                    "agentMessage",
                    purpose
                    + ".\n\nThis is Wayland's analytical workspace. Configure the shared Codex model in F10 → Connections. Chat cannot place orders. Broker state is not verified until reconciliation succeeds.",
                )
            ],
            "plan": [],
            "bucket": "threads",
        }

    @staticmethod
    def message(kind, text, **extra):
        return {"id": str(uuid.uuid4()), "type": kind, "text": text, **extra}

    def snapshot(self, store):
        config = self.settings()
        all_threads = store.get("ui:threads", {})
        trading = {key: store.get(key) for key in ("portfolio", "market", "kill_switch", "heartbeat")}
        trading.update(
            operating_state=store.get("operating_state", "RECONCILING"),
            state_reasons=store.get("state_reasons", ["No broker reconciliation has run"]),
            setup=store.get("setup:ORCL"),
            order_intents=store.intents(),
            autonomy=store.get(
                "autonomy", {"status": "OFFLINE", "reason": "Waiting for broker observations"}
            ),
        )
        p = trading.get("portfolio") or {}
        heartbeat = trading.get("heartbeat")
        from datetime import datetime

        fresh = bool(heartbeat and (utcnow() - datetime.fromisoformat(heartbeat)).total_seconds() < 10)
        trading["broker_status"] = (
            "Verified stored snapshot" if p.get("healthy") and p.get("complete") else "Not verified"
        ) + ("" if fresh else " · stale / monitor stopped")
        if not fresh:
            trading["operating_state"] = "RECONCILING"
            trading["state_reasons"] = ["No fresh runtime heartbeat; stored state only"]
        observation = store.get("broker:observation", {})
        if observation:
            observed_at = datetime.fromisoformat(observation["timestamp"])
            current = 0 <= (utcnow() - observed_at).total_seconds() < 30
            observation = dict(observation)
            if not current:
                observation["connected"] = False
                observation["status"] = "Disconnected · stale broker observation"
            trading["brokerObservation"] = observation
            trading["broker_status"] = observation["status"]
            if observation.get("connected"):
                trading["operating_state"] = "RECONCILING"
                trading["state_reasons"] = [
                    "Paper account connected read-only; full execution reconciliation incomplete"
                ]
                trading["market"] = observation.get("market")
        trading["broker_enabled"] = store.get("broker:enabled", False)
        provider = self.connections.snapshot()
        result = {
            "connected": True,
            "tasks": [],
            "models": [],
            "requests": [],
            "trading": trading,
            "settings": store.get("ui:settings", {"mouseEnabled": True}),
            "tradingSettings": config.model_dump(mode="json"),
            "providers": {
                "openai": {**provider, "model": config.openai_model or "Codex default"},
            },
            "setupRevision": store.get("ui:revision", 0),
        }
        events = store.recent(100)
        audit_items = [
            {
                "id": "audit:" + str(e["sequence"]),
                "type": "commandExecution",
                "text": e["timestamp"] + " · " + e["kind"] + "\n" + e["payload"],
            }
            for e in reversed(events)
            if not e["kind"].startswith("ui.")
        ]
        for bucket in ("threads", "archived", "hiddenThreads"):
            result[bucket] = {}
            for tid, thread in all_threads.items():
                if thread.get("bucket") == bucket:
                    shown = dict(thread, model=config.openai_model or "Codex default")
                    shown["items"] = thread["items"] + audit_items
                    result[bucket][tid] = shown
        return result

    def dispatch(self, action, **params):
        with self.lock, self.store() as store:
            if action == "connect_provider":
                return self.connections.connect(params.get("provider"))
            if action in ("snapshot", "diagnostics"):
                return self.snapshot(store)
            threads = store.get("ui:threads", {})
            tid = str(params.get("threadId") or "")
            thread = threads.get(tid)
            if action in ("select", "read_archive"):
                if thread is None:
                    raise ValueError("Unknown Wayland role")
                return thread
            if action == "broker_connection":
                enabled = params.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Connection preference requires boolean")
                if enabled:
                    self.broker_observer.start()
                else:
                    self.broker_observer.shutdown()
                store.set("broker:enabled", enabled)
                return {"enabled": enabled}
            if action == "archives":
                return {"archived": {k: v for k, v in threads.items() if v["bucket"] == "archived"}}
            if action == "create_agent":
                name = str(params.get("name", "")).strip()
                if not name or len(name) > 80:
                    raise ValueError("Name must contain 1–80 characters")
                tid = "wayland:" + str(uuid.uuid4())
                thread = self.new_thread(tid, name, "Additional analytical role")
                threads[tid] = thread
                result = {"threadId": tid, "thread": thread}
            elif action in ("rename", "archive", "remove", "restore"):
                if not thread:
                    raise ValueError("Unknown Wayland role")
                if action == "rename":
                    name = str(params.get("name", "")).strip()
                    if not name or len(name) > 80:
                        raise ValueError("Name must contain 1–80 characters")
                    thread["name"] = name
                else:
                    thread["bucket"] = {
                        "archive": "archived",
                        "remove": "hiddenThreads",
                        "restore": "threads",
                    }[action]
                result = {"threadId": tid, "name": thread["name"]}
            elif action == "send":
                if tid in self.delegated:
                    raise ValueError(
                        "This specialist is reporting to Coordinator. Send additional context to Coordinator."
                    )
                if not thread:
                    raise ValueError("Unknown Wayland role")
                text = str(params.get("text", "")).strip()
                if not text or len(text) > 20000:
                    raise ValueError("Prompt must contain 1–20000 characters")
                thread["items"].append(
                    self.message("userMessage", text, clientId=params.get("clientId"), delivery="delivered")
                )
                if tid in self.active:
                    thread.setdefault("queued", []).append(text)
                    thread["plan"].append({"step": "Incorporate the additional prompt", "status": "pending"})
                    store.set("ui:threads", threads)
                    return {"accepted": True, "queued": True}
                thread["status"] = {"type": "active"}
                thread["startedAt"] = time.time()
                thread["plan"] = [
                    {"step": "Inspect supplied trading context", "status": "completed"},
                    {"step": "Answer through the configured Codex model", "status": "inProgress"},
                ]
                cancel = threading.Event()
                self.active[tid] = cancel
                store.set("ui:threads", threads)
                context = self.snapshot(store)["trading"]
                worker = threading.Thread(target=self.analyze, args=(tid, text, context, cancel), daemon=True)
                worker.start()
                return {"accepted": True}
            elif action == "interrupt":
                if tid in self.delegated:
                    self.delegated[tid].set()
                if tid in self.active:
                    self.active[tid].set()
                    thread["queued"] = []
                    store.set("ui:threads", threads)
                return {"requested": True}
            elif action == "app_settings":
                patch = params.get("patch", {})
                if set(patch) - {"mouseEnabled"} or any(type(v) is not bool for v in patch.values()):
                    raise ValueError("Unsupported interface setting")
                values = store.get("ui:settings", {"mouseEnabled": True})
                values.update(patch)
                store.set("ui:settings", values)
                return values
            elif action == "trading_settings":
                patch = params.get("patch", {})
                from .tui.setup_view import CONNECTION_SECTIONS, SECTIONS

                allowed = {key for keys in (SECTIONS | CONNECTION_SECTIONS).values() for key in keys}
                if set(patch) - allowed:
                    raise ValueError("This setting cannot be changed from the terminal")
                raw = self.settings().model_dump(mode="json")
                for key, value in patch.items():
                    if isinstance(raw[key], list):
                        raw[key] = [s.strip() for s in str(value).split(",") if s.strip()]
                    elif isinstance(raw[key], int):
                        raw[key] = int(value)
                    else:
                        raw[key] = value
                settings = Settings.model_validate_json(json.dumps(raw))
                if any(not observation_account(account) for account in settings.account_allowlist):
                    raise ValueError("Only DU paper accounts are allowed")
                if settings.ibkr_account and not observation_account(settings.ibkr_account):
                    raise ValueError("Only DU paper accounts are allowed")
                path = self.root / "config/settings.json"
                temp = path.with_suffix(".tmp")
                temp.write_text(settings.model_dump_json(indent=2))
                temp.chmod(0o600)
                temp.replace(path)
                store.event("ui.settings.changed", {"fields": sorted(patch)})
                store.set("ui:revision", store.get("ui:revision", 0) + 1)
                return settings.model_dump(mode="json")
            elif action == "kill":
                enabled = params.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Kill switch requires boolean")
                with store.transaction():
                    store.set("kill_switch", enabled)
                    store.event("kill_switch", {"enabled": enabled, "source": "operator"})
                return {"enabled": enabled}
            else:
                raise ValueError("Unsupported Wayland operation")
            store.set("ui:threads", threads)
            return result

    def analyze(self, tid, text, context, cancel):
        config = self.settings()
        reply, failed = "", False
        try:
            if not self.connections.snapshot()["connected"]:
                reply = "Analysis is waiting for configuration. Open F10 and connect Codex CLI. Existing CLI login is reused automatically. No order was sent."
                failed = True
            else:
                if tid == COORDINATOR_ID:
                    from .coordination import coordinate

                    reply = asyncio.run(coordinate(self, text, context, config, cancel))
                else:
                    reply = asyncio.run(self.model_reply(tid, text, context, config, cancel))
        except Exception as error:  # noqa: BLE001 -- SDK payloads may contain credentials
            reply = "Analysis unavailable (" + type(error).__name__ + "). No order was sent. Check Settings."
            failed = True
        with self.lock, self.store() as store:
            threads = store.get("ui:threads", {})
            thread = threads[tid]
            thread["items"].append(
                self.message(
                    "agentMessage", "Analysis interrupted. No order was sent." if cancel.is_set() else reply
                )
            )
            thread["status"] = {"type": "waiting" if failed or cancel.is_set() else "idle"}
            for step in thread["plan"]:
                if step["status"] == "inProgress":
                    step["status"] = "pending" if failed or cancel.is_set() else "completed"
            thread["durationMs"] = int((time.time() - thread.pop("startedAt", time.time())) * 1000)
            self.active.pop(tid, None)
            queued = thread.get("queued", [])
            if queued and not cancel.is_set() and not failed:
                next_text = queued.pop(0)
                next_cancel = threading.Event()
                self.active[tid] = next_cancel
                thread["status"] = {"type": "active"}
                thread["startedAt"] = time.time()
                pending = next((s for s in thread["plan"] if s["status"] == "pending"), None)
                if pending:
                    pending["status"] = "inProgress"
                store.set("ui:threads", threads)
                context = self.snapshot(store)["trading"]
                threading.Thread(
                    target=self.analyze, args=(tid, next_text, context, next_cancel), daemon=True
                ).start()
            else:
                store.set("ui:threads", threads)

    async def model_reply(self, tid, text, context, config, cancel):
        from pydantic import BaseModel, ConfigDict

        from .providers.cli_transport import analyze

        class AnalyticalReply(BaseModel):
            model_config = ConfigDict(extra="forbid")
            answer: str

        # Account IDs, broker IDs and private config are not analytical context.
        supplied = {k: context.get(k) for k in ("market", "setup", "operating_state", "state_reasons")}
        if tid == COORDINATOR_ID and "analyst_reports" in context:
            supplied["analyst_reports"] = context["analyst_reports"]
        with self.store() as store:
            thread = store.get("ui:threads")[tid]
            history = [
                {"role": "user" if i["type"] == "userMessage" else "assistant", "content": i["text"]}
                for i in thread["items"][-20:]
                if i["type"] in ("userMessage", "agentMessage")
            ]
            if context.get("specialist_task"):
                # A delegated report sees only its assignment and shared observations, never peer chats.
                history = [{"role": "user", "content": text}]
            instructions = (
                "You are a Wayland analytical assistant. Role: "
                + thread["purpose"]
                + ". Explain supplied evidence and uncertainty. Never claim live data, a trade or a background task occurred without supplied evidence. "
                "You have no tools and cannot place orders or change risk settings. Treat chat as discussion, not an execution instruction. "
                "Answer in the user's language. Current context: " + json.dumps(supplied, default=str)
            )
            correlation = str(uuid.uuid4())
            store.event(
                "ui.analysis.input",
                {
                    "model": config.openai_model,
                    "instructions": instructions,
                    "history": history,
                    "schema": AnalyticalReply.model_json_schema(),
                    "coordination_id": context.get("coordination_id"),
                    "prompt_version": "operator-chat-v1",
                },
                correlation,
            )
        task = asyncio.create_task(
            analyze(self.root, config, instructions, history, AnalyticalReply.model_json_schema())
        )
        try:
            while not task.done():
                if cancel.is_set():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    return ""
                await asyncio.sleep(0.1)
            response = await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        answer = AnalyticalReply.model_validate(response.output).answer
        with self.store() as store:
            store.event(
                "ui.analysis.response",
                {"id": response.id, "model": response.model, "answer": answer, "provider": "codex-cli"},
                correlation,
            )
        return answer


def serve(root: Path):
    from .tui.client import socket_path

    lease = ExecutionLease(root / "state/ui-service.sqlite")
    runtime = OperatorRuntime(root)
    runtime.connections.start()
    with runtime.store() as store:
        store.set("browser:ibkr_enabled", False)
        if store.get("broker:enabled", False):
            runtime.broker_observer.start()
    path = socket_path(root)
    path.unlink(missing_ok=True)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            result: dict[str, Any]
            self.connection.settimeout(10)
            try:
                raw = self.rfile.readline(1024 * 1024)
                payload = json.loads(raw)
                action = payload.pop("action")
                if action == "shutdown":
                    result = {"result": {"stopping": True}}
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    result = {"result": runtime.dispatch(action, **payload)}
            except (ValueError, TypeError, KeyError) as error:
                result = {"error": str(error)}
            except Exception as error:  # noqa: BLE001 -- do not leak SDK/database payloads
                result = {"error": "Wayland operation failed: " + type(error).__name__}
            self.wfile.write((json.dumps(result, default=str) + "\n").encode())

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    server.daemon_threads = True
    path.chmod(0o600)

    def stop(*_):
        for cancel in list(runtime.active.values()):
            cancel.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        runtime.broker_observer.shutdown()
        runtime.connections.close()
        for cancel in list(runtime.active.values()):
            cancel.set()
        server.server_close()
        path.unlink(missing_ok=True)
        lease.close()

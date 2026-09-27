"""Independent operator service; analysis chat has no broker/execution tools."""

import asyncio
import json
import os
import signal
import socketserver
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .agents import REGISTRY
from .audit import AuditStore, ExecutionLease
from .config import Settings, load_settings
from .models import utcnow


class OperatorRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.active: dict[str, threading.Event] = {}
        from .broker.observer import BrokerObserver

        self.broker_observer = BrokerObserver(self)
        with self.store() as store:
            if store.get("ui:threads") is None:
                threads = {}
                for role in REGISTRY:
                    threads[role.agent_id] = self.new_thread(
                        role.agent_id, role.agent_id.split(":")[1].replace("-", " ").title(), role.purpose
                    )
                store.set("ui:threads", threads)
            threads = store.get("ui:threads", {})
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
                    + ".\n\nThis is Wayland's analytical workspace. Configure an OpenAI API model in Setup. Chat cannot place orders. Broker state is not verified until reconciliation succeeds.",
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
        info = store.get("ui:provider", {})
        provider_status = info.get("status") or (
            "Credentials present · not verified"
            if os.environ.get("OPENAI_API_KEY")
            else "OPENAI_API_KEY missing"
        )
        result = {
            "connected": True,
            "tasks": [],
            "models": [],
            "requests": [],
            "trading": trading,
            "settings": store.get("ui:settings", {"mouseEnabled": True}),
            "tradingSettings": config.model_dump(mode="json"),
            "providers": {"openai": {"status": provider_status, "model": config.openai_model}},
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
                    shown = dict(thread, model=config.openai_model or "Not configured")
                    shown["items"] = thread["items"] + audit_items
                    result[bucket][tid] = shown
        return result

    def dispatch(self, action, **params):
        with self.lock, self.store() as store:
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
                    {"step": "Answer through the configured OpenAI API model", "status": "inProgress"},
                ]
                cancel = threading.Event()
                self.active[tid] = cancel
                store.set("ui:threads", threads)
                context = self.snapshot(store)["trading"]
                worker = threading.Thread(target=self.analyze, args=(tid, text, context, cancel), daemon=True)
                worker.start()
                return {"accepted": True}
            elif action == "interrupt":
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
                from .tui.setup_view import SECTIONS

                allowed = {key for keys in SECTIONS.values() for key in keys}
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
                if any(not account.startswith("DU") for account in settings.account_allowlist):
                    raise ValueError("Only DU paper accounts are allowed")
                if settings.ibkr_account and not settings.ibkr_account.startswith("DU"):
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
            if not config.openai_model or not os.environ.get("OPENAI_API_KEY"):
                reply = "Analysis is waiting for configuration. Set the OpenAI API model in Setup and provide OPENAI_API_KEY to the Wayland service. CLI login does not supply API access. No order was sent."
                failed = True
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
        from openai import AsyncOpenAI
        from pydantic import BaseModel, ConfigDict

        class AnalyticalReply(BaseModel):
            model_config = ConfigDict(extra="forbid")
            answer: str

        # Account IDs, broker IDs and private config are not analytical context.
        supplied = {k: context.get(k) for k in ("market", "setup", "operating_state", "state_reasons")}
        with self.store() as store:
            thread = store.get("ui:threads")[tid]
            from openai.types.responses import ResponseInputParam

            history: ResponseInputParam = [
                {"role": "user" if i["type"] == "userMessage" else "assistant", "content": i["text"]}
                for i in thread["items"][-20:]
                if i["type"] in ("userMessage", "agentMessage")
            ]
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
                    "prompt_version": "operator-chat-v1",
                },
                correlation,
            )
        async with AsyncOpenAI(timeout=config.analysis_timeout_seconds, max_retries=0) as client:
            task = asyncio.create_task(
                client.responses.parse(
                    model=config.openai_model,
                    instructions=instructions,
                    input=history,
                    text_format=AnalyticalReply,
                    store=False,
                    max_output_tokens=2000,
                )
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
            if response.status != "completed" or response.output_parsed is None:
                raise ValueError("Incomplete or refused analysis")
            with self.store() as store:
                store.event(
                    "ui.analysis.response",
                    {"id": response.id, "model": response.model, "answer": response.output_parsed.answer},
                    correlation,
                )
                store.set("ui:provider", {"status": "Verified by successful API response"})
            return response.output_parsed.answer


def serve(root: Path):
    from .tui.client import socket_path

    lease = ExecutionLease(root / "state/ui-service.sqlite")
    runtime = OperatorRuntime(root)
    with runtime.store() as store:
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
        for cancel in list(runtime.active.values()):
            cancel.set()
        server.server_close()
        path.unlink(missing_ok=True)
        lease.close()

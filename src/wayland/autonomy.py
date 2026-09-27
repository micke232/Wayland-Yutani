"""Bind observations to the existing event/risk/execution pipeline without user prompts."""

import asyncio

from .audit import AuditStore
from .execution import ExecutionEngine
from .providers.coordinator import CoordinatorProvider
from .service import WaylandService


class AutonomousSession:
    def __init__(self, runtime, broker, settings):
        self.runtime = runtime
        store = AuditStore(runtime.root / "state/wayland.sqlite")
        try:
            self.engine = ExecutionEngine(store, broker, settings)
        except BaseException:
            store.close()
            raise
        self.provider = CoordinatorProvider(settings, store, runtime)
        self.service = WaylandService(self.engine, self.provider)
        self.task = None
        self.protection_task = None
        store.set("autonomy", {"status": "OBSERVING", "reason": "Waiting for verified market events"})

    def offer(self, snapshot):
        if self.task and not self.task.done():
            # Monitoring existing exposure must not wait for model latency.
            self.engine.store.set("market", snapshot.model_dump(mode="json"))
            if self.protection_task is None or self.protection_task.done():
                self.protection_task = asyncio.create_task(self.protect(snapshot))
            return
        self.task = asyncio.create_task(self.process(snapshot))

    async def protect(self, snapshot):
        try:
            await self.service.positions.protect(snapshot)
        except Exception as error:  # noqa: BLE001 -- conservative monitoring failure
            self.engine.store.set(
                "autonomy", {"status": "BLOCKED", "reason": "Position monitoring unavailable"}
            )
            self.engine.store.event("autonomy.monitor_error", {"type": type(error).__name__})

    async def process(self, snapshot):
        try:
            result = await self.service.market_event(snapshot)
            if result is not None:
                with self.runtime.lock, self.runtime.store() as store:
                    threads = store.get("ui:threads")
                    from .agents import COORDINATOR_ID

                    if COORDINATOR_ID in threads:
                        threads[COORDINATOR_ID]["items"].append(
                            self.runtime.message(
                                "agentMessage",
                                "Deterministic execution result: "
                                + result.state
                                + "\n"
                                + ", ".join(result.reasons),
                            )
                        )
                        store.set("ui:threads", threads)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- fail closed without raw SDK payloads
            self.engine.store.set("autonomy", {"status": "BLOCKED", "reason": type(error).__name__})
            self.engine.store.event("autonomy.error", {"type": type(error).__name__})

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.protection_task and not self.protection_task.done():
            self.protection_task.cancel()
            try:
                await self.protection_task
            except asyncio.CancelledError:
                pass
        await self.provider.close()
        await self.engine.close()

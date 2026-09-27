"""Event-driven orchestration with independently refreshable position monitoring."""

import asyncio
import uuid

from .agents import REGISTRY
from .execution import ExecutionEngine
from .market import candidates
from .models import Action, MarketSnapshot, OperatingState, utcnow
from .positions import PositionManager
from .providers.openai import OpenAIProvider
from .strategy import SetupDetector


class WaylandService:
    def __init__(self, engine: ExecutionEngine, provider: OpenAIProvider):
        self.engine, self.provider = engine, provider
        self.detector = SetupDetector(engine.settings, engine.store)
        self.analysis_lock = asyncio.Lock()
        self.positions = PositionManager(engine)
        engine.store.set("agents", [{"id": r.agent_id, "purpose": r.purpose} for r in REGISTRY])
        engine.store.set("runtime_session", "wayland:" + str(uuid.uuid4()))

    async def market_event(self, snapshot: MarketSnapshot, news: tuple[dict, ...] = ()):
        self.engine.store.set("market", snapshot.model_dump(mode="json"))
        await self.positions.protect(snapshot)
        if self.analysis_lock.locked():
            return None  # Never queue stale model requests.
        async with self.analysis_lock:
            event = self.detector.observe(snapshot)
            if not event:
                return None
            if not await self.engine.reconcile() or self.engine.state not in (
                OperatingState.READY,
                OperatingState.DEGRADED,
            ):
                return None
            offered = candidates(snapshot, self.engine.settings, utcnow())
            if not offered:
                self.engine.store.event(
                    "strategy.wait", {"reason": "no_qualified_candidate"}, snapshot.snapshot_id
                )
                return None
            context = {
                "market": snapshot.model_dump(mode="json"),
                "candidates": [c.model_dump(mode="json") for c in offered],
                "setup": {"event": event},
                "news": list(news),
                "analysis_id": str(uuid.uuid4()),
            }
            proposal = await self.provider.analyze(context)
            if proposal is None:
                self.engine.set_state(OperatingState.DEGRADED, ["analysis_unavailable"])
                return None
            self.engine.store.set("latest_decision", proposal.model_dump(mode="json"))
            if proposal.action == Action.WAIT:
                return None
            candidate = next((c for c in offered if c.candidate_id == proposal.candidate_id), None)
            # The engine rechecks freshness after reasoning. A slow model cannot trade on the old quote.
            return await self.engine.execute(proposal, snapshot, candidate)

    async def monitor(self, stop: asyncio.Event, interval: float = 2) -> None:
        while not stop.is_set():
            await self.engine.reconcile()
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass

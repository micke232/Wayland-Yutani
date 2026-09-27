"""Event detector; a quote does not imply an LLM call."""

from decimal import Decimal

from .audit import AuditStore
from .config import Settings
from .market import fresh
from .models import MarketSnapshot, utcnow


class SetupDetector:
    def __init__(self, settings: Settings, store: AuditStore):
        self.settings, self.store = settings, store

    def observe(self, snapshot: MarketSnapshot) -> str | None:
        key = "setup:" + snapshot.symbol
        now = utcnow()
        if (
            snapshot.symbol not in self.settings.allowed_symbols
            or not snapshot.connected
            or not snapshot.live
            or not fresh(snapshot.timestamp, now, self.settings.max_market_data_age_seconds)
        ):
            self.store.set(key, {"phase": "WAITING", "reason": "unusable_market_data"})
            return None
        state = self.store.get(key, {})
        if state.get("snapshot_id") == snapshot.snapshot_id:
            return None
        if state.get("date") != str(now.date()):
            state = {}
        low = min(snapshot.price, Decimal(state.get("low", str(snapshot.price))))
        high = max(snapshot.price, Decimal(state.get("high", str(snapshot.price))))
        phase = state.get("phase", "WAITING")
        event = None
        if phase in ("WAITING", "FIRED") and snapshot.price >= low * (1 + self.settings.rebound_fraction):
            phase, high = "REBOUND", snapshot.price
        elif phase == "REBOUND" and snapshot.price <= high * (1 - self.settings.reversal_fraction):
            last = float(state.get("last_analysis", 0))
            if now.timestamp() - last >= self.settings.analysis_cooldown_seconds:
                phase, event = "FIRED", "rebound_reversal"
                state["last_analysis"] = now.timestamp()
                low, high = snapshot.price, snapshot.price
        state.update(
            phase=phase, low=str(low), high=str(high), date=str(now.date()), snapshot_id=snapshot.snapshot_id
        )
        self.store.set(key, state)
        if event:
            self.store.event("setup.detected", {"event": event, "state": state}, snapshot.snapshot_id)
        return event

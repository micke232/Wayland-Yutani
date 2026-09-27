"""Broker-authoritative position monitoring and narrowly bounded protective exits."""

from .market import fresh
from .models import Action, Direction, MarketSnapshot, TradeProposal, utcnow


class PositionManager:
    def __init__(self, engine):
        self.engine = engine

    async def protect(self, market: MarketSnapshot) -> list:
        if (
            not market.connected
            or not market.live
            or not fresh(market.timestamp, utcnow(), self.engine.settings.max_market_data_age_seconds)
        ):
            return []
        if not await self.engine.reconcile() or self.engine.portfolio is None:
            return []
        results = []
        for position in self.engine.portfolio.positions:
            if position.symbol != market.symbol or position.invalidation_price is None:
                continue
            if market.price < position.invalidation_price:
                continue
            proposal = TradeProposal(
                proposal_id=f"protect:{position.position_id}:{market.snapshot_id}",
                snapshot_id=market.snapshot_id,
                timestamp=utcnow(),
                symbol=position.symbol,
                action=Action.EXIT,
                direction=Direction.NEUTRAL,
                instrument=position.candidate.instrument,
                candidate_id=None,
                position_id=position.position_id,
                quantity=position.quantity,
                estimated_cost_sek=None,
                max_loss_sek=None,
                confidence=1.0,
                thesis="Deterministic protective exit: configured invalidation price crossed",
                invalidation=position.invalidation,
                invalidation_price=position.invalidation_price,
            )
            self.engine.store.event(
                "position.protective_exit", proposal.model_dump(mode="json"), position.position_id
            )
            results.append(await self.engine.execute(proposal, market, None))
        return results

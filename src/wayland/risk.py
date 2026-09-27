"""All entry permissions and amounts are independently calculated in Python."""

import uuid
from datetime import datetime
from decimal import Decimal

from .config import Settings
from .market import fresh, price_candidate
from .models import (
    Action,
    Candidate,
    Direction,
    MarketSnapshot,
    OperatingState,
    OrderCommand,
    OrderState,
    PortfolioState,
    RiskDecision,
    TradeProposal,
    TradingMode,
)


class RiskEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(
        self,
        proposal: TradeProposal,
        market: MarketSnapshot,
        portfolio: PortfolioState,
        candidate: Candidate | None,
        state: OperatingState,
        kill: bool,
        entries_today: int,
        now: datetime,
    ) -> RiskDecision:
        s = self.settings
        reasons: list[str] = []
        if s.mode != TradingMode.PAPER or portfolio.mode != TradingMode.PAPER:
            reasons.append("live_hard_locked")
        if portfolio.account not in s.account_allowlist:
            reasons.append("account_not_allowlisted")
        if not portfolio.healthy or not portfolio.complete:
            reasons.append("broker_unhealthy_or_incomplete")
        if not fresh(portfolio.timestamp, now, s.max_market_data_age_seconds):
            reasons.append("stale_broker_state")
        if proposal.snapshot_id != market.snapshot_id or proposal.symbol != market.symbol:
            reasons.append("snapshot_mismatch")
        if not fresh(proposal.timestamp, now, s.max_market_data_age_seconds):
            reasons.append("stale_proposal")
        if (
            not market.connected
            or not market.live
            or not fresh(market.timestamp, now, s.max_market_data_age_seconds)
        ):
            reasons.append("market_stale_disconnected_or_delayed")
        if not fresh(market.fx_timestamp, now, s.max_fx_age_seconds):
            reasons.append("stale_fx")
        if proposal.action == Action.WAIT:
            return RiskDecision(
                proposal_id=proposal.proposal_id, approved=False, reasons=tuple(reasons or ["wait"])
            )
        pending = [
            o
            for o in portfolio.orders
            if o.state in (OrderState.OPEN, OrderState.PARTIAL, OrderState.SUBMITTING, OrderState.UNKNOWN)
        ]
        position = None
        if proposal.action == Action.ENTER:
            if state != OperatingState.READY:
                reasons.append("entries_not_ready")
            if kill:
                reasons.append("kill_switch")
            if proposal.symbol not in s.allowed_symbols:
                reasons.append("symbol_not_allowed")
            if proposal.instrument not in s.allowed_instruments:
                reasons.append("instrument_not_allowed")
            if proposal.direction != Direction.BEARISH:
                reasons.append("direction_not_supported")
            if portfolio.pnl_date != now.date():
                reasons.append("daily_pnl_not_current")
            if portfolio.daily_realized_pnl_sek + portfolio.daily_unrealized_pnl_sek <= -s.max_daily_loss_sek:
                reasons.append("daily_loss_limit")
            if entries_today >= s.max_trades_per_day:
                reasons.append("daily_trade_limit")
            occupied = {p.position_id for p in portfolio.positions}
            occupied.update(o.command.position_id for o in pending if o.command.action == Action.ENTER)
            if len(occupied) >= s.max_open_positions:
                reasons.append("open_position_limit")
            if (
                candidate is None
                or candidate.candidate_id != proposal.candidate_id
                or candidate.instrument != proposal.instrument
            ):
                reasons.append("candidate_mismatch")
        else:
            if state == OperatingState.RECONCILING:
                reasons.append("position_state_unverified")
            position = next((p for p in portfolio.positions if p.position_id == proposal.position_id), None)
            if position is None or position.symbol != proposal.symbol:
                reasons.append("position_not_found")
            elif proposal.quantity > position.quantity:
                reasons.append("exit_exceeds_position")
            elif any(o.command.position_id == position.position_id for o in pending):
                reasons.append("position_has_pending_order")
            candidate = position.candidate if position else None
        command = None
        if candidate is not None:
            try:
                limit, quotes = price_candidate(candidate, market, s, now, proposal.action == Action.EXIT)
                fees = s.fee_per_contract_sek * len(quotes) * proposal.quantity
                # Reserve both entry and exit fees for maximum-loss bounds.
                debit = limit * quotes[0].contract.multiplier * proposal.quantity * market.usd_sek
                loss = debit + fees * 2 if proposal.action == Action.ENTER else Decimal(0)
                if proposal.action == Action.ENTER:
                    if loss > s.max_position_sek or debit + fees > s.max_position_sek:
                        reasons.append("position_cost_or_loss_limit")
                    if proposal.max_loss_sek is None or proposal.max_loss_sek < loss:
                        reasons.append("model_understated_loss")
                    if proposal.estimated_cost_sek is None or proposal.estimated_cost_sek < debit + fees:
                        reasons.append("model_understated_cost")
                intent_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL, "wayland:" + portfolio.account + ":" + proposal.proposal_id
                    )
                )
                command = OrderCommand(
                    intent_id=intent_id,
                    account=portfolio.account,
                    symbol=proposal.symbol,
                    action=proposal.action,
                    candidate=candidate,
                    contracts=tuple(q.contract for q in quotes),
                    quantity=proposal.quantity,
                    limit_usd=limit,
                    usd_sek=market.usd_sek,
                    max_loss_sek=loss,
                    fees_sek=fees,
                    position_id=position.position_id if position else intent_id,
                    invalidation=proposal.invalidation,
                    invalidation_price=proposal.invalidation_price,
                )
            except ValueError as error:
                reasons.append(str(error))
        if command is None and not reasons:
            reasons.append("maximum_loss_not_determinable")
        return RiskDecision(
            proposal_id=proposal.proposal_id,
            approved=not reasons,
            reasons=tuple(reasons),
            command=command if not reasons else None,
        )

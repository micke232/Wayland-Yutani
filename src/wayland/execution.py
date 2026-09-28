"""Single owner, persist-before-send, no retry on uncertain broker submission."""

import asyncio
import json
from collections import Counter
from collections.abc import Callable

from .audit import AuditStore, ExecutionLease
from .broker.base import Broker
from .config import Settings
from .market import fresh
from .models import (
    TERMINAL,
    Action,
    Candidate,
    ExecutionResult,
    MarketSnapshot,
    OperatingState,
    OrderCommand,
    OrderState,
    PortfolioState,
    RiskDecision,
    TradeProposal,
    TradingMode,
    utcnow,
)
from .risk import RiskEngine


class ExecutionEngine:
    def __init__(
        self,
        store: AuditStore,
        broker: Broker,
        settings: Settings,
        fault: Callable[[str], None] | None = None,
    ):
        self.store, self.broker, self.settings = store, broker, settings
        self.lease = ExecutionLease(store.path)
        self.lock = asyncio.Lock()
        self.risk = RiskEngine(settings)
        self.portfolio: PortfolioState | None = None
        self.state = OperatingState.RECONCILING
        self.fault = fault or (lambda _: None)
        self.stopping = False
        self.set_state(OperatingState.RECONCILING, ["startup_requires_broker_verification"])

    def set_state(self, state: OperatingState, reasons: list[str]) -> None:
        self.state = state
        with self.store.transaction():
            self.store.set("operating_state", state)
            self.store.set("state_reasons", reasons)
            self.store.set("heartbeat", utcnow().isoformat())
            self.store.event("operating_state", {"state": state, "reasons": reasons})

    async def reconcile(self) -> bool:
        async with self.lock:
            return await self._reconcile()

    async def _reconcile(self) -> bool:
        self.set_state(OperatingState.RECONCILING, ["broker_snapshot_pending"])
        try:
            portfolio = await asyncio.wait_for(self.broker.snapshot(), 15)
        except Exception as error:  # noqa: BLE001 -- fail closed without exposing SDK payloads
            from .broker.diagnostics import diagnostic

            self.store.event("broker.snapshot_failed", diagnostic("snapshot", error))
            self.store.set("broker:snapshot_error", diagnostic("snapshot", error))
            self.portfolio = None
            self.set_state(OperatingState.SAFE, ["broker_snapshot_failed:" + type(error).__name__])
            return False
        self.portfolio = portfolio
        issues = []
        if (
            not portfolio.healthy
            or not portfolio.complete
            or not fresh(portfolio.timestamp, utcnow(), self.settings.max_market_data_age_seconds)
        ):
            issues.append("broker_state_unverified")
        if portfolio.mode != TradingMode.PAPER or self.settings.mode != TradingMode.PAPER:
            issues.append("live_hard_locked")
        if portfolio.account not in self.settings.account_allowlist:
            issues.append("account_not_allowlisted")
        prior = self.store.get("verified_portfolio", {})
        prior_orders = {o["intent_id"]: o for o in prior.get("orders", [])}
        self.store.set("portfolio", portfolio.model_dump(mode="json"))
        if issues:
            self.set_state(OperatingState.SAFE, issues)
            return False
        intents = {row["intent_id"]: row for row in self.store.intents()}
        observed = {}
        for order in portfolio.orders:
            if order.intent_id in observed:
                issues.append("duplicate_broker_intent:" + order.intent_id)
            observed[order.intent_id] = order
            saved = intents.get(order.intent_id)
            if saved is None:
                issues.append("unmanaged_broker_order:" + order.broker_id)
                continue
            if (
                OrderCommand.model_validate_json(saved["command"]) != order.command
                or order.command.account != portfolio.account
                or order.filled > order.command.quantity
                or (order.state == OrderState.FILLED and order.filled != order.command.quantity)
            ):
                issues.append("broker_order_conflict:" + order.intent_id)
                continue
            previous_order = prior_orders.get(order.intent_id)
            if previous_order and order.filled < previous_order["filled"]:
                issues.append("broker_fill_regression:" + order.intent_id)
                continue
            old = OrderState(saved["status"])
            if old in TERMINAL and order.state != old:
                issues.append("terminal_order_conflict:" + order.intent_id)
                continue
            if saved["broker_id"] and saved["broker_id"] != order.broker_id:
                issues.append("broker_id_conflict:" + order.intent_id)
                continue
            if saved["status"] != order.state or saved["broker_id"] != order.broker_id:
                self.store.transition(order.intent_id, order.state, order.broker_id)
        for intent_id, row in intents.items():
            if intent_id not in observed:
                if row["status"] == OrderState.PREPARED:
                    # No broker call can occur before durable SUBMITTING. Never auto-replay an old proposal.
                    self.store.transition(intent_id, OrderState.ABORTED)
                elif row["status"] not in (OrderState.REJECTED, OrderState.ABORTED):
                    # Missing historical fills/order data is uncertainty, not evidence of non-submission.
                    issues.append("unresolved_intent:" + intent_id)
        expected: Counter[str] = Counter()
        fill_ids = set()
        fills: Counter[str] = Counter()
        for fill in portfolio.fills:
            if fill.execution_id in fill_ids:
                issues.append("duplicate_execution_id")
            fill_ids.add(fill.execution_id)
            fills[fill.intent_id] += fill.quantity
            if fill.intent_id not in observed:
                issues.append("unmatched_execution:" + fill.intent_id)
        for order in portfolio.orders:
            if fills[order.intent_id] != order.filled:
                issues.append("fill_quantity_conflict:" + order.intent_id)
            expected[order.command.position_id] += order.filled * (
                1 if order.command.action == Action.ENTER else -1
            )
        actual = {p.position_id: p.quantity for p in portfolio.positions}
        if len(actual) != len(portfolio.positions) or any(q < 0 for q in expected.values()):
            issues.append("invalid_position_exposure")
        if actual != {key: q for key, q in expected.items() if q}:
            issues.append("position_quantity_conflict")
        for position in portfolio.positions:
            originating = next(
                (
                    o.command
                    for o in portfolio.orders
                    if o.command.position_id == position.position_id and o.command.action == Action.ENTER
                ),
                None,
            )
            if (
                originating is None
                or originating.candidate != position.candidate
                or originating.symbol != position.symbol
            ):
                issues.append("position_contract_conflict:" + position.position_id)
        self.store.event("reconciliation", {"issues": issues, "portfolio": portfolio.model_dump(mode="json")})
        if issues:
            self.set_state(OperatingState.SAFE, issues)
            return False
        self.store.set("verified_portfolio", portfolio.model_dump(mode="json"))
        if self.stopping or self.store.get("kill_switch", False):
            self.set_state(OperatingState.SAFE, ["shutdown" if self.stopping else "kill_switch"])
        elif not self.store.get("analysis_healthy", True):
            self.set_state(OperatingState.DEGRADED, ["analysis_unavailable"])
        else:
            self.set_state(OperatingState.READY, [])
        return True

    def entries_today(self) -> int:
        today = str(utcnow().date())
        return sum(
            row["created"].startswith(today)
            and json.loads(row["command"])["action"] == Action.ENTER
            and row["status"] not in (OrderState.ABORTED, OrderState.REJECTED)
            for row in self.store.intents()
        )

    async def execute(
        self,
        proposal: TradeProposal,
        market: MarketSnapshot,
        candidate: Candidate | None,
        decision: RiskDecision | None = None,
    ) -> ExecutionResult:
        async with self.lock:
            existing = self.store.lookup(proposal.proposal_id)
            if existing:
                return ExecutionResult(
                    intent_id=existing["intent_id"],
                    state=existing["status"],
                    broker_id=existing["broker_id"],
                    reasons=("duplicate_proposal_no_resubmission",),
                )
            if self.stopping:
                return ExecutionResult(intent_id=None, state="REJECTED", reasons=("shutdown",))
            if not await self._reconcile() or self.portfolio is None:
                return ExecutionResult(
                    intent_id=None, state="REJECTED", reasons=tuple(self.store.get("state_reasons"))
                )
            verified = self.risk.evaluate(
                proposal,
                market,
                self.portfolio,
                candidate,
                self.state,
                self.store.get("kill_switch", False),
                self.entries_today(),
                utcnow(),
            )
            self.store.event("market.snapshot", market.model_dump(mode="json"), proposal.snapshot_id)
            self.store.event("proposal", proposal.model_dump(mode="json"), proposal.proposal_id)
            self.store.event("risk.decision", verified.model_dump(mode="json"), proposal.proposal_id)
            if decision is not None and decision != verified:
                return ExecutionResult(intent_id=None, state="REJECTED", reasons=("risk_decision_mismatch",))
            if not verified.approved or verified.command is None:
                return ExecutionResult(intent_id=None, state="REJECTED", reasons=verified.reasons)
            command = verified.command
            # This release has no code path that unlocks LIVE, even for EXIT or cancellation.
            if self.settings.mode != TradingMode.PAPER or self.portfolio.mode != TradingMode.PAPER:
                return ExecutionResult(intent_id=None, state="REJECTED", reasons=("live_hard_locked",))
            self.store.prepare(proposal, command)
            self.fault("after_intent")
            if proposal.action == Action.ENTER and self.store.get("kill_switch", False):
                self.store.transition(command.intent_id, OrderState.ABORTED)
                return ExecutionResult(intent_id=command.intent_id, state="ABORTED", reasons=("kill_switch",))
            self.store.transition(command.intent_id, OrderState.SUBMITTING)
            self.fault("before_broker")
            try:
                order = await asyncio.wait_for(self.broker.submit(command), 15)
                self.fault("after_broker")
                if order.intent_id != command.intent_id or order.command != command:
                    raise ValueError("broker_acknowledgement_conflict")
                self.store.transition(command.intent_id, order.state, order.broker_id)
            except Exception as error:  # noqa: BLE001 -- fail closed without exposing SDK payloads
                self.store.transition(command.intent_id, OrderState.UNKNOWN)
                self.set_state(OperatingState.SAFE, ["submission_uncertain:" + type(error).__name__])
                return ExecutionResult(
                    intent_id=command.intent_id, state="UNKNOWN", reasons=("reconcile_before_any_retry",)
                )
            self.set_state(OperatingState.RECONCILING, ["order_submitted_refresh_required"])
            return ExecutionResult(intent_id=command.intent_id, state=order.state, broker_id=order.broker_id)

    async def cancel(self, intent_id: str) -> bool:
        async with self.lock:
            if not await self._reconcile() or self.portfolio is None:
                return False
            order = next((o for o in self.portfolio.orders if o.intent_id == intent_id), None)
            if order is None or order.state not in (OrderState.OPEN, OrderState.PARTIAL):
                return False
            self.store.event("cancel.requested", {"broker_id": order.broker_id}, intent_id)
            try:
                await asyncio.wait_for(self.broker.cancel(order.broker_id), 15)
            except Exception as error:  # noqa: BLE001 -- fail closed without exposing SDK payloads
                self.set_state(OperatingState.SAFE, ["cancel_uncertain:" + type(error).__name__])
                return False
            return await self._reconcile()

    async def close(self) -> None:
        self.stopping = True
        async with self.lock:
            self.set_state(OperatingState.SAFE, ["shutdown"])
            try:
                await self.broker.close()
            finally:
                self.lease.close()
                self.store.close()

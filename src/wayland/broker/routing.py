"""Conservative native paper order lifecycle; unknown broker evidence blocks entries."""

from collections import Counter
from decimal import Decimal

from ..models import (
    Action,
    BrokerOrder,
    Fill,
    InstrumentType,
    OrderCommand,
    OrderState,
    PortfolioState,
    Position,
    TradingMode,
    utcnow,
)

STATES = {
    "Submitted": OrderState.OPEN,
    "PreSubmitted": OrderState.OPEN,
    "PendingSubmit": OrderState.SUBMITTING,
    "PendingCancel": OrderState.OPEN,
    "Filled": OrderState.FILLED,
    "Cancelled": OrderState.CANCELLED,
    "ApiCancelled": OrderState.CANCELLED,
}


def integer(value):
    result = Decimal(str(value))
    if not result.is_finite() or result != int(result) or result < 0:
        raise ValueError("invalid broker quantity")
    return int(result)


class PaperRouting:
    """Local journal retains broker evidence, never substitutes it for current positions."""

    def __init__(self, broker, store):
        self.broker, self.store = broker, store

    def key(self, name):
        return "ibkr:" + self.broker.settings.ibkr_account + ":" + name

    def commands(self):
        return {
            r["intent_id"]: OrderCommand.model_validate_json(r["command"])
            for r in self.store.intents()
            if OrderCommand.model_validate_json(r["command"]).account == self.broker.settings.ibkr_account
        }

    def check_trade(self, trade, command):
        o, c = trade.order, trade.contract
        if (
            command.candidate.instrument != InstrumentType.LONG_PUT
            or len(command.contracts) != 1
            or c.secType != "OPT"
            or c.conId != command.contracts[0].con_id
            or o.account != command.account
            or o.action != ("BUY" if command.action == Action.ENTER else "SELL")
            or integer(o.totalQuantity) != command.quantity
            or o.orderType != "LMT"
            or Decimal(str(o.lmtPrice)) != command.limit_usd
        ):
            raise ValueError("broker order terms conflict")

    async def snapshot(self):
        b = self.broker
        positions, open_orders, executions, completed = await b.read_state()
        commands = self.commands()
        orders = {
            k: BrokerOrder.model_validate_json(v) for k, v in self.store.get(self.key("orders"), {}).items()
        }
        fills = {k: Fill.model_validate_json(v) for k, v in self.store.get(self.key("fills"), {}).items()}
        issues = []
        seen: dict[str, str] = {}
        for trade in [*completed, *open_orders]:
            if trade.order.account != b.settings.ibkr_account:
                continue
            ref = trade.order.orderRef
            command = commands.get(ref)
            if command is None:
                issues.append("unmanaged_broker_order")
                continue
            self.check_trade(trade, command)
            identity = str(trade.order.permId or trade.order.orderId)
            if ref in seen and seen[ref] != identity:
                issues.append("duplicate_broker_intent")
            seen[ref] = identity
            sent = self.store.get(self.key("sent:" + ref))
            if sent is None:
                issues.append("missing_submission_journal")
                continue
            if trade.order.clientId != sent["client_id"] or (
                trade.order.orderId and trade.order.orderId != sent["order_id"]
            ):
                issues.append("broker_order_identity_conflict")
                continue
            filled = integer(trade.orderStatus.filled)
            state = STATES.get(trade.orderStatus.status, OrderState.UNKNOWN)
            if filled and state == OrderState.OPEN:
                state = OrderState.PARTIAL
            if state == OrderState.UNKNOWN:
                issues.append("unknown_order_status")
            old = orders.get(ref)
            if old and filled < old.filled:
                issues.append("broker_fill_regression")
                continue
            orders[ref] = BrokerOrder(
                intent_id=ref,
                broker_id=f"{sent['client_id']}:{sent['order_id']}",
                state=state,
                command=command,
                filled=filled,
            )
        for raw in executions:
            e = raw.execution
            if e.acctNumber != b.settings.ibkr_account:
                continue
            command = commands.get(e.orderRef)
            if command is None:
                issues.append("unmanaged_execution")
                continue
            if raw.contract.secType != "OPT" or raw.contract.conId != command.candidate.long_con_id:
                issues.append("execution_contract_conflict")
                continue
            if e.side != ("BOT" if command.action == Action.ENTER else "SLD"):
                issues.append("execution_side_conflict")
                continue
            fill = Fill(
                execution_id=e.execId,
                intent_id=e.orderRef,
                quantity=integer(e.shares),
                price_usd=Decimal(str(e.price)),
                timestamp=e.time,
            )
            if fill.execution_id in fills and fills[fill.execution_id] != fill:
                issues.append("execution_correction_requires_review")
                continue
            # IB corrections change the suffix: never count both as independent fills.
            if any(k.rsplit(".", 1)[0] == e.execId.rsplit(".", 1)[0] and k != e.execId for k in fills):
                issues.append("execution_correction_requires_review")
                continue
            fills[e.execId] = fill
        for ref, order in orders.items():
            if (
                order.state not in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)
                and ref not in seen
            ):
                issues.append("missing_active_order")
        totals: Counter[str] = Counter()
        costs: dict[str, Decimal] = {}
        for fill in fills.values():
            if fill.intent_id not in commands:
                issues.append("unknown_historical_execution")
                continue
            command = commands[fill.intent_id]
            totals[command.position_id] += fill.quantity * (1 if command.action == Action.ENTER else -1)
            if command.action == Action.ENTER:
                costs[command.position_id] = (
                    costs.get(command.position_id, Decimal(0)) + fill.price_usd * fill.quantity
                )
        expected: Counter[int] = Counter()
        mapped = []
        for pid, quantity in totals.items():
            if quantity < 0:
                issues.append("negative_exposure")
            if quantity <= 0:
                continue
            entry = next(c for c in commands.values() if c.position_id == pid and c.action == Action.ENTER)
            entry_fills = [f for f in fills.values() if f.intent_id == entry.intent_id]
            bought = sum(f.quantity for f in entry_fills)
            expected[entry.candidate.long_con_id] += quantity
            mapped.append(
                Position(
                    position_id=pid,
                    symbol=entry.symbol,
                    candidate=entry.candidate,
                    quantity=quantity,
                    average_entry_usd=costs[pid] / bought,
                    max_loss_sek=entry.max_loss_sek * quantity / entry.quantity,
                    invalidation=entry.invalidation,
                    invalidation_price=entry.invalidation_price,
                )
            )
        actual: dict[int, Decimal] = {}
        for position in positions:
            if position.account == b.settings.ibkr_account and position.position:
                actual[position.contract.conId] = actual.get(position.contract.conId, Decimal(0)) + Decimal(
                    str(position.position)
                )
        if dict(actual) != dict(expected):
            issues.append("broker_position_conflict")
        # Account-wide PnL must be broker-sourced and converted to SEK; no assumed zero.
        realized, unrealized = await b.account_pnl()
        with self.store.transaction():
            self.store.set(self.key("orders"), {k: v.model_dump_json() for k, v in orders.items()})
            self.store.set(self.key("fills"), {k: v.model_dump_json() for k, v in fills.items()})
            self.store.set(self.key("issues"), issues)
        return PortfolioState(
            account=b.settings.ibkr_account,
            mode=TradingMode.PAPER,
            timestamp=utcnow(),
            healthy=b.ib.isConnected(),
            complete=not issues and b.settings.ibkr_paper_orders,
            positions=tuple(mapped),
            orders=tuple(orders.values()),
            fills=tuple(fills.values()),
            daily_realized_pnl_sek=realized,
            daily_unrealized_pnl_sek=unrealized,
            pnl_date=utcnow().date(),
        )

    async def submit(self, command):
        from ib_async import Contract, LimitOrder

        b = self.broker
        if not b.settings.ibkr_paper_orders or not b.verified or not b.ib.isConnected():
            raise RuntimeError("IBKR execution not enabled")
        if (
            command.action not in (Action.ENTER, Action.EXIT)
            or command.account != b.settings.ibkr_account
            or command.candidate.instrument != InstrumentType.LONG_PUT
            or len(command.contracts) != 1
            or command.contracts[0].multiplier != 100
            or command.symbol not in b.settings.allowed_symbols
        ):
            raise ValueError("unsupported native paper order")
        saved = next((r for r in self.store.intents() if r["intent_id"] == command.intent_id), None)
        if (
            not saved
            or saved["status"] != OrderState.SUBMITTING
            or OrderCommand.model_validate_json(saved["command"]) != command
        ):
            raise ValueError("durable submitting intent required")
        key = self.key("sent:" + command.intent_id)
        if self.store.get(key) is not None:
            raise RuntimeError("submission already attempted; reconcile, never replay")
        con = command.contracts[0]
        contract = Contract(
            conId=con.con_id, secType="OPT", symbol=con.symbol, exchange="SMART", currency="USD"
        )
        order_id = b.ib.client.getReqId()
        order = LimitOrder(
            "BUY" if command.action == Action.ENTER else "SELL",
            command.quantity,
            float(command.limit_usd),
            account=command.account,
            orderId=order_id,
            orderRef=command.intent_id,
            tif="DAY",
            outsideRth=False,
            transmit=True,
        )
        # This durable marker precedes the network call, including when placeOrder raises.
        self.store.set(key, {"order_id": order_id, "client_id": b.settings.ibkr_client_id})
        b.ib.placeOrder(contract, order)
        # Local enqueue is not broker acknowledgement.
        return BrokerOrder(
            intent_id=command.intent_id,
            broker_id=f"{b.settings.ibkr_client_id}:{order_id}",
            state=OrderState.SUBMITTING,
            command=command,
            filled=0,
        )

    async def cancel(self, broker_id):
        b = self.broker
        if not b.settings.ibkr_paper_orders or not b.verified or not b.ib.isConnected():
            raise RuntimeError("IBKR execution not enabled")
        client, order_id = map(int, broker_id.split(":"))
        if client != b.settings.ibkr_client_id:
            raise ValueError("foreign order cannot be cancelled")
        async with b.state_lock:
            trades = await b.ib.reqOpenOrdersAsync()
        for trade in trades:
            if (
                trade.order.orderId == order_id
                and trade.order.account == b.settings.ibkr_account
                and trade.order.clientId == client
            ):
                if trade.order.orderRef not in self.commands():
                    raise ValueError("unmanaged order")
                b.ib.cancelOrder(trade.order)
                return
        raise ValueError("order not found; reconcile")

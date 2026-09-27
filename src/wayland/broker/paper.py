"""Durable simulator, deliberately NOT a claim of IBKR paper verification."""

import uuid
from decimal import Decimal
from pathlib import Path

from ..audit import AuditStore
from ..models import (
    Action,
    BrokerOrder,
    Fill,
    OrderCommand,
    OrderState,
    PortfolioState,
    Position,
    TradingMode,
    utcnow,
)


class PaperBroker:
    def __init__(self, path: Path, account: str = "SIMULATED-PAPER"):
        self.store = AuditStore(path)
        self.account = account
        self.healthy = True
        self.fail_after_accept = False
        self.reject_next = False

    def _orders(self) -> list[BrokerOrder]:
        return [BrokerOrder.model_validate_json(value) for value in self.store.get("orders", [])]

    def _positions(self) -> list[Position]:
        return [Position.model_validate_json(value) for value in self.store.get("positions", [])]

    async def snapshot(self) -> PortfolioState:
        now = utcnow()
        return PortfolioState(
            account=self.account,
            mode=TradingMode.PAPER,
            timestamp=now,
            healthy=self.healthy,
            complete=self.healthy,
            orders=tuple(self._orders()),
            positions=tuple(self._positions()),
            fills=tuple(Fill.model_validate_json(v) for v in self.store.get("fills", [])),
            daily_realized_pnl_sek=Decimal(self.store.get("pnl:" + str(now.date()), "0")),
            pnl_date=now.date(),
        )

    async def submit(self, command: OrderCommand) -> BrokerOrder:
        if not self.healthy or command.account != self.account:
            raise ConnectionError("paper broker unavailable or wrong account")
        # The fake intentionally does not deduplicate intent_id; engine tests must catch replay.
        order = BrokerOrder(
            intent_id=command.intent_id,
            broker_id=str(uuid.uuid4()),
            command=command,
            filled=0,
            state=OrderState.REJECTED if self.reject_next else OrderState.OPEN,
        )
        self.reject_next = False
        with self.store.transaction():
            self.store.set(
                "orders", [o.model_dump_json() for o in self._orders()] + [order.model_dump_json()]
            )
            self.store.event("paper.accepted", order.model_dump(mode="json"), command.intent_id)
        if self.fail_after_accept:
            self.fail_after_accept = False
            raise TimeoutError("injected lost acknowledgement")
        return order

    async def fill(self, broker_id: str, quantity: int | None = None) -> None:
        """Explicit simulator control. No automatic fills at unrealistic live prices."""
        orders = self._orders()
        order = next(o for o in orders if o.broker_id == broker_id)
        if order.state not in (OrderState.OPEN, OrderState.PARTIAL):
            raise ValueError("order is not fillable")
        command = order.command
        remaining = command.quantity - order.filled
        quantity = remaining if quantity is None else quantity
        if quantity <= 0 or quantity > remaining:
            raise ValueError("invalid fill quantity")
        filled = order.filled + quantity
        update = order.model_copy(
            update={
                "filled": filled,
                "state": OrderState.FILLED if filled == command.quantity else OrderState.PARTIAL,
            }
        )
        positions = self._positions()
        old = next((p for p in positions if p.position_id == command.position_id), None)
        position: Position | None
        pnl = Decimal(0)
        if command.action == Action.ENTER:
            total = (old.quantity if old else 0) + quantity
            cost = (
                old.average_entry_usd * old.quantity if old else Decimal(0)
            ) + command.limit_usd * quantity
            position = Position(
                position_id=command.position_id,
                symbol=command.symbol,
                candidate=command.candidate,
                quantity=total,
                average_entry_usd=cost / total,
                max_loss_sek=(old.max_loss_sek if old else Decimal(0))
                + command.max_loss_sek * quantity / command.quantity,
                mark_usd=command.limit_usd,
                unrealized_pnl_sek=Decimal(0),
                invalidation=command.invalidation,
                invalidation_price=command.invalidation_price,
            )
        else:
            if old is None or quantity > old.quantity:
                raise ValueError("EXIT cannot create short exposure")
            pnl = (
                (command.limit_usd - old.average_entry_usd)
                * quantity
                * command.contracts[0].multiplier
                * command.usd_sek
            )
            position = (
                old.model_copy(
                    update={
                        "quantity": old.quantity - quantity,
                        "max_loss_sek": old.max_loss_sek * (old.quantity - quantity) / old.quantity,
                    }
                )
                if old.quantity > quantity
                else None
            )
        pnl -= command.fees_sek * quantity / command.quantity
        fill = Fill(
            execution_id=str(uuid.uuid4()),
            intent_id=command.intent_id,
            quantity=quantity,
            price_usd=command.limit_usd,
            timestamp=utcnow(),
        )
        with self.store.transaction():
            self.store.set(
                "orders", [(update if o.broker_id == broker_id else o).model_dump_json() for o in orders]
            )
            self.store.set(
                "positions",
                [p.model_dump_json() for p in positions if p.position_id != command.position_id]
                + ([position.model_dump_json()] if position else []),
            )
            self.store.set("fills", self.store.get("fills", []) + [fill.model_dump_json()])
            key = "pnl:" + str(fill.timestamp.date())
            self.store.set(key, str(Decimal(self.store.get(key, "0")) + pnl))
            self.store.event("paper.fill", fill.model_dump(mode="json"), command.intent_id)

    async def cancel(self, broker_id: str) -> None:
        if not self.healthy:
            raise ConnectionError("paper broker disconnected")
        orders = self._orders()
        if not any(o.broker_id == broker_id for o in orders):
            raise ValueError("unknown order")
        self.store.set(
            "orders",
            [
                (
                    o.model_copy(update={"state": OrderState.CANCELLED})
                    if o.broker_id == broker_id and o.state in (OrderState.OPEN, OrderState.PARTIAL)
                    else o
                ).model_dump_json()
                for o in orders
            ],
        )

    async def close(self) -> None:
        self.store.close()

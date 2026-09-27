import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from ib_async import Contract, Execution, OrderStatus, Trade
from ib_async import Fill as IbFill

from wayland.audit import AuditStore
from wayland.broker.ibkr import IbkrBroker
from wayland.config import Settings
from wayland.demo import fixture
from wayland.execution import ExecutionEngine
from wayland.models import OrderState, utcnow


class Gateway:
    """Real SDK objects, controllable broker boundary, no SDK/network mocking of domain logic."""

    def __init__(self):
        self.orders = []
        self.positions = []
        self.fills = []
        self.sent = 0
        self.raise_after_send = False
        self.fail_reads = False
        self.client = NS(getReqId=lambda: 42)
        self.connected = True

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    async def reqPositionsAsync(self):
        if self.fail_reads:
            raise ConnectionError
        return self.positions

    async def reqAllOpenOrdersAsync(self):
        return [t for t in self.orders if t.orderStatus.status != "Filled"]

    async def reqCompletedOrdersAsync(self, api_only):
        return [t for t in self.orders if t.orderStatus.status == "Filled"]

    async def reqExecutionsAsync(self):
        return self.fills

    async def reqOpenOrdersAsync(self):
        return await self.reqAllOpenOrdersAsync()

    def placeOrder(self, contract, order):
        self.sent += 1
        order.clientId = 37
        order.permId = 900
        trade = Trade(contract, order, OrderStatus(status="Submitted", filled=0))
        self.orders.append(trade)
        if self.raise_after_send:
            raise TimeoutError
        return trade

    def cancelOrder(self, order):
        next(t for t in self.orders if t.order == order).orderStatus.status = "Cancelled"

    def fill(self, quantity=1):
        trade = self.orders[0]
        trade.orderStatus.filled = quantity
        trade.orderStatus.status = "Filled" if quantity == trade.order.totalQuantity else "Submitted"
        execution = Execution(
            execId="fixture.1",
            acctNumber="DU123",
            orderRef=trade.order.orderRef,
            side="BOT",
            shares=quantity,
            price=2,
            time=utcnow(),
        )
        self.fills = [IbFill(trade.contract, execution, None, execution.time)]
        self.positions = [NS(account="DU123", contract=trade.contract, position=quantity)]


def session(tmp_path, gateway=None):
    ib = gateway or Gateway()
    ib.connected = True
    config = Settings(ibkr_account="DU123", account_allowlist=("DU123",), ibkr_paper_orders=True)
    broker = IbkrBroker(config, ib)
    broker.verified = True
    broker.account_pnl = AsyncMock(return_value=(Decimal(0), Decimal(0)))
    store = AuditStore(tmp_path / "native.sqlite")
    engine = ExecutionEngine(store, broker, config)
    broker.bind_store(store)
    return engine, broker, ib


def test_native_submit_fill_restart_and_broker_positions(tmp_path):
    async def run():
        engine, _broker, ib = session(tmp_path)
        market, candidate, proposal = fixture()
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "SUBMITTING"  # enqueue is NOT acknowledgement
        assert ib.orders[0].order.orderRef == result.intent_id
        assert ib.orders[0].order.tif == "DAY"
        assert await engine.reconcile()
        assert engine.portfolio.orders[0].state == OrderState.OPEN
        ib.fill()
        assert await engine.reconcile()
        assert engine.portfolio.positions[0].quantity == 1
        await engine.close()
        engine, _broker, ib = session(tmp_path, ib)
        assert await engine.reconcile()
        assert engine.portfolio.positions[0].quantity == 1
        repeated = await engine.execute(proposal, market, candidate)
        assert "duplicate" in repeated.reasons[0]
        assert ib.sent == 1
        await engine.close()

    asyncio.run(run())


def test_lost_ack_reconciles_without_second_submission(tmp_path):
    async def run():
        engine, _broker, ib = session(tmp_path)
        ib.raise_after_send = True
        market, candidate, proposal = fixture()
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "UNKNOWN"
        assert await engine.reconcile()
        assert engine.portfolio.orders[0].state == OrderState.OPEN
        await engine.execute(proposal, market, candidate)
        assert ib.sent == 1
        await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", ["foreign_position", "missing_order", "duplicate", "wrong_terms", "disconnect"]
)
def test_conflicting_broker_evidence_blocks_restart(tmp_path, fault):
    async def run():
        engine, _broker, ib = session(tmp_path)
        market, candidate, proposal = fixture()
        await engine.execute(proposal, market, candidate)
        assert await engine.reconcile()
        if fault == "foreign_position":
            ib.positions = [NS(account="DU123", contract=Contract(conId=999), position=1)]
        elif fault == "missing_order":
            ib.orders = []
        elif fault == "duplicate":
            import copy

            duplicate = copy.deepcopy(ib.orders[0])
            duplicate.order.permId += 1
            ib.orders.append(duplicate)
        elif fault == "wrong_terms":
            ib.orders[0].order.lmtPrice = 99
        else:
            ib.fail_reads = True
        assert not await engine.reconcile()
        assert engine.state == "SAFE"
        await engine.execute(fixture()[2], market, candidate)
        assert ib.sent == 1
        await engine.close()

    asyncio.run(run())


def test_cancel_requires_managed_current_client_order(tmp_path):
    async def run():
        engine, broker, _ib = session(tmp_path)
        market, candidate, proposal = fixture()
        result = await engine.execute(proposal, market, candidate)
        assert await engine.cancel(result.intent_id)
        assert engine.portfolio.orders[0].state == "CANCELLED"
        with pytest.raises(ValueError):
            await broker.cancel("99:42")
        await engine.close()

    asyncio.run(run())


def test_stale_or_missing_pnl_never_becomes_zero(tmp_path):
    async def run():
        engine, broker, ib = session(tmp_path)
        broker.account_pnl = AsyncMock(side_effect=ValueError("unavailable"))
        assert not await engine.reconcile()
        assert ib.sent == 0
        await engine.close()

    asyncio.run(run())


def test_partial_fill_and_missing_history_stay_broker_authoritative(tmp_path):
    async def run():
        engine, _broker, ib = session(tmp_path)
        market, candidate, proposal = fixture()
        proposal = proposal.model_copy(
            update={"quantity": 2, "estimated_cost_sek": Decimal(4030), "max_loss_sek": Decimal(4060)}
        )
        await engine.execute(proposal, market, candidate)
        ib.fill(1)
        assert await engine.reconcile()
        assert engine.portfolio.orders[0].state == "PARTIAL"
        assert engine.portfolio.positions[0].quantity == 1
        await engine.close()
        engine, _broker, ib = session(tmp_path, ib)
        ib.orders = []
        ib.fills = []  # truncated broker history cannot prove outstanding quantity was cancelled
        assert not await engine.reconcile()
        assert ib.sent == 1
        await engine.close()

    asyncio.run(run())


def test_crash_marker_before_network_call_never_replays(tmp_path):
    async def run():
        engine, broker, ib = session(tmp_path)
        market, candidate, proposal = fixture()

        def failed_send(*args):
            raise ConnectionError("before broker acceptance")

        ib.placeOrder = failed_send
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "UNKNOWN"
        command = broker.routing.commands()[result.intent_id]
        assert broker.routing.store.get(broker.routing.key("sent:" + result.intent_id))
        assert not await engine.reconcile()
        with pytest.raises((ValueError, RuntimeError)):
            await broker.submit(command)
        await engine.close()

    asyncio.run(run())


def test_readonly_gateway_failure_does_not_break_inspection(tmp_path):
    async def run():
        engine, broker, ib = session(tmp_path)

        def blocked(_):
            future = asyncio.get_running_loop().create_future()
            asyncio.get_running_loop().call_soon(
                broker.api_error, -1, 321, "The API interface is currently in Read-Only mode."
            )
            return future

        ib.reqCompletedOrdersAsync = blocked
        assert not await asyncio.wait_for(engine.reconcile(), 1)
        assert broker.gateway_read_only
        inspection = await broker.inspect()
        assert inspection["positions"] == []
        assert ib.isConnected()
        await engine.close()

    asyncio.run(run())


def test_option_feed_is_bounded_and_filters_out_of_range_expiries(tmp_path):
    from datetime import timedelta

    async def run():
        engine, broker, _ib = session(tmp_path)
        now = utcnow()
        market, _, _ = fixture()
        broker.option_chains = AsyncMock(
            return_value=[
                {
                    "exchange": "SMART",
                    "multiplier": "100",
                    "expirations": [(now + timedelta(days=n)).strftime("%Y%m%d") for n in (1, 30, 120)],
                    "strikes": [80, 90, 100, 110, 120, 130],
                }
            ]
        )
        broker.option_quote = AsyncMock(return_value=market.options[0])
        result = await broker.selected_options("ORCL", Decimal(105))
        assert len(result) == 3
        assert broker.option_quote.await_count == 3
        assert all(
            call.args[1] == (now + timedelta(days=30)).date() for call in broker.option_quote.await_args_list
        )
        await broker.selected_options("ORCL", Decimal(105))
        assert broker.option_chains.await_count == 1
        await engine.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["stable", "underlying", "option_cost"])
def test_repricing_preserves_model_bounds_and_rejects_changed_setup(engine, change):
    from wayland.service import WaylandService

    async def run():
        market, _candidate, proposal = fixture()
        refreshed = market.model_copy(update={"snapshot_id": "refreshed"})
        if change == "underlying":
            refreshed = refreshed.model_copy(update={"price": market.price * Decimal("1.02")})
        if change == "option_cost":
            quote = market.options[0].model_copy(update={"bid": Decimal("2.4"), "ask": Decimal("2.5")})
            refreshed = refreshed.model_copy(update={"options": (quote,)})
        engine.broker.refresh_before_execution = True
        engine.broker.market_data = AsyncMock(return_value=refreshed)
        service = WaylandService(engine, NS(analyze=AsyncMock(return_value=proposal)))
        service.detector.observe = lambda _: "test_event"
        result = await service.market_event(market)
        assert result is not None
        if change == "stable":
            assert result.state == "OPEN"
            order = (await engine.broker.snapshot()).orders[0]
            assert order.command.max_loss_sek == proposal.max_loss_sek
            assert any(e["kind"] == "proposal.revalidated" for e in engine.store.recent(100))
        else:
            assert result.state == "REJECTED"
            assert not (await engine.broker.snapshot()).orders

    asyncio.run(run())


@pytest.mark.parametrize("bad", [None, "pnl", "fx", "currency"])
def test_account_pnl_uses_broker_base_currency_and_fresh_fx(bad):
    from datetime import timedelta

    async def run():
        settings = Settings(ibkr_account="DU123", account_allowlist=("DU123",))
        ticker = NS(marketDataType=1, time=utcnow(), bid=11, ask=11)
        if bad == "fx":
            ticker.time -= timedelta(minutes=5)
        ib = NS(
            accountValues=lambda _: [
                NS(
                    tag="$LEDGER-RealCurrency",
                    currency="BASE",
                    value="UNKNOWN" if bad == "currency" else "EUR",
                )
            ],
            qualifyContractsAsync=AsyncMock(return_value=[Contract(conId=1)]),
            reqTickersAsync=AsyncMock(return_value=[ticker]),
        )
        broker = IbkrBroker(settings, ib)
        broker.pnl = NS(realizedPnL=float("nan") if bad == "pnl" else -2, unrealizedPnL=1)
        broker.pnl_time = utcnow()
        if bad:
            with pytest.raises(ValueError):
                await broker.account_pnl()
        else:
            assert await broker.account_pnl() == (Decimal(-22), Decimal(11))

    asyncio.run(run())

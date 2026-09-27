import asyncio
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from wayland.audit import AuditStore
from wayland.broker.paper import PaperBroker
from wayland.execution import ExecutionEngine
from wayland.models import OperatingState, OrderState, RiskDecision


def test_approved_duplicate_fill_and_restart(engine, sample, tmp_path, settings):
    market, candidate, proposal = sample

    async def run():
        assert engine.state == OperatingState.RECONCILING
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "OPEN"
        again = await engine.execute(proposal, market, candidate)
        assert again.intent_id == result.intent_id
        assert len((await engine.broker.snapshot()).orders) == 1
        await engine.broker.fill(result.broker_id)
        assert await engine.reconcile()
        assert engine.portfolio.positions[0].quantity == 1
        assert engine.state == OperatingState.READY

    asyncio.run(run())


def test_rejected_forged_decision_unhealthy_and_live_never_submit(engine, sample):
    market, candidate, proposal = sample

    async def run():
        bad = proposal.model_copy(update={"quantity": 100})
        result = await engine.execute(bad, market, candidate)
        assert result.state == "REJECTED"
        forged = RiskDecision(proposal_id=proposal.proposal_id, approved=True, reasons=())
        result = await engine.execute(proposal, market, candidate, forged)
        assert "risk_decision_mismatch" in result.reasons
        engine.broker.healthy = False
        assert (await engine.execute(proposal, market, candidate)).state == "REJECTED"
        assert len((await engine.broker.snapshot()).orders) == 0

    asyncio.run(run())


def test_lost_ack_reconciles_without_replay(engine, sample):
    market, candidate, proposal = sample

    async def run():
        engine.broker.fail_after_accept = True
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "UNKNOWN" and engine.state == OperatingState.SAFE
        assert await engine.reconcile()
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "OPEN"
        assert len((await engine.broker.snapshot()).orders) == 1

    asyncio.run(run())


def test_partial_fill_cancel_and_reconciliation(engine, sample):
    market, candidate, proposal = sample
    proposal = proposal.model_copy(
        update={"quantity": 2, "estimated_cost_sek": Decimal(4030), "max_loss_sek": Decimal(4060)}
    )

    async def run():
        result = await engine.execute(proposal, market, candidate)
        await engine.broker.fill(result.broker_id, 1)
        assert await engine.reconcile()
        assert engine.portfolio.orders[0].state == OrderState.PARTIAL
        assert await engine.cancel(result.intent_id)
        assert engine.portfolio.orders[0].state == OrderState.CANCELLED
        assert engine.portfolio.positions[0].quantity == 1

    asyncio.run(run())


def test_disconnect_and_position_conflict_blocks_entries(engine, sample):
    market, candidate, proposal = sample

    async def run():
        result = await engine.execute(proposal, market, candidate)
        await engine.broker.fill(result.broker_id)
        engine.broker.healthy = False
        assert not await engine.reconcile()
        engine.broker.healthy = True
        positions = engine.broker._positions()
        engine.broker.store.set(
            "positions", [positions[0].model_copy(update={"quantity": 2}).model_dump_json()]
        )
        assert not await engine.reconcile()
        assert "position_quantity_conflict" in engine.store.get("state_reasons")

    asyncio.run(run())


@pytest.mark.parametrize(
    "stage,orders,ready", [("after_intent", 0, True), ("before_broker", 0, False), ("after_broker", 1, True)]
)
def test_actual_process_death_at_submission_boundary(tmp_path, settings, stage, orders, ready):
    code = """
import asyncio, os, sys
from pathlib import Path
from wayland.audit import AuditStore
from wayland.broker.paper import PaperBroker
from wayland.config import Settings
from wayland.demo import fixture
from wayland.execution import ExecutionEngine
root=Path(sys.argv[1]); stage=sys.argv[2]
def crash(point):
    if point==stage: os._exit(91)
async def run():
    engine=ExecutionEngine(AuditStore(root/'wayland.sqlite'),PaperBroker(root/'broker.sqlite'),
                           Settings(account_allowlist=('SIMULATED-PAPER',)),fault=crash)
    market,candidate,proposal=fixture()
    await engine.execute(proposal,market,candidate)
asyncio.run(run())
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), stage], env=env, timeout=15, check=False
    )
    assert child.returncode == 91

    async def restart():
        broker = PaperBroker(tmp_path / "broker.sqlite")
        store = AuditStore(tmp_path / "wayland.sqlite")
        engine = ExecutionEngine(store, broker, settings)
        try:
            assert engine.state == OperatingState.RECONCILING
            assert await engine.reconcile() is ready
            assert len((await broker.snapshot()).orders) == orders
            assert len(store.intents()) == 1
            assert store.recent()
            if stage == "before_broker":
                assert engine.state == OperatingState.SAFE
            if stage == "after_intent":
                assert store.intents()[0]["status"] == OrderState.ABORTED
        finally:
            await engine.close()

    asyncio.run(restart())


@pytest.mark.parametrize("fill", [False, True])
def test_restart_with_open_order_or_position(tmp_path, settings, sample, fill):
    market, candidate, proposal = sample

    async def run():
        engine = ExecutionEngine(
            AuditStore(tmp_path / "wayland.sqlite"), PaperBroker(tmp_path / "broker.sqlite"), settings
        )
        result = await engine.execute(proposal, market, candidate)
        if fill:
            await engine.broker.fill(result.broker_id)
        await engine.close()
        engine = ExecutionEngine(
            AuditStore(tmp_path / "wayland.sqlite"), PaperBroker(tmp_path / "broker.sqlite"), settings
        )
        try:
            assert await engine.reconcile()
            assert engine.store.intents()[0]["broker_id"] == result.broker_id
            assert len(engine.portfolio.positions) == int(fill)
            assert len(engine.portfolio.orders) == 1
        finally:
            await engine.close()

    asyncio.run(run())


def test_single_writer_and_persistent_kill(engine, tmp_path, settings):
    second = AuditStore(tmp_path / "wayland.sqlite")
    broker = PaperBroker(tmp_path / "other.sqlite")
    try:
        with pytest.raises(RuntimeError, match="another Wayland"):
            ExecutionEngine(second, broker, settings)
        second.set("kill_switch", True)
        assert engine.store.get("kill_switch") is True
    finally:
        second.close()
        asyncio.run(broker.close())


def test_live_mode_cannot_reach_submission(engine, sample):
    from wayland.config import Settings
    from wayland.models import TradingMode

    engine.settings = Settings(mode=TradingMode.LIVE, account_allowlist=("SIMULATED-PAPER",))
    market, candidate, proposal = sample

    async def run():
        result = await engine.execute(proposal, market, candidate)
        assert result.state == "REJECTED"
        assert "live_hard_locked" in result.reasons
        assert not (await engine.broker.snapshot()).orders
        assert not engine.store.intents()

    asyncio.run(run())

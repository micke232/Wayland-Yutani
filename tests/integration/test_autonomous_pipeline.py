import asyncio
from decimal import Decimal

from wayland.agents import COORDINATOR_ID, SPECIALISTS
from wayland.autonomy import AutonomousSession
from wayland.broker.paper import PaperBroker
from wayland.config import Settings, app_root
from wayland.demo import fixture
from wayland.models import utcnow
from wayland.operator import OperatorRuntime
from wayland.orchestration import RunJournal
from wayland.orchestration_demo import FixtureSpecialistProvider
from wayland.providers.codex import CodexProvider


def test_market_event_runs_specialists_risk_order_and_exit_without_prompts(tmp_path, monkeypatch):
    runtime = OperatorRuntime(app_root(tmp_path))
    config = Settings(account_allowlist=("SIMULATED-PAPER",), openai_model="fixture-model")
    runtime.connections.publish(True, "Connected · test CLI")
    market, _, proposal = fixture()
    calls = []

    class Specialist(FixtureSpecialistProvider):
        async def analyze(self, request):
            assert request.role != COORDINATOR_ID
            calls.append(request.role)
            return await super().analyze(request)

    runtime.specialist_provider = Specialist()

    async def strategist(self, context):
        calls.append(COORDINATOR_ID)
        assert len(context["setup"]["analyst_reports"]) == 4
        return proposal.model_copy(
            update={
                "snapshot_id": context["market"]["snapshot_id"],
                "candidate_id": context["candidates"][0]["candidate_id"],
                "timestamp": utcnow(),
            }
        )

    monkeypatch.setattr(CodexProvider, "analyze", strategist)

    async def run():
        broker = PaperBroker(tmp_path / "broker.sqlite")
        session = AutonomousSession(runtime, broker, config)
        try:
            for i, price in enumerate(("100", "102", "101")):
                tick = market.model_copy(
                    update={"snapshot_id": str(i), "price": Decimal(price), "timestamp": utcnow()}
                )
                await session.process(tick)
                if i < 2:
                    assert not calls
            assert calls == [r.agent_id for r in SPECIALISTS] + [COORDINATOR_ID]
            run = RunJournal(runtime.root / "state/wayland.sqlite").read()
            assert run["trigger"] == "market_event" and run["status"] == "COMPLETED"
            assert len(run["specialists"]) == 4
            assert all(r["status"] == "COMPLETED" for r in run["specialists"].values())
            orders = (await broker.snapshot()).orders
            assert len(orders) == 1
            assert orders[0].state == "OPEN"
            await broker.fill(orders[0].broker_id)
            # A deterministic invalidation exits the verified position without another model call.
            await session.process(
                market.model_copy(
                    update={"snapshot_id": "exit", "price": Decimal(111), "timestamp": utcnow()}
                )
            )
            orders = (await broker.snapshot()).orders
            assert len(orders) == 2
            assert orders[1].command.action == "EXIT"
            await broker.fill(orders[1].broker_id)
            assert not (await broker.snapshot()).positions
            assert len(calls) == 5
            with runtime.store() as store:
                threads = store.get("ui:threads")
                assert not any(i["type"] == "userMessage" for t in threads.values() for i in t["items"])
        finally:
            await session.close()

    asyncio.run(run())


def test_unverified_broker_blocks_autonomous_analysis_and_orders(tmp_path, monkeypatch):
    runtime = OperatorRuntime(app_root(tmp_path))
    config = Settings(account_allowlist=("SIMULATED-PAPER",), openai_model="fixture-model")
    market, _, _ = fixture()
    calls = []

    async def forbidden(*args):
        calls.append(True)
        raise AssertionError("Unverified state reached model")

    monkeypatch.setattr(runtime, "model_reply", forbidden)

    class IncompleteBroker(PaperBroker):
        async def snapshot(self):
            return (await super().snapshot()).model_copy(update={"complete": False})

    async def run():
        broker = IncompleteBroker(tmp_path / "broker.sqlite")
        session = AutonomousSession(runtime, broker, config)
        try:
            for i, price in enumerate(("100", "102", "101")):
                await session.process(
                    market.model_copy(update={"snapshot_id": str(i), "price": Decimal(price)})
                )
            assert not calls
            assert not (await broker.snapshot()).orders
            assert session.engine.store.get("autonomy")["status"] == "BLOCKED"
        finally:
            await session.close()

    asyncio.run(run())


def test_position_monitoring_continues_while_analysis_is_busy(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    config = Settings(account_allowlist=("SIMULATED-PAPER",))
    market, _, _ = fixture()

    async def run():
        session = AutonomousSession(runtime, PaperBroker(tmp_path / "broker.sqlite"), config)
        gate = asyncio.Event()
        observed = []

        async def busy():
            await gate.wait()

        async def protect(snapshot):
            observed.append(snapshot.snapshot_id)

        session.service.positions.protect = protect
        session.task = asyncio.create_task(busy())
        try:
            session.offer(market)
            await session.protection_task
            assert observed == [market.snapshot_id]
            assert not session.task.done()
        finally:
            gate.set()
            await session.close()

    asyncio.run(run())

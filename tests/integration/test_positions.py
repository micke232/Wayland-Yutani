import asyncio
from decimal import Decimal

from wayland.positions import PositionManager


def test_protective_exit_survives_provider_outage_and_kill(engine, sample):
    market, candidate, proposal = sample

    async def run():
        result = await engine.execute(proposal, market, candidate)
        await engine.broker.fill(result.broker_id)
        engine.store.set("analysis_healthy", False)
        engine.store.set("kill_switch", True)
        triggered = market.model_copy(update={"price": Decimal(111)})
        results = await PositionManager(engine).protect(triggered)
        assert results[0].state == "OPEN"
        again = await PositionManager(engine).protect(triggered)
        assert len((await engine.broker.snapshot()).orders) == 2
        assert again[0].intent_id == results[0].intent_id
        await engine.broker.fill(results[0].broker_id)
        assert await engine.reconcile()
        assert not engine.portfolio.positions

    asyncio.run(run())

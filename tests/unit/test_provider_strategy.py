import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from wayland.providers.codex import CodexProvider
from wayland.strategy import SetupDetector


def test_provider_success_is_auditable(engine, sample):
    market, candidate, proposal = sample
    response = SimpleNamespace(
        id="response-fixture",
        model="fixture-version",
        status="completed",
        output_text=proposal.model_dump_json(),
        output_parsed=proposal,
    )
    client = SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=response)))
    provider = CodexProvider(
        engine.settings.model_copy(update={"openai_model": "fixture-alias"}), engine.store, client
    )
    result = asyncio.run(
        provider.analyze(
            {"market": market.model_dump(mode="json"), "candidates": [candidate.model_dump(mode="json")]}
        )
    )
    assert result == proposal
    args = client.responses.parse.call_args.kwargs
    assert args["store"] is False and "tools" not in args
    events = engine.store.recent()
    assert any("fixture-version" in e["payload"] for e in events)
    assert any("fixture-alias" in e["payload"] and "context" in e["payload"] for e in events)


def test_provider_failure_refusal_and_invalid_response_wait(engine, sample):
    market, _, _ = sample
    for outcome in (
        TimeoutError(),
        SimpleNamespace(
            id="refusal", model="fixture", status="completed", output_text="refused", output_parsed=None
        ),
    ):
        method = (
            AsyncMock(side_effect=outcome)
            if isinstance(outcome, Exception)
            else AsyncMock(return_value=outcome)
        )
        client = SimpleNamespace(responses=SimpleNamespace(parse=method))
        provider = CodexProvider(
            engine.settings.model_copy(update={"openai_model": "fixture"}), engine.store, client
        )
        assert asyncio.run(provider.analyze({"market": market.model_dump(mode="json")})) is None
        assert engine.store.get("analysis_healthy") is False


def test_setup_triggers_on_rebound_reversal_not_every_quote(engine, sample):
    market, _, _ = sample
    detector = SetupDetector(engine.settings, engine.store)
    results = []
    for index, price in enumerate(("100", "100.2", "101.5", "102", "101")):
        quote = market.model_copy(update={"price": Decimal(price), "snapshot_id": str(index)})
        results.append(detector.observe(quote))
    assert results == [None, None, None, None, "rebound_reversal"]
    assert detector.observe(quote) is None

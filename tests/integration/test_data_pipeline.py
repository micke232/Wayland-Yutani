import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from wayland.broker.diagnostics import BrokerPermissionError, api_diagnostic
from wayland.broker.ibkr import IbkrBroker
from wayland.broker.research import NativeResearch
from wayland.config import Settings, app_root
from wayland.data import DataPipeline, DataUnavailable, assess, option_costs, technical
from wayland.delegation import evidence_for
from wayland.demo import fixture
from wayland.doctor import run_doctor
from wayland.models import utcnow
from wayland.operator import OperatorRuntime


def evidence_fixture(market=None):
    market = market or fixture()[0]
    now = utcnow().isoformat()
    quote = {
        **market.model_dump(mode="json"),
        "bid": "99",
        "ask": "101",
        "last": "100",
        "spread": "2",
        "volume": "10000",
        "source": "IBKR fixture",
        "market_session": "REGULAR",
        "data_type": "live",
    }
    history = {"timestamp": now, "bars": [], "indicators": {"sma20": "100", "timestamp": now}}
    return {
        "quote": quote,
        "market": quote,
        "history": history,
        "price_history": history,
        "indicators": history["indicators"],
        "options": {"quotes": [q.model_dump(mode="json") for q in market.options]},
        "news": [{"provider": "FIXTURE", "article_id": "1", "headline": "Fixture only", "timestamp": now}],
        "candidates": option_costs(market, Settings(), utcnow()),
        "checks": {k: {"status": "OK"} for k in ("quote", "history", "options", "news")},
    }


class Source:
    def __init__(self, broker=None):
        self.qualification = {"status": "OK"}
        self.chain = {"status": "OK"}
        self.data = evidence_fixture()

    async def quote(self):
        return self.data["quote"]

    async def history(self):
        return self.data["history"]

    async def options(self):
        return self.data["options"]

    async def news(self):
        return self.data["news"]

    async def close(self):
        pass


def test_closed_ohlcv_indicator_values_and_malformed_bars():
    now = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)
    bars = [
        {
            "timestamp": now - timedelta(minutes=5 * (20 - i)),
            "open": 100,
            "high": 102,
            "low": 98,
            "close": 100,
            "volume": 10,
        }
        for i in range(20)
    ]
    result = technical(bars, Settings(), now)
    assert result["indicators"]["sma20"] == "100"
    assert Decimal(result["indicators"]["vwap"]) == 100
    assert result["indicators"]["session_low"] == "98"
    # Incomplete current candle is excluded.
    assert technical([*bars, {**bars[-1], "timestamp": now}], Settings(), now) == result
    with pytest.raises(ValueError):
        technical(bars[:19], Settings(), now)
    with pytest.raises(ValueError):
        technical([*bars, bars[-1]], Settings(), now)
    with pytest.raises(ValueError):
        technical([{**bars[0], "low": 103}, *bars[1:]], Settings(), now)


@pytest.mark.parametrize("kind", ["quote", "history", "options", "news", "fx"])
def test_stale_data_blocks_entries_at_consumption(kind):
    data = evidence_fixture()
    assert assess(data, Settings())["entry_policy"]["allowed"]
    old = (utcnow() - timedelta(days=2)).isoformat()
    if kind == "options":
        data["options"]["quotes"][0]["timestamp"] = old
    elif kind == "news":
        data["news"][0]["timestamp"] = old
    elif kind == "fx":
        data["quote"]["fx_timestamp"] = old
    else:
        data[kind]["timestamp"] = old
    out = assess(data, Settings())
    assert not out["entry_policy"]["allowed"]
    assert kind in out["entry_policy"]["missing"]


def test_partial_provider_failure_preserves_other_evidence_and_redacts():
    source = Source()
    source.history = AsyncMock(side_effect=PermissionError("secret account token"))
    result = asyncio.run(DataPipeline(source, Settings()).collect())
    assert result["checks"]["history"]["status"] == "FAIL"
    assert result["checks"]["quote"]["status"] == "OK"
    assert result["news"]
    assert result["candidates"]
    assert "secret" not in str(result)
    assert not result["entry_policy"]["allowed"]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("private"),
        PermissionError("private"),
        TimeoutError("private"),
        DataUnavailable("Empty/unsupported options chain"),
    ],
)
def test_pipeline_reports_independent_provider_errors(error):
    source = Source()
    source.options = AsyncMock(side_effect=error)
    out = asyncio.run(DataPipeline(source, Settings()).collect())
    assert out["checks"]["options"]["status"] == "FAIL"
    assert out["checks"]["news"]["status"] == "OK"
    assert out["market"]["options"] == []
    assert not out["entry_policy"]["allowed"]
    assert "private" not in str(out)


def test_delayed_and_unreferenced_news_never_enter():
    data = evidence_fixture()
    data["quote"]["live"] = False
    data["news"] = [{"headline": "Broker connection OK"}]
    result = assess(data, Settings())
    assert {"quote", "news"} <= set(result["entry_policy"]["missing"])


def test_option_costs_are_deterministic_and_include_both_leg_fees():
    market = fixture()[0]
    short = market.options[0].model_copy(
        update={
            "contract": market.options[0].contract.model_copy(update={"con_id": 1002, "strike": Decimal(95)}),
            "bid": Decimal("0.9"),
            "ask": Decimal(1),
        }
    )
    market = market.model_copy(update={"options": (*market.options, short)})
    costs = option_costs(market, Settings(), utcnow())
    long = next(c for c in costs if c["instrument"] == "LONG_PUT")
    assert Decimal(long["max_loss_sek"]) == Decimal(long["net_debit_usd"]) * 100 * market.usd_sek + 30
    spread = next(c for c in costs if c["instrument"] == "PUT_SPREAD")
    assert Decimal(spread["max_loss_sek"]) == Decimal(spread["net_debit_usd"]) * 100 * market.usd_sek + 60


def test_runtime_evidence_reaches_correct_specialist_without_peer_context(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    evidence = evidence_fixture()
    with runtime.store() as store:
        store.set("data:evidence", evidence)
    context = runtime.dispatch("snapshot")["trading"]
    market = evidence_for("wayland:market-analyst", context)
    tech = evidence_for("wayland:technical-analyst", context)
    news = evidence_for("wayland:news-analyst", context)
    options = evidence_for("wayland:options-analyst", context)
    assert market["market"]["bid"] == "99"
    assert tech["indicators"]["sma20"] == "100"
    assert news["news"][0]["article_id"] == "1"
    assert options["candidates"][0]["max_loss_sek"]
    assert "news" not in market and "candidates" not in tech and "price_history" not in options


def test_permission_diagnostics_do_not_contain_raw_sdk_payload():
    for code in (354, 10089, 10167, 162):
        item = api_diagnostic(10, code, "no permission SECRET token account password")
        assert item and item["code"] == code and "SECRET" not in str(item)


def test_completed_order_denial_from_coroutine_sdk_is_exact_and_bounded():
    async def run():
        b = IbkrBroker(Settings(), NS(isConnected=lambda: True))
        b.verified = True
        b.ib.reqPositionsAsync = AsyncMock(return_value=[])
        b.ib.reqAllOpenOrdersAsync = AsyncMock(return_value=[])
        b.ib.reqExecutionsAsync = AsyncMock(return_value=[])

        async def denied(_):
            await asyncio.sleep(0)
            b.api_error(-1, 321, "Read-Only API SECRET")
            await asyncio.sleep(60)

        b.ib.reqCompletedOrdersAsync = denied
        with pytest.raises(BrokerPermissionError) as e:
            await asyncio.wait_for(b.read_state(), 1)
        assert e.value.operation == "reqCompletedOrdersAsync"
        assert "SECRET" not in str(e.value)
        assert b.diagnostics[-1]["code"] == 321

    asyncio.run(run())


def test_empty_native_chain_and_missing_news_are_explicit():
    async def run():
        ib = NS(
            isConnected=lambda: True,
            qualifyContractsAsync=AsyncMock(return_value=[NS(conId=1)]),
            reqSecDefOptParamsAsync=AsyncMock(return_value=[]),
            reqNewsProvidersAsync=AsyncMock(return_value=[]),
        )
        broker = IbkrBroker(Settings(), ib)
        broker.verified = True
        source = NativeResearch(broker)
        with pytest.raises(DataUnavailable, match="Empty"):
            await source.options()
        with pytest.raises(DataUnavailable, match="news providers"):
            await source.news()
        assert source.chain["status"] == "FAIL"
        await source.close()

    asyncio.run(run())


@pytest.mark.parametrize("disconnected", [False, True])
def test_doctor_is_order_free_and_reports_real_vs_fixture(tmp_path, disconnected):
    calls = []

    class Broker:
        def __init__(self, config):
            self.diagnostics = []
            assert not config.ibkr_paper_orders

        async def connect(self):
            if disconnected:
                raise ConnectionError("SECRET")

        async def read_state(self, completed):
            raise BrokerPermissionError()

        async def close(self):
            calls.append("closed")

        async def submit(self, *args):
            pytest.fail("doctor placed an order")

        async def cancel(self, *args):
            pytest.fail("doctor cancelled an order")

    async def model(*args):
        return {"status": "OK", "verification": "mock model"}

    root = app_root(tmp_path)
    result = asyncio.run(
        run_doctor(root, Settings(ibkr_account="DU123", account_allowlist=("DU123",)), Broker, Source, model)
    )
    assert result["checks"]["OpenAI"]["status"] == "OK"
    assert result["checks"]["Agent orchestration"]["status"] == "OK"
    assert result["checks"]["Agent orchestration"]["verification"] == "fixture"
    assert result["checks"]["Reconciliation"]["status"] == "FAIL"
    assert result["checks"]["Operating state"]["status"] == "SAFE"
    assert result["checks"]["Live trading"]["status"] == "LOCKED"
    assert "SECRET" not in str(result)
    assert calls == ["closed"]


def test_native_quote_and_news_transform_independent_endpoints(monkeypatch):
    async def run():
        now = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)
        monkeypatch.setattr("wayland.broker.research.utcnow", lambda: now)

        async def qualify(c):
            return [NS(conId=42, secType=c.secType)]

        async def tickers(c):
            return [
                NS(
                    time=now,
                    bid=100 if c.secType == "STK" else 10,
                    ask=102 if c.secType == "STK" else 10.1,
                    last=101,
                    volume=1200,
                    marketDataType=1,
                )
            ]

        ib = NS(
            isConnected=lambda: True,
            qualifyContractsAsync=qualify,
            reqTickersAsync=tickers,
            reqContractDetailsAsync=AsyncMock(
                return_value=[
                    NS(
                        liquidSessions=lambda: [
                            NS(start=now - timedelta(hours=1), end=now + timedelta(hours=4))
                        ]
                    )
                ]
            ),
            reqNewsProvidersAsync=AsyncMock(
                return_value=[NS(code="NEWS", name="Independent news publisher")]
            ),
            reqHistoricalNewsAsync=AsyncMock(
                return_value=[
                    NS(
                        time=now.replace(tzinfo=None),
                        providerCode="NEWS",
                        articleId="article-1",
                        headline="ORCL fixture headline",
                    )
                ]
            ),
        )
        broker = IbkrBroker(Settings(), ib)
        broker.verified = True
        source = NativeResearch(broker)
        quote = await source.quote()
        assert quote["bid"] == "100" and quote["ask"] == "102" and quote["spread"] == "2"
        assert quote["last"] == "101" and quote["volume"] == "1200"
        assert quote["market_session"] == "REGULAR" and quote["live"]
        assert quote["timestamp_kind"].startswith("snapshot_receipt")
        assert Decimal(quote["usd_sek"]) == Decimal("10.05")
        news = await source.news()
        assert news[0]["provider"] == "NEWS" and news[0]["article_id"] == "article-1"
        assert news[0]["content_type"] == "headline_only"
        assert "bid" not in news[0]
        ib.reqNewsProvidersAsync.assert_awaited_once()
        await source.close()

    asyncio.run(run())


def test_native_qualification_failure_has_precise_safe_operation():
    async def run():
        ib = NS(
            isConnected=lambda: True, qualifyContractsAsync=AsyncMock(side_effect=PermissionError("SECRET"))
        )
        broker = IbkrBroker(Settings(), ib)
        broker.verified = True
        source = NativeResearch(broker)
        result = await DataPipeline(source, Settings()).collect()
        assert result["checks"]["quote"]["operation"] == "qualifyContractsAsync(ORCL)"
        assert "PermissionError" in result["checks"]["quote"]["reason"]
        assert "SECRET" not in str(result)
        await source.close()

    asyncio.run(run())


def test_unknown_session_and_missing_deterministic_cost_block():
    data = evidence_fixture()
    data["quote"]["market_session"] = "UNKNOWN"
    data["candidates"] = []
    result = assess(data, Settings())
    assert not result["entry_policy"]["allowed"]
    assert "deterministic_candidate_costs" in result["entry_policy"]["missing"]


def test_sdk_reason_attribute_is_not_logged():
    from wayland.broker.diagnostics import diagnostic

    error = RuntimeError("SECRET")
    error.reason = "SECRET"
    error.operation = "SECRET"
    assert "SECRET" not in str(diagnostic("safe operation", error))


def test_snapshot_permission_audit_records_operation(engine):
    async def run():
        engine.broker.snapshot = AsyncMock(side_effect=BrokerPermissionError())
        assert not await engine.reconcile()
        saved = engine.store.get("broker:snapshot_error")
        assert saved["operation"] == "reqCompletedOrdersAsync"
        assert saved["code"] == 321
        assert engine.state == "SAFE"
        assert any(e["kind"] == "broker.snapshot_failed" for e in engine.store.recent())

    asyncio.run(run())


def test_missing_news_blocks_new_native_analysis_but_not_reconciliation(engine):
    from wayland.service import WaylandService

    async def run():
        market = fixture()[0]
        data = evidence_fixture(market)
        data["news"] = []
        engine.store.set("data:evidence", data)
        engine.broker.refresh_before_execution = True
        provider = NS(analyze=AsyncMock())
        service = WaylandService(engine, provider)
        service.detector.observe = lambda _: "test"
        assert await service.market_event(market) is None
        provider.analyze.assert_not_awaited()
        assert engine.state == "DEGRADED"
        assert not (await engine.broker.snapshot()).orders

    asyncio.run(run())


def test_news_expiry_during_analysis_blocks_entry(engine):
    from wayland.service import WaylandService

    async def run():
        market, _, proposal = fixture()
        data = evidence_fixture(market)
        engine.store.set("data:evidence", data)
        engine.broker.refresh_before_execution = True
        engine.broker.market_data = AsyncMock(return_value=market)

        async def analyze(context):
            assert context["news"] and context["indicators"]
            data["news"] = []
            engine.store.set("data:evidence", data)
            return proposal

        service = WaylandService(engine, NS(analyze=analyze))
        service.detector.observe = lambda _: "test"
        result = await service.market_event(market)
        assert result.state == "REJECTED"
        assert result.reasons == ("analytical_data_unavailable",)
        assert not (await engine.broker.snapshot()).orders

    asyncio.run(run())


def test_cached_feed_keeps_original_timestamps_and_does_not_refresh_evidence_age():
    async def run():
        source = Source()
        cache = {}
        source.news = AsyncMock(return_value=source.data["news"])
        first = await DataPipeline(source, Settings(), cache).collect()
        second = await DataPipeline(source, Settings(), cache).collect()
        source.news.assert_awaited_once()
        assert second["checks"]["news"]["cached"]
        assert second["news"][0]["timestamp"] == first["news"][0]["timestamp"]
        assert not assess(second, Settings(), utcnow() + timedelta(days=2))["entry_policy"]["allowed"]

    asyncio.run(run())


def test_account_inspection_failure_does_not_cancel_data_collection(tmp_path):
    from wayland.broker.observer import BrokerObserver

    runtime = OperatorRuntime(app_root(tmp_path))
    broker = NS(
        ib=NS(isConnected=lambda: True),
        inspect=AsyncMock(side_effect=PermissionError("SECRET")),
        market_data=AsyncMock(side_effect=TimeoutError()),
    )
    asyncio.run(BrokerObserver(runtime).sample(broker, runtime.settings()))
    observation = runtime.dispatch("snapshot")["trading"]["brokerObservation"]
    assert observation["connected"]
    assert not observation["execution_ready"]
    assert observation["inspection"]["verified"] is False
    assert "SECRET" not in str(observation)

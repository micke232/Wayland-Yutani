"""Timestamped analytical evidence, deterministic indicators and fail-closed data policy."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from .broker.diagnostics import diagnostic
from .market import candidates, fresh, price_candidate
from .models import MarketSnapshot, utcnow


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware provider timestamp required")
    return value.astimezone(UTC)


def number(value, positive=False):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("Invalid provider numeric value")
    return result


def technical(bars, settings, now):
    """Closed 5-minute TRADES bars; never estimate indicators in an LLM."""
    normalized = []
    for bar in bars:
        time = stamp(bar["timestamp"])
        if time + timedelta(minutes=5) > now:
            continue  # Exclude the forming bar and future bars.
        o, h, l, c = (number(bar[k], True) for k in ("open", "high", "low", "close"))
        v = number(bar["volume"])
        if not l <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid OHLC bounds")
        normalized.append(
            {
                "timestamp": time.isoformat(),
                "open": str(o),
                "high": str(h),
                "low": str(l),
                "close": str(c),
                "volume": str(v),
            }
        )
    if not normalized or [b["timestamp"] for b in normalized] != sorted({b["timestamp"] for b in normalized}):
        raise ValueError("Empty, duplicate or unordered OHLCV")
    # Intraday strategy: do not pool prior sessions into current-session indicators.
    from zoneinfo import ZoneInfo

    day = stamp(normalized[-1]["timestamp"]).astimezone(ZoneInfo("America/New_York")).date()
    current = [
        b for b in normalized if stamp(b["timestamp"]).astimezone(ZoneInfo("America/New_York")).date() == day
    ]
    if len(current) < 20:
        raise ValueError("At least 20 closed 5-minute bars from the latest session required")
    if any(
        (stamp(b["timestamp"]) - stamp(a["timestamp"])).total_seconds() != 300 for a, b in pairwise(current)
    ):
        raise ValueError("Gap in closed intraday five-minute bars")
    closes = [Decimal(b["close"]) for b in current]
    low = min(Decimal(b["low"]) for b in current)
    high = max(Decimal(b["high"]) for b in current)
    volume = sum(Decimal(b["volume"]) for b in current)
    vwap = (
        sum(
            (Decimal(b["high"]) + Decimal(b["low"]) + Decimal(b["close"])) / 3 * Decimal(b["volume"])
            for b in current
        )
        / volume
        if volume
        else None
    )
    last = stamp(current[-1]["timestamp"]) + timedelta(minutes=5)
    return {
        "source": "IBKR historical TRADES",
        "timeframe": "5 mins",
        "use_rth": True,
        "timestamp": last.isoformat(),
        "session_date": str(day),
        "bars": normalized,
        "indicators": {
            "timestamp": last.isoformat(),
            "method": "closed intraday bars; SMA20, typical-price volume-weighted VWAP",
            "sma20": str(sum(closes[-20:]) / 20),
            "vwap": str(vwap) if vwap is not None else None,
            "session_low": str(low),
            "session_high": str(high),
            "rebound_fraction": str(closes[-1] / low - 1),
            "drawdown_from_high": str(1 - closes[-1] / high),
            "rebound_threshold": str(settings.rebound_fraction),
            "reversal_threshold": str(settings.reversal_fraction),
        },
    }


def option_costs(snapshot, settings, now):
    result = []
    for c in candidates(snapshot, settings, now):
        debit, quotes = price_candidate(c, snapshot, settings, now)
        fees = settings.fee_per_contract_sek * len(quotes) * 2
        loss = debit * quotes[0].contract.multiplier * snapshot.usd_sek + fees
        result.append(
            {
                **c.model_dump(mode="json"),
                "quantity": 1,
                "net_debit_usd": str(debit),
                "max_loss_sek": str(loss),
                "estimated_entry_cost_sek": str(
                    debit * quotes[0].contract.multiplier * snapshot.usd_sek + fees / 2
                ),
                "round_trip_fee_bound_sek": str(fees),
                "usd_sek": str(snapshot.usd_sek),
                "fx_timestamp": snapshot.fx_timestamp.isoformat(),
                "timestamp": snapshot.timestamp.isoformat(),
                "pricing": "long ask minus short bid; per one contract/spread",
            }
        )
    return result


class DataPipeline:
    """Independent provider failures never erase other providers' successful data."""

    def __init__(self, source, settings, cache=None):
        self.source, self.settings = source, settings
        self.cache = cache if cache is not None else {}

    async def collect(self):
        result: dict[str, Any] = {
            "collected_at": utcnow().isoformat(),
            "checks": {},
            "news": [],
            "candidates": [],
        }

        async def fetch(key, method):
            import copy

            cached = self.cache.get(key)
            if key in ("history", "news") and cached and (utcnow() - cached[0]).total_seconds() < 60:
                result[key] = copy.deepcopy(cached[1])
                result["checks"][key] = {
                    "status": "OK",
                    "operation": key,
                    "timestamp": cached[0].isoformat(),
                    "cached": True,
                }
                return
            try:
                value = await asyncio.wait_for(method(), 25)
                result[key] = value
                if key in ("history", "news"):
                    self.cache[key] = (utcnow(), copy.deepcopy(value))
                result["checks"][key] = {"status": "OK", "operation": key, "timestamp": utcnow().isoformat()}
            except Exception as error:  # noqa: BLE001 -- independent provider failure, sanitized diagnostics
                result["checks"][key] = {"status": "FAIL", **diagnostic(key, error)}
                # A known, fixed source validation message is safe. Raw SDK text is never included.
                if isinstance(error, DataUnavailable):
                    result["checks"][key]["reason"] = error.reason

        await asyncio.gather(
            *(fetch(k, getattr(self.source, k)) for k in ("quote", "history", "options", "news"))
        )
        now = utcnow()
        quote = result.get("quote")
        if quote:
            result["market"] = dict(quote)
            result["market"]["options"] = result.get("options", {}).get("quotes", [])
            try:
                snapshot = MarketSnapshot.model_validate_json(
                    json.dumps(
                        {
                            k: result["market"][k]
                            for k in (
                                "snapshot_id",
                                "symbol",
                                "timestamp",
                                "price",
                                "usd_sek",
                                "fx_timestamp",
                                "live",
                                "options",
                            )
                        }
                    )
                )
                if not snapshot.live or not fresh(
                    snapshot.fx_timestamp, now, self.settings.max_fx_age_seconds
                ):
                    raise ValueError("Unusable FX/live quote")
                result["candidates"] = option_costs(snapshot, self.settings, now)
            except (KeyError, ValueError):
                result["candidates"] = []
        if not quote and result.get("options"):
            result["market"] = {"symbol": "ORCL", "source": "IBKR", "options": result["options"]["quotes"]}
        if result.get("history"):
            result["price_history"] = result["history"]
            result["indicators"] = result["history"]["indicators"]
        return assess(result, self.settings, now)


class DataUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def assess(data, settings, now=None):
    """Re-evaluate cache ages at consumption time, not just at collection time."""
    import copy

    data = copy.deepcopy(data)
    now = now or utcnow()
    checks = data.setdefault("checks", {})
    for key, maximum, field in [
        ("quote", settings.max_market_data_age_seconds, "timestamp"),
        ("history", 900, "timestamp"),
    ]:
        value = data.get(key)
        if value:
            age = (now - stamp(value[field])).total_seconds()
            checks[key]["age_seconds"] = round(age, 2)
            if not 0 <= age <= maximum:
                checks[key].update(status="FAIL", reason=f"Stale {key}: age {age:.1f}s; maximum {maximum}s")
    quote = data.get("quote") or {}
    if quote and quote.get("connected") is False:
        checks["quote"].update(status="FAIL", reason="Broker disconnected during quote collection")
    if quote and quote.get("market_session") != "REGULAR":
        checks["quote"].update(
            status="FAIL", reason="Outside verified regular market session (or session unknown)"
        )
    if quote and not quote.get("live"):
        checks["quote"].update(status="FAIL", reason="Delayed/frozen quote; not eligible for ENTER")
    if quote and (
        not quote.get("fx_timestamp")
        or not fresh(stamp(quote["fx_timestamp"]), now, settings.max_fx_age_seconds)
    ):
        checks["fx"] = {"status": "FAIL", "reason": "Fresh live USD/SEK quote unavailable"}
    else:
        checks["fx"] = {"status": "OK" if quote else "FAIL", "reason": "USD/SEK conversion"}
    options = data.get("options", {})
    quotes = options.get("quotes", [])
    if not quotes:
        checks.setdefault("options", {}).update(
            status="FAIL", reason=checks.get("options", {}).get("reason", "No qualified option quotes")
        )
    elif any(
        not q.get("live") or not fresh(stamp(q["timestamp"]), now, settings.max_market_data_age_seconds)
        for q in quotes
    ):
        checks["options"].update(status="FAIL", reason="Stale or delayed option quotes")
    news = data.get("news") or []
    # Headline feed is separate evidence, with provider code, article reference and publication time.
    if not news or any(
        not n.get("provider")
        or not n.get("article_id")
        or not n.get("headline")
        or not fresh(stamp(n["timestamp"]), now, 86400)
        for n in news
    ):
        checks.setdefault("news", {}).update(
            status="FAIL", reason=checks.get("news", {}).get("reason", "No sourced news in the last 24 hours")
        )
    failures = [
        k for k in ("quote", "fx", "history", "options", "news") if checks.get(k, {}).get("status") != "OK"
    ]
    if not data.get("candidates"):
        failures.append("deterministic_candidate_costs")
    data["entry_policy"] = {
        "allowed": not failures and bool(data.get("candidates")),
        "missing": failures,
        "policy": "Require fresh broker/FX/options, closed intraday history and sourced news for new AI ENTER; exits remain risk-gated",
    }
    return data

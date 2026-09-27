"""Deterministic contract quality, candidate construction and debit bounds."""

from datetime import datetime
from decimal import Decimal

from .config import Settings
from .models import Candidate, InstrumentType, MarketSnapshot, OptionQuote


def fresh(timestamp: datetime, now: datetime, maximum: int) -> bool:
    return 0 <= (now - timestamp).total_seconds() <= maximum


def price_candidate(
    candidate: Candidate,
    snapshot: MarketSnapshot,
    settings: Settings,
    now: datetime,
    exit_order: bool = False,
) -> tuple[Decimal, tuple[OptionQuote, ...]]:
    by_id = {q.contract.con_id: q for q in snapshot.options}
    ids = [candidate.long_con_id] + ([candidate.short_con_id] if candidate.short_con_id else [])
    if any(key not in by_id for key in ids):
        raise ValueError("option_quote_missing")
    quotes = tuple(by_id[key] for key in ids)
    for q in quotes:
        c = q.contract
        dte = (c.expiry - now.date()).days
        if c.symbol != candidate.symbol or c.symbol != snapshot.symbol:
            raise ValueError("contract_symbol_mismatch")
        if c.multiplier != 100 or c.currency != "USD":
            raise ValueError("unsupported_contract_terms")
        if not q.live or not fresh(q.timestamp, now, settings.max_market_data_age_seconds):
            raise ValueError("stale_or_delayed_option_quote")
        if dte < 1:
            raise ValueError("expired_option")
        if not exit_order:
            if not settings.min_dte <= dte <= settings.max_dte:
                raise ValueError("dte_outside_limits")
            if (q.ask - q.bid) / q.ask > settings.max_bid_ask_fraction:
                raise ValueError("option_spread_too_wide")
            if q.volume is not None and q.volume < settings.min_volume:
                raise ValueError("option_volume_too_low")
            if q.open_interest is not None and q.open_interest < settings.min_open_interest:
                raise ValueError("option_open_interest_too_low")
    long = quotes[0]
    if candidate.instrument == InstrumentType.PUT_SPREAD:
        short = quotes[1]
        if (
            long.contract.expiry != short.contract.expiry
            or long.contract.strike <= short.contract.strike
            or long.contract.multiplier != short.contract.multiplier
        ):
            raise ValueError("invalid_debit_put_spread")
        value = long.bid - short.ask if exit_order else long.ask - short.bid
        if value >= long.contract.strike - short.contract.strike:
            raise ValueError("spread_debit_exceeds_width")
    else:
        value = long.bid if exit_order else long.ask
    if value <= 0:
        raise ValueError("no_executable_positive_limit")
    return value, quotes


def candidates(snapshot: MarketSnapshot, settings: Settings, now: datetime) -> tuple[Candidate, ...]:
    found = []
    for long in snapshot.options:
        options = [
            Candidate(
                candidate_id=f"put:{long.contract.con_id}",
                symbol=snapshot.symbol,
                instrument=InstrumentType.LONG_PUT,
                long_con_id=long.contract.con_id,
            )
        ]
        for short in snapshot.options:
            if long.contract.strike > short.contract.strike and long.contract.expiry == short.contract.expiry:
                options.append(
                    Candidate(
                        candidate_id=f"spread:{long.contract.con_id}:{short.contract.con_id}",
                        symbol=snapshot.symbol,
                        instrument=InstrumentType.PUT_SPREAD,
                        long_con_id=long.contract.con_id,
                        short_con_id=short.contract.con_id,
                    )
                )
        for candidate in options:
            try:
                price_candidate(candidate, snapshot, settings, now)
            except ValueError:
                continue
            found.append(candidate)
    return tuple(found)

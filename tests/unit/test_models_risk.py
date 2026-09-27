import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wayland.config import Settings
from wayland.market import candidates, price_candidate
from wayland.models import (
    Action,
    Direction,
    InstrumentType,
    OperatingState,
    Position,
    TradeProposal,
    TradingMode,
    utcnow,
)
from wayland.risk import RiskEngine


def test_enter_requires_instrument_quantity_loss(sample):
    _, _, proposal = sample
    for patch in (
        {"quantity": 0},
        {"instrument": None},
        {"max_loss_sek": None},
        {"estimated_cost_sek": None},
        {"candidate_id": None},
        {"quantity": -1},
    ):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate_json(proposal.model_copy(update=patch).model_dump_json())


def test_strict_unknown_and_nonfinite(sample):
    _, _, proposal = sample
    raw = proposal.model_dump_json()[:-1] + ',"execute_shell":"no"}'
    with pytest.raises(ValidationError):
        TradeProposal.model_validate_json(raw)
    with pytest.raises(ValidationError):
        Settings(max_position_sek=Decimal("NaN"))


def evaluate(
    engine,
    sample,
    *,
    portfolio_patch=None,
    proposal_patch=None,
    market_patch=None,
    config_patch=None,
    state=OperatingState.READY,
    kill=False,
    entries=0,
):
    market, candidate, proposal = sample
    portfolio = asyncio.run(engine.broker.snapshot())
    portfolio = portfolio.model_copy(update=portfolio_patch or {})
    return RiskEngine(engine.settings.model_copy(update=config_patch or {})).evaluate(
        proposal.model_copy(update=proposal_patch or {}),
        market.model_copy(update=market_patch or {}),
        portfolio,
        candidate,
        state,
        kill,
        entries,
        utcnow(),
    )


def test_good_proposal_and_deterministic_amounts(engine, sample):
    result = evaluate(engine, sample)
    assert result.approved
    assert result.command.max_loss_sek == Decimal(2030)
    assert result.command.limit_usd == Decimal(2)


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"proposal_patch": {"symbol": "OTHER"}}, "symbol_not_allowed"),
        ({"proposal_patch": {"instrument": InstrumentType.STOCK}}, "instrument_not_allowed"),
        ({"proposal_patch": {"quantity": 100}}, "position_cost_or_loss_limit"),
        ({"proposal_patch": {"max_loss_sek": Decimal(1)}}, "model_understated_loss"),
        ({"market_patch": {"connected": False}}, "market_stale_disconnected_or_delayed"),
        ({"market_patch": {"live": False}}, "market_stale_disconnected_or_delayed"),
        (
            {"market_patch": {"timestamp": utcnow() - timedelta(minutes=1)}},
            "market_stale_disconnected_or_delayed",
        ),
        ({"market_patch": {"fx_timestamp": utcnow() - timedelta(minutes=5)}}, "stale_fx"),
        (
            {"market_patch": {"timestamp": utcnow() + timedelta(days=1)}},
            "market_stale_disconnected_or_delayed",
        ),
        ({"portfolio_patch": {"healthy": False}}, "broker_unhealthy_or_incomplete"),
        ({"portfolio_patch": {"account": "NOT-ALLOWED"}}, "account_not_allowlisted"),
        ({"portfolio_patch": {"mode": TradingMode.LIVE}}, "live_hard_locked"),
        ({"config_patch": {"mode": TradingMode.LIVE, "live_enabled": True}}, "live_hard_locked"),
        ({"portfolio_patch": {"daily_realized_pnl_sek": Decimal(-2000)}}, "daily_loss_limit"),
        ({"portfolio_patch": {"daily_unrealized_pnl_sek": Decimal(-2000)}}, "daily_loss_limit"),
        ({"entries": 3}, "daily_trade_limit"),
        ({"kill": True}, "kill_switch"),
        ({"state": OperatingState.DEGRADED}, "entries_not_ready"),
        ({"state": OperatingState.SAFE}, "entries_not_ready"),
        ({"state": OperatingState.RECONCILING}, "entries_not_ready"),
    ],
)
def test_entry_rejections(engine, sample, kwargs, reason):
    result = evaluate(engine, sample, **kwargs)
    assert not result.approved and reason in result.reasons


def test_open_position_limit(engine, sample):
    _, candidate, _ = sample
    position = Position(
        position_id="p",
        symbol="ORCL",
        candidate=candidate,
        quantity=1,
        average_entry_usd=Decimal(2),
        max_loss_sek=Decimal(2030),
    )
    result = evaluate(engine, sample, portfolio_patch={"positions": (position,)})
    assert "open_position_limit" in result.reasons


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"bid": Decimal(".1")}, "option_spread_too_wide"),
        ({"volume": 0}, "option_volume_too_low"),
        ({"open_interest": 0}, "option_open_interest_too_low"),
        ({"timestamp": utcnow() - timedelta(minutes=2)}, "stale_or_delayed_option_quote"),
        ({"live": False}, "stale_or_delayed_option_quote"),
    ],
)
def test_option_quality(sample, settings, patch, reason):
    market, candidate, _ = sample
    market = market.model_copy(update={"options": (market.options[0].model_copy(update=patch),)})
    with pytest.raises(ValueError, match=reason):
        price_candidate(candidate, market, settings, utcnow())


def test_optional_volume_and_spread_shape(sample, settings):
    market, _, _ = sample
    long = market.options[0].model_copy(update={"volume": None, "open_interest": None})
    short_contract = long.contract.model_copy(update={"con_id": 1002, "strike": Decimal(95)})
    short = long.model_copy(update={"contract": short_contract, "bid": Decimal(".9"), "ask": Decimal(1)})
    market = market.model_copy(update={"options": (long, short)})
    spread = next(
        c for c in candidates(market, settings, utcnow()) if c.instrument == InstrumentType.PUT_SPREAD
    )
    price, _ = price_candidate(spread, market, settings, utcnow())
    assert price == Decimal("1.1")
    invalid = spread.model_copy(update={"long_con_id": 1002, "short_con_id": 1001})
    with pytest.raises(ValueError, match="invalid_debit_put_spread"):
        price_candidate(invalid, market, settings, utcnow())


def test_exit_ignores_entry_limits_but_not_position_size(engine, sample):
    _market, candidate, _proposal = sample
    position = Position(
        position_id="p",
        symbol="ORCL",
        candidate=candidate,
        quantity=1,
        average_entry_usd=Decimal(2),
        max_loss_sek=Decimal(2030),
    )
    patch = {"action": Action.EXIT, "position_id": "p", "direction": Direction.NEUTRAL}
    result = evaluate(
        engine,
        sample,
        portfolio_patch={"positions": (position,), "daily_realized_pnl_sek": Decimal(-5000)},
        proposal_patch=patch,
        state=OperatingState.SAFE,
        kill=True,
        entries=99,
        config_patch={"allowed_symbols": ()},
    )
    assert result.approved
    rejected = evaluate(
        engine, sample, portfolio_patch={"positions": (position,)}, proposal_patch={**patch, "quantity": 2}
    )
    assert "exit_exceeds_position" in rejected.reasons

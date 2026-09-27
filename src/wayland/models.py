"""Validated immutable domain data. Money uses Decimal, timestamps use UTC-aware values."""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Action(StrEnum):
    WAIT = "WAIT"
    ENTER = "ENTER"
    EXIT = "EXIT"


class Direction(StrEnum):
    BEARISH = "BEARISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"


class InstrumentType(StrEnum):
    STOCK = "STOCK"
    LONG_PUT = "LONG_PUT"
    PUT_SPREAD = "PUT_SPREAD"


class OperatingState(StrEnum):
    RECONCILING = "RECONCILING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    SAFE = "SAFE"


class OrderState(StrEnum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ABORTED = "ABORTED"


TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.ABORTED}


class OptionContract(Model):
    con_id: int = Field(gt=0)
    symbol: str
    right: Literal["P"] = "P"
    strike: Positive
    expiry: date
    multiplier: int = Field(gt=0)
    currency: Literal["USD"] = "USD"
    exchange: str = "SMART"


class OptionQuote(Model):
    contract: OptionContract
    bid: Money
    ask: Positive
    timestamp: AwareDatetime
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    live: bool = True

    @model_validator(mode="after")
    def uncrossed(self):
        if self.ask < self.bid:
            raise ValueError("crossed option quote")
        return self


class MarketSnapshot(Model):
    snapshot_id: str = Field(min_length=1)
    symbol: str
    timestamp: AwareDatetime
    price: Positive
    usd_sek: Positive
    fx_timestamp: AwareDatetime
    connected: bool = True
    live: bool = True
    options: tuple[OptionQuote, ...] = ()

    @model_validator(mode="after")
    def unique_contracts(self):
        ids = [quote.contract.con_id for quote in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate option contract")
        return self


class Candidate(Model):
    candidate_id: str
    symbol: str
    instrument: InstrumentType
    long_con_id: int = Field(gt=0)
    short_con_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def bounded_shape(self):
        if self.instrument not in (InstrumentType.LONG_PUT, InstrumentType.PUT_SPREAD):
            raise ValueError("unsupported candidate")
        if (self.instrument == InstrumentType.PUT_SPREAD) != (self.short_con_id is not None):
            raise ValueError("put spread requires exactly two legs")
        if self.long_con_id == self.short_con_id:
            raise ValueError("spread legs must differ")
        return self


class TradeProposal(Model):
    proposal_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    symbol: str
    action: Action
    direction: Direction
    instrument: InstrumentType | None
    candidate_id: str | None
    position_id: str | None
    quantity: int = Field(ge=0)
    estimated_cost_sek: Money | None
    max_loss_sek: Money | None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    thesis: str = Field(max_length=4000)
    invalidation: str = Field(max_length=2000)
    invalidation_price: Positive | None

    @model_validator(mode="after")
    def executable_shape(self):
        if self.action == Action.ENTER and (
            self.instrument is None
            or not self.candidate_id
            or self.quantity <= 0
            or self.max_loss_sek is None
            or self.estimated_cost_sek is None
        ):
            raise ValueError("ENTER requires instrument, candidate, positive quantity and bounded loss/cost")
        if self.action == Action.EXIT and (not self.position_id or self.quantity <= 0):
            raise ValueError("EXIT requires existing position and positive quantity")
        if self.action == Action.WAIT and self.quantity != 0:
            raise ValueError("WAIT cannot carry quantity")
        return self


class OrderCommand(Model):
    intent_id: str
    account: str
    symbol: str
    action: Action
    candidate: Candidate
    contracts: tuple[OptionContract, ...]
    quantity: int = Field(gt=0)
    limit_usd: Positive
    usd_sek: Positive
    max_loss_sek: Money
    fees_sek: Money
    position_id: str
    invalidation: str = ""
    invalidation_price: Positive | None = None


class BrokerOrder(Model):
    intent_id: str
    broker_id: str
    state: OrderState
    command: OrderCommand
    filled: int = Field(ge=0)


class Fill(Model):
    execution_id: str
    intent_id: str
    quantity: int = Field(gt=0)
    price_usd: Positive
    timestamp: AwareDatetime


class Position(Model):
    position_id: str
    symbol: str
    candidate: Candidate
    quantity: int = Field(gt=0)
    average_entry_usd: Positive
    max_loss_sek: Money
    mark_usd: Money | None = None
    unrealized_pnl_sek: Decimal | None = Field(default=None, allow_inf_nan=False)
    invalidation: str = ""
    invalidation_price: Positive | None = None


class PortfolioState(Model):
    account: str
    mode: TradingMode
    timestamp: AwareDatetime
    healthy: bool
    complete: bool
    positions: tuple[Position, ...] = ()
    orders: tuple[BrokerOrder, ...] = ()
    fills: tuple[Fill, ...] = ()
    daily_realized_pnl_sek: Decimal = Field(default=Decimal(0), allow_inf_nan=False)
    daily_unrealized_pnl_sek: Decimal = Field(default=Decimal(0), allow_inf_nan=False)
    pnl_date: date


class RiskDecision(Model):
    proposal_id: str
    approved: bool
    reasons: tuple[str, ...]
    command: OrderCommand | None = None


class ExecutionResult(Model):
    intent_id: str | None
    state: str
    reasons: tuple[str, ...] = ()
    broker_id: str | None = None

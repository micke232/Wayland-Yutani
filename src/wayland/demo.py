"""Explicit synthetic fixtures; no market data or trading claims."""

import uuid
from datetime import timedelta
from decimal import Decimal

from .models import (
    Action,
    Candidate,
    Direction,
    InstrumentType,
    MarketSnapshot,
    OptionContract,
    OptionQuote,
    TradeProposal,
    utcnow,
)


def fixture():
    now = utcnow()
    contract = OptionContract(
        con_id=1001,
        symbol="ORCL",
        strike=Decimal(100),
        expiry=(now + timedelta(days=30)).date(),
        multiplier=100,
    )
    quote = OptionQuote(
        contract=contract, bid=Decimal("1.90"), ask=Decimal(2), timestamp=now, volume=100, open_interest=1000
    )
    market = MarketSnapshot(
        snapshot_id=str(uuid.uuid4()),
        symbol="ORCL",
        timestamp=now,
        price=Decimal(105),
        usd_sek=Decimal(10),
        fx_timestamp=now,
        options=(quote,),
    )
    candidate = Candidate(
        candidate_id="put:1001", symbol="ORCL", instrument=InstrumentType.LONG_PUT, long_con_id=1001
    )
    proposal = TradeProposal(
        proposal_id=str(uuid.uuid4()),
        snapshot_id=market.snapshot_id,
        timestamp=now,
        symbol="ORCL",
        action=Action.ENTER,
        direction=Direction.BEARISH,
        instrument=InstrumentType.LONG_PUT,
        candidate_id=candidate.candidate_id,
        position_id=None,
        quantity=1,
        estimated_cost_sek=Decimal(2015),
        max_loss_sek=Decimal(2030),
        confidence=0.7,
        thesis="SYNTHETIC DEMO ONLY",
        invalidation="Synthetic underlying > 110",
        invalidation_price=Decimal(110),
    )
    return market, candidate, proposal

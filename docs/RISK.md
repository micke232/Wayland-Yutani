# Deterministic risk contract

Defaults: ORCL only; long puts and debit put spreads; SEK 10,000 maximum position
cost/loss; SEK 2,000 daily realized plus unrealized loss threshold; three entries
per UTC day; one open position including reserved entry orders; market and option
quotes at most ten seconds old; FX at most sixty seconds old. Future timestamps,
delayed prices and disconnected sources are rejected. No profitability claim is
made by this research setup.

All defaults are validated configuration. Account allowlist is empty until the
operator supplies it. No single flag or model output enables LIVE. The execution
engine and IBKR adapter enforce separate gates. Protective exits cannot unlock
live access either.

## Options and money

Prices and monetary arithmetic use Decimal. Qualified contract IDs, put right,
underlying symbol, USD currency, standard multiplier 100, matching spread expiry
and correct strike ordering are required. Non-standard adjusted options are not
yet supported. DTE bounds default to 7–90 for entries. Bid/ask cannot be crossed;
relative spread is capped at 20%; volume/open interest minima apply when those
fields exist. Their absence is explicit, not interpreted as positive liquidity.
Contract terms, quotes and maximum loss are mandatory.

Long-put entry limit uses ask; spread entry uses long ask minus short bid.
Conservative debit plus entry/exit fee reserves determines maximum loss in SEK.
Fee reserve defaults to SEK 15 per contract per side **for simulation** and must
be calibrated against broker commissions. FX is required and independently aged.
Model estimates cannot reduce computed cost/loss. Daily loss reflects broker P&L;
simulated marks are not live valuations.

The daily loss threshold blocks new entries after observed losses reach it. It
is not a guarantee losses cannot exceed that amount. Option exercise, assignment,
market gaps and actual execution costs require specific handling before real
paper execution. The simulator fills spread units atomically; it does not validate
real combo legging/assignment risk.

## Entries versus exits

Entries require READY, healthy complete broker state, account allowlist, fresh
market/FX/options data, matching snapshot/candidate, allowed instrument/direction,
position/trade/day limits and kill off. Pending entry orders reserve a slot.
Cancelled entry attempts conservatively count towards the daily trade cap.
Unknown outcomes prevent progress until authoritative reconciliation.

Exits derive the instrument and available quantity from the verified broker
position. They cannot create or enlarge shorts and cannot overlap a pending order
on that position. Entry symbol/trade/loss/kill limits do not prevent reductions.
Fresh valid pricing is still required; exits are not blind market orders.

PositionManager can emit a deterministic EXIT when the stored bearish
invalidation price is crossed. This remains available during model outage.
Model-written free-text invalidation conditions are audited but never executed
as code. The numeric invalidation threshold is checked only against fresh input.

## Kill and recovery

Kill is durable. Restart cannot clear it. Unknown submissions and broker conflicts
have no automatic retry/reset shortcut. Investigate broker truth and journal
correlation; do not delete the DB to restore READY. LIVE readiness is a separate,
explicitly approved milestone after prolonged paper verification.

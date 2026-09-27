# Delivery and verification roadmap

## A / A2: foundation before intelligence

Implemented source: typed models/config, independent registry/state, durable audit,
single execution owner, separate persistent fake broker, deterministic risk,
intent lifecycle, startup RECONCILING, authoritative order/fill/position comparison,
unknown-outcome blocking, partial fill/cancel/exit, CLI and failure-injection cases.
Execution results and check outcomes are recorded in VERIFICATION.md; source
existence alone is not a claim of passing verification.

## B / C: market and option data

Read-only IBKR connection and account guards, qualified ORCL/USDSEK snapshots,
option-chain metadata and qualified option quotes are implemented behind an optional
adapter. Deterministic candidates and quality gates exist. Real Gateway login,
market permissions, timestamps, reconnect coverage and option data remain unverified.

## D: analysis

Setup state machine and cooldown are persisted. Structured OpenAI analysis and
an audit chain are implemented without execution tools. Real API/model compatibility
and timing/cost behavior require explicit environment verification. No real model
call has been made by writing the provider. A stale response is rejected, not repriced.
News is explicitly supplied context; no news feed or news freshness policy is
currently integrated. Thus the full autonomous strategy remains incomplete.

## E: paper execution

Simulator implementation includes durable order intent, partial fills, cancellation,
reconciliation and positions. **IBKR submit/cancel remain disabled.** Next priorities:
complete account/fill-history coverage, durable broker event ingestion, orderRef /
permId mapping, duplicate/correction handling, combo leg/assignment exposure and
failure injection at real paper connection boundaries. Never infer native broker
idempotency from application intent IDs.

## F: service/operator

CLI status/audit/kill, monitor loop, heartbeat, signal handling, operating states
and deployment documentation exist. A continuous quote/news event source and
richer operator TUI are future work. No public endpoints or infrastructure deployment.

## G: paper soak (not performed)

Run only after B–F real paper prerequisites pass. Record duration/session hours,
accounts/modes (redacted in shared reports), data permissions, reconnect events,
intent/order/fill counts, duplicate/conflict count, startup recovery, partial fills,
kill behavior, model outage behavior, quote/FX age, audit reconstruction and resource
usage. Include forced process termination and Gateway disconnect/reconnect. Passing
simulated tests does not fulfill this milestone.

## H: LIVE (hard-locked)

Requires a separate explicit authorization. Readiness must cover real paper soak,
account allowlist, tested bounded-loss/fees/FX/assignment behavior, risk limits,
kill/operational modes, complete reconciliation, broker/order idempotency evidence,
monitoring, operator recovery and deployment security. Completing earlier phases
never removes the lock automatically.

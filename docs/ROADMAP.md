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
and deployment documentation exist. The Tyrell-derived terminal UI provides the
sidebar, role chat, plans, market/position/order/audit views, validated setup and
appearance settings. Its isolated local operator service supports detach/reconnect
and persisted conversations. Chat has no broker tools.
The paper observer now feeds the autonomous event pipeline. A complete option/news
feed and broker execution reconciliation remain outstanding. No public endpoints or
infrastructure deployment.

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


## Coordinator and autonomous event delivery

Coordinator is the main operator contact and the default selected role. F1 documents
all responsibilities. Its specialists only report to Coordinator; their individual
chat histories are not copied into delegated reports. Archived/busy roles are
reported missing instead of impersonated.

The automatic event path runs the same bounded specialist collection, followed by
a strict typed proposal through OpenAIProvider, RiskEngine and ExecutionEngine.
The model has no tools. Simulator tests prove entry and protective exit with no
userMessage or approval. Existing exposure monitoring stays active during reasoning.
Real IBKR remains blocked at the earlier reconciliation gate. In particular, the
observer's underlying/FX snapshots are not yet a complete options/news feed.

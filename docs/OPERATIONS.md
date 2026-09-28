# Operations

## Prerequisites and isolation

Python >=3.12 and the declared extras are required for development/model/Gateway
features. Keep runtime dependencies in a dedicated venv. No model provider is
called by status/demo/tests. Use F10 to connect Codex CLI. Existing CLI authentication is reused by the CLI
itself; credentials and development conversations are never copied into Wayland.

Use a dedicated data root, ordinarily `~/.wayland`, and a single execution service
per broker account. Back up the full SQLite database through SQLite backup tooling
or after clean shutdown; copying only the main file while WAL is active is unsafe.
Audit includes model inputs/news: keep it private, define retention and avoid
supplying sensitive material. SDK exception bodies and environment values are not
logged. Configuration examples contain no account numbers or secrets.

## IB Gateway paper inspection

Install/sign in to IB Gateway's **paper** environment separately. API is loopback
only, paper port 4002 (Gateway) or 7497 (TWS), with a stable nonzero client ID.
The terminal connection flow discovers a single paper account automatically and saves
it in `ibkr_account` and `account_allowlist`; multiple accounts require a selection.
The adapter checks the reported managed account; port/name conventions alone are
not a substitute for the operator verifying Gateway's paper session.

```sh
wayland --config /private/path/settings.json ibkr-inspect
wayland --config /private/path/settings.json ibkr-inspect --market
```

Both commands are read-only. Missing permissions, closed/disconnected sessions,
nonfinite prices or delayed data stop the request. Data availability outside
market hours is not evidence of a live-data entitlement. No real Gateway check
has been performed merely because fake adapter tests pass.

## Service lifecycle

`wayland monitor --backend simulator` runs reconciliation without sending entries.
`--once` is appropriate for verification without leaving a persistent process.
The IBKR monitor deliberately stays SAFE because full execution-history coverage
is not implemented. Status is stored state: inspect heartbeat; it is not a live
broker query. SIGINT/SIGTERM stops the loop, prevents new submissions and preserves
state. A crash before completion forces reconciliation on the next start.

The event service is a Python integration interface. There is currently no
production news ingestion, execution-enabled IBKR feed, remote control endpoint or
web dashboard. These are future integration work, not implicit background services.

## Target host (documentation only)

Intended target: EC2 eu-north-1, Ubuntu 24.04 x86_64, t3.small, 20 GB encrypted gp3,
IB Gateway on the same host. Capacity and Gateway session behavior still require
soak verification. No AWS resources have been created.

Prefer SSM or another restricted administration channel. Do not expose IB Gateway,
VNC/RDP, SQLite, or administrative ports publicly. Supply secrets through a private
service environment/secret mechanism; do not put them in Git, user-data or logs.

Example unit, to adapt only after local paper verification:

```ini
[Unit]
Description=Wayland paper monitoring
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=wayland
WorkingDirectory=/opt/wayland
EnvironmentFile=/etc/wayland/runtime.env
ExecStart=/opt/wayland/.venv/bin/wayland --root /var/lib/wayland --config /etc/wayland/settings.json monitor --backend ibkr
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/wayland

[Install]
WantedBy=multi-user.target
```

This unit is not installed or enabled by the package. Gateway process supervision,
authentication renewal and scheduled maintenance handling remain operator setup.

## Failure runbook

1. Activate kill; preserve DB/WAL and broker session evidence.
2. Inspect state reasons, intent IDs, broker order IDs and fills.
3. If submission is uncertain, reconcile first; never manually replay the same
   order just because a timeout occurred.
4. Resolve account/data/history coverage issues before allowing READY.
5. Do not remove a process lock file to force another writer; stop the real owner.
6. Report verified observations separately from simulated results.


## Terminal broker connection

F10 → Connections → Connect IBKR paper starts a read-only observer in Wayland's own service.
Configure the shared paper account and allowlist in that same Connections view. The observer reconnects
after connection failure and revalidates account settings. Disconnect stops polling;
the saved preference survives service restarts. Settings changes re-establish the
connection before using a changed account.

The UI displays broker-returned positions/open orders separately from execution's
verified portfolio. Successful account connection is not successful reconciliation:
the operating state remains RECONCILING and execution_ready remains false.
Missing/stale quotes do not hide positions and never become tradeable data.
An observation older than 30 seconds is shown as stale/disconnected.

Local verification found no configured account and no listeners on 4002/7497.
Real account authentication, market permissions and paper data remain unverified.
Gateway paper login must occur in IBKR's application, not inside chat.


### Native paper routing (0.6.0)

Routing remains off by default. F10 → IBKR paper connection → Ibkr Paper Orders
accepts true/false. Gateway must use Paper Trading and have Read-Only API disabled.
Only long puts are routed. Combo orders are rejected.
A Gateway permission failure leaves monitoring connected and reconciliation blocked.
Unknown order outcomes require broker reconciliation; never clear the journal to retry.
Missing broker PnL/FX, external positions/orders and incomplete historical evidence
block readiness. Keep the local journal across restarts; do not copy another account's state.

## Order-free provider diagnostics

Run `wayland doctor` (or `wayland doctor --json`). It opens a separate read-only
IBKR connection, checks the configured account allowlist, qualification, quotes,
historical bars, options and subscribed news. It does not construct an execution
engine, bind order routing, place/cancel orders or change the trading state.
The OpenAI check is a real, small structured Codex CLI request using existing login.
The orchestration check is explicitly a four-request **fixture** smoke test, not
four real model analyses. The complete sanitized report is saved to
`~/.wayland/logs/doctor.json`.

`Reconciliation` requires successful broker readback AND a current READY runtime
reconciliation for the same account. Doctor does not replace reconciliation or
turn a disconnected/stale runtime into READY. Its NORMAL/SAFE/DEGRADED line is a
diagnostic assessment; LIVE always remains LOCKED.

Gateway error 321 with a Read-Only message on `reqCompletedOrdersAsync` blocks
order-history verification. Wayland records the operation, code and a fixed safe
reason, never the raw SDK error. Inspect Gateway's API settings and the paper
session; doctor never changes those settings. A timeout is reported as a timeout,
not assumed to be a missing subscription. Known market-data entitlement codes
are listed separately. Raw SDK logging is suppressed in doctor.

## Analytical data and entry policy

Independent collectors continue even when account reconciliation fails:

- Market: ORCL bid/ask/last/spread/volume, IBKR source, live/frozen/delayed type,
  receipt timestamp and regular-session status from IBKR contract schedules.
  Snapshot timestamps are receipt times, **not guaranteed exchange tick times**.
  Missing last/volume and unknown session remain explicit. FX failure preserves
  the stock evidence but blocks entry pricing.
- Technical: three days of RTH TRADES at five-minute resolution; only closed bars
  are used. At least 20 consecutive bars in the latest session are required.
  SMA20, typical-price volume-weighted VWAP, session extrema, rebound and reversal
  fractions are computed with Decimal in Python. This deliberately prevents
  entry during the first 100 minutes of a new session until enough bars exist.
  Historical high/low evidence does not replace the runtime's event detector.
- Options: IBKR-qualified puts with conId, terms and timestamps. Sampling is
  bounded to the nearest three strikes of one eligible expiry, plus held legs.
  Volume/open interest remain null when IBKR snapshots do not supply them.
  Long-ask minus short-bid pricing supplies deterministic per-unit debit,
  entry cost and maximum loss in SEK with FX and conservative round-trip fees.
  A calculable spread is research evidence; native spread routing remains disabled.
- News: a distinct `reqNewsProviders` / `reqHistoricalNews` feed of ORCL headlines
  from available subscribed news providers, carrying publisher, article ID,
  publication/retrieval time and source reference. These are headlines, not full
  articles or a comprehensive earnings/event calendar. Brokerage messages are
  never converted into news. Missing/empty news means unavailable, not no catalysts.

Quote/options use the configured freshness limit (default 10s); FX defaults to
60s. Latest closed OHLCV must be no older than 15 minutes, sourced news no older
than 24 hours. All ages are checked again when consumed and before new entries.
Missing news/history, delayed/stale critical data, an unknown/outside regular
session, or an indeterminate candidate cost blocks new AI ENTER. Position
protection still goes through the existing RiskEngine. SAFE broker reconciliation
always takes precedence. History/news may be cached for 60s and chain definitions
for 15 minutes; original evidence timestamps are retained and cached reads marked.

API references: [IBKR error codes](https://interactivebrokers.github.io/tws-api/message_codes.html)
and [ib_async endpoint documentation](https://ib-api-reloaded.github.io/ib_async/api.html).

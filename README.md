# Wayland

**Models reason. Deterministic Python controls execution.**

Wayland is a paper-first trading research application with durable order intentions,
explicit reconciliation and an inspectable audit trail. Its initial research scope
is ORCL bearish rebound/reversal setups using long puts or defined-risk put spreads.
Its trading state and conversations are independent of development assistants.
Codex CLI owns authentication; Wayland creates fresh ephemeral analysis sessions.

## Current boundary

- A persistent **simulator** exercises entry, rejection, partial fill, cancellation,
  exit, process death and recovery. It is not Interactive Brokers paper trading.
- The **IBKR adapter defaults to read-only**. Opt-in native paper routing supports
  long-put DAY limit orders and owned-order cancellation after reconciliation.
  It samples ORCL/USDSEK and qualified options, journals broker evidence and blocks
  on missing history, unknown positions or unavailable account PnL. Native spreads
  remain disabled; real market-session paper soak is still outstanding.
- An isolated Codex CLI provider returns validated proposals. It has no
  broker tools or credentials. A model error produces no new trade.
- **LIVE is hard-locked**, regardless of configuration flags.
- `monitor` refreshes broker state; it does not enable a live market/news feed or
  autonomous entries. `WaylandService.market_event` is the integration boundary.

## Terminal application

Run `wayland` to open the Tyrell-derived terminal interface. Its sidebar, focus
navigation, mouse handling, prompt editor, history selection, manual clipboard,
clickable links, slash completion and appearance editor are reused from Tyrell.
Wayland owns its runtime and state; Tyrell is not installed or contacted.

Coordinator (Strategist) is selected on startup and is the main operator contact.
F1 explains every default role and its responsibilities. The sidebar also lets
you talk directly to individual specialists. Use F3 to create one, F5 to
rename, and F4 to browse the archive. F2 / arrow keys navigate Chat, Plan, Market,
Audit, Positions, Orders and Setup. F10 opens Connections directly; press S there for other settings and appearance.

Coordinator collects independent Market, Technical, News and Options reports and
synthesizes them. Specialists do not receive peer conversations or reports.
Ordinary chat uses the configured Codex model without order permissions.

Broker observations also drive the autonomous event loop: a qualifying setup,
verified broker state and qualified option candidates trigger specialist review,
a strict TradeProposal and deterministic risk/execution checks without a user
prompt or per-order approval. Normal quotes do not invoke models. Each review
uses at most four specialist calls and one synthesis; missing/busy roles are
explicitly unavailable. Position monitoring continues during reasoning.

This automatic chain is verified with the persistent simulator and mocked models.
The real IBKR adapter still reports incomplete execution reconciliation and blocks
orders. Real broker history, continuous option qualification, live model timing,
news ingestion and paper soak remain outstanding. Connecting Gateway alone does
not make the system ready for autonomous IBKR paper trading. Additional prompts are queued visibly in the plan. F10 → Connections
connects Codex and IBKR once for all roles. Setup holds global data quality
and risk settings. Its changes apply
to new analyses and the next monitor start, never to an active order.

Market/position/order views show stored Wayland records and explicitly identify
missing or stale state. They do not imply a connected live feed. The demo still
uses its separate simulation database and does not populate the trading views.

The UI starts an independent, local operator service. Ctrl+Q closes the view;
analysis and conversations remain in that service. `wayland service-stop` stops
it; `wayland service-restart` restarts it with your current shell environment.
Connect from **F10**:

- **O — Connect Codex CLI:** an existing CLI login is detected automatically.
  When signed out, Wayland starts the official `codex login` browser flow. There
  is no Wayland login website, credential form or token copying. The optional
  model field can be left empty to use Codex's default model.
- **I — Open IBKR:** opens the installed official IB Gateway or Trader Workstation.
  Choose **Paper Trading** and log in there. Wayland detects the local paper API
  and verifies the reported account before saving it. Multiple paper accounts
  require selecting the desired account in Connections. If the client is missing,
  the button opens IBKR's official download page.
- **S — Other settings:** appearance, risk/data limits and diagnostics.

IBKR API access must be enabled in the official client. Wayland uses only local
paper ports 4002/7497; routing is disabled by default and live accounts are rejected. Connected
means the account can be observed, **not** that execution reconciliation is complete.
Real IBKR order submission remains locked.

Codex runs with user/project instructions, hooks, apps, plugins, shell/browser tools
and agent spawning disabled, with read-only sandboxing and ephemeral sessions.
Each result must pass local schema validation and the existing deterministic risk
checks. A failed login, quota error, interrupted process or invalid result authorizes
no new order. Wayland does not import development conversations or reuse thread IDs.
The former browser form and managed Client Portal proxy have been removed.

Authentication references: [Codex authentication](https://learn.chatgpt.com/docs/auth),
[non-interactive Codex](https://learn.chatgpt.com/docs/non-interactive-mode), and
[IB Gateway/TWS](https://www.interactivebrokers.com/docs/tws-api/doc/download-tws-or-ib-gateway/download-tws-or-ib-gateway).

## Install

Python 3.12 or newer, on macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,ibkr]'
.venv/bin/wayland
.venv/bin/wayland config-example
.venv/bin/wayland status
.venv/bin/wayland demo
```

Default state belongs exclusively to `~/.wayland/{config,state,logs,data}`.
Use `--root PATH` before the subcommand to isolate a development run. Demo uses
its own databases under `data/demo`, distinct from normal runtime state.
The synthetic demo performs an entry, simulated fill and protective-shaped exit;
it never connects to a broker or sends a model request. Repeated runs still obey
persistent daily limits.

Save `config-example` output to a JSON file and pass `--config FILE`. The default
account allowlist is empty: new entries fail closed until configured. No real
account identifiers or credentials belong in the repository. IB Gateway handles
broker login. Codex CLI manages model authentication.
Wayland uses Codex CLI authentication and its applicable model access and usage limits.

## Operator commands

```sh
wayland --config settings.json status
wayland --config settings.json monitor --backend simulator --once
wayland kill on
wayland audit
# Explicit opt-in, read-only connection to a local paper Gateway:
wayland --config settings.json ibkr-inspect --market
```

Status is a stored snapshot; inspect its heartbeat and reason fields. Activating
kill blocks entries, not policy-validated reductions of existing exposure. Neither
turning kill off nor restarting bypasses reconciliation. Unknown submissions are
not retried automatically.

## Development

```sh
.venv/bin/ruff check src tests
.venv/bin/mypy src/wayland
.venv/bin/pytest -q
```

Tests use a durable fake broker and mocked model/Gateway calls. Crash tests kill
real child processes at persistence/submission boundaries. Ordinary tests do not
require account access, Gateway, an API key or paid model calls.

[Architecture](docs/ARCHITECTURE.md) · [Risk controls](docs/RISK.md) ·
[Operations](docs/OPERATIONS.md) · [Roadmap and verification](docs/ROADMAP.md)


### Native paper orders

F10 → IBKR paper connection → **Ibkr Paper Orders** accepts `true` or `false`.
In the official Gateway's Configure → Settings → API → Settings, **Read-Only API**
must be unchecked for native paper routing and completed-order reconciliation.
Keep Gateway logged into **Paper Trading**. Never change to a live port/account.

A connected account is not automatically READY. Wayland requires current broker
positions/open orders/completed orders/executions and account-wide PnL converted
to SEK, plus fresh underlying, FX and option quotes. Missing subscriptions,
closed-market quotes or incomplete order history block new entries. F10 shows
the connection and data blockers. The application never assumes a timed-out order
failed, never automatically resends it, and does not route native spreads.


### Coordinator and specialist runs

Wayland's Python runtime controls the fixed four-role agent chain:
`AgentOrchestrator → SpecialistRunner → model provider → validated reports → Coordinator`.
The existing isolated Codex CLI transport is reused. Models have no agent-launch
or broker-order tools.

Chat coordination prepares four distinct, role-specific assignments. Each specialist
receives a new request and its own evidence envelope, with no peer conversations or
previous reports. Up to four requests execute concurrently. A timeout/failure produces
an explicit missing report while the other specialists continue. The Coordinator
receives schema-validated reports with run IDs and timestamps, not internal chat history.
Automatic market-event analysis uses the same orchestrator; proposals still pass
through the independent RiskEngine/ExecutionEngine.

Inspect the latest run or a particular run:

```sh
wayland runs
wayland runs --run-id <coordinator-run-id> --json
```

Test the production orchestration layer without IBKR, model credentials or network
calls using explicitly synthetic providers:

```sh
wayland orchestration-demo
wayland runs --demo --json
```

Fixture runs live under `~/.wayland/data/orchestration-demo/`, separately from normal
runtime runs. Audit records retain the original requests, context references and
context payloads, start/end times, provider/model, statuses and validated results.
Interrupted runs are marked on service restart and never silently replayed.

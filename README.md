# Wayland

**Models reason. Deterministic Python controls execution.**

Wayland is a paper-first trading research application with durable order intentions,
explicit reconciliation and an inspectable audit trail. Its initial research scope
is ORCL bearish rebound/reversal setups using long puts or defined-risk put spreads.
It is independent of development assistants and their CLI sessions.

## Current boundary

- A persistent **simulator** exercises entry, rejection, partial fill, cancellation,
  exit, process death and recovery. It is not Interactive Brokers paper trading.
- The **IBKR adapter is read-only**: paper account verification, positions/orders,
  ORCL/USDSEK data, chain metadata and qualified option quotes. It rejects all
  order and cancel submissions pending real paper lifecycle verification.
- An isolated OpenAI Responses provider returns validated proposals. It has no
  broker tools or credentials. A model error produces no new trade.
- **LIVE is hard-locked**, regardless of configuration flags.
- `monitor` refreshes broker state; it does not enable a live market/news feed or
  autonomous entries. `WaylandService.market_event` is the integration boundary.

## Terminal application

Run `wayland` to open the Tyrell-derived terminal interface. Its sidebar, focus
navigation, mouse handling, prompt editor, history selection, manual clipboard,
clickable links, slash completion and appearance editor are reused from Tyrell.
Wayland owns its runtime and state; Tyrell is not installed or contacted.

The sidebar contains independent analytical roles. Use F3 to create one, F5 to
rename, and F4 to browse the archive. F2 / arrow keys navigate Chat, Plan, Market,
Audit, Positions, Orders and Setup. F10 opens application settings and appearance.

Chat uses the configured OpenAI API model for analysis only, without tools or
order permissions. Additional prompts are queued visibly in the plan. Setup
validates model, paper account, data quality and risk settings. Its changes apply
to new analyses and the next monitor start, never to an active order.

Market/position/order views show stored Wayland records and explicitly identify
missing or stale state. They do not imply a connected live feed. The demo still
uses its separate simulation database and does not populate the trading views.

The UI starts an independent, local operator service. Ctrl+Q closes the view;
analysis and conversations remain in that service. `wayland service-stop` stops
it; `wayland service-restart` restarts it with your current shell environment.
After exporting OPENAI_API_KEY, restart the service and choose the model in Setup.
No API request is made merely by opening the interface.

## Install

Python 3.12 or newer, on macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,openai,ibkr]'
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
broker login; `OPENAI_API_KEY` is supplied to the SDK through the environment.
A CLI subscription does not supply Wayland's runtime API access.

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

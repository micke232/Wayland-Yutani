# Verification record

## Local verification — 2026-09-27

Environment: macOS, Python 3.14.7, isolated worktree .venv. Installed editable
package with dev, openai and ibkr extras. Key versions: Pydantic 2.13.5,
pytest 9.1.1, Ruff 0.16.9, mypy 1.20.2, OpenAI 2.54.0, ib_async 2.1.0.

| Check | Result |
| --- | --- |
| `.venv/bin/pytest -q` | 60 passed |
| `.venv/bin/ruff check src tests` | Passed |
| `.venv/bin/ruff format --check src tests` | 27 files formatted |
| `.venv/bin/mypy src/wayland` | Passed, 20 source files |
| `git diff --check` | Passed |
| Wheel build with `pip wheel --no-deps .` | Passed |
| Wheel installation to isolated target | Passed; imported that installed package and ran config-example |
| CLI demo in isolated .runtime root | Entry OPEN, two fills, position closed, final READY |
| CLI monitor --once with safe defaults | SAFE / account_not_allowlisted, as expected |
| CLI status | Returned persisted monitor state |

## What the tests exercise

- Deterministic risk limits, data age, currency, multiplier, DTE, spread and
  available liquidity fields; conservative loss including fee reserves.
- Duplicate proposals, lost acknowledgement, partial fill/cancel, broker disconnect,
  conflicting exposure, startup recovery with open orders and open positions.
- Actual subprocess termination after durable intent, before broker invocation,
  and after broker acceptance. Recovery does not blindly replay uncertain orders.
- Single writer lock, persistent kill switch and rejection of LIVE before submission.
- Protective exits during model outage and provider success/failure/refusal audit.
- Persisted setup detection/cooldown and read-only IBKR account/connection guards.
- Invalid/crossed/non-finite underlying and FX bid/ask rejection.

The broker is a separately persisted simulator. Model responses and IBKR boundary
tests use test doubles. Simulator spread fills are atomic; this does not establish
real combo-leg or assignment safety. The CLI demo uses synthetic prices and no
network, model or brokerage account.

## External integration not verified

No real OpenAI request, IB Gateway connection, IBKR paper order, prolonged paper
soak, live trade or infrastructure deployment has been performed. Installing the
SDKs does not establish account access or runtime compatibility with a real Gateway.
The IBKR adapter is explicitly read-only and cannot submit or cancel orders.

The simulator's complete history is stronger than IBKR's recent execution window.
Real history completeness, event persistence, broker ID correlation, combo lifecycle,
market entitlements and reconnect behavior must be verified before enabling paper
execution. See ROADMAP.md. Only Python 3.14.7 was executed locally; other supported
Python versions and deployment platforms remain unverified.

## Terminal UI delivery — 2026-09-27

Version 0.2.0 reuses Tyrell's interactive UI with a Wayland-only operator service.

- Full suite: **102 passed**, including the original risk/recovery cases.
- Ruff lint and format checks pass; mypy passes for 41 source files.
- Real PTY integration exercises animated startup, the five-role sidebar, F10,
  appearance, keyboard exit and service persistence after detaching.
- Renderer tests cover all seven tabs at 70×18, 120×40 and 180×50.
- Tests cover mouse role selection, draft preservation, slash completion,
  role create/rename/archive/restore, validated settings, persistent entry kill,
  queued prompts/plans, missing API configuration, and analytical API isolation.
- Original Tyrell composer/clipboard regression tests were ported. Clipboard
  subprocess behavior is mocked; no new claim of OS-wide Cmd+C verification.
- Analytical API test uses a fake SDK response and confirms no tools/account data
  are sent, and requested/reported models are audited. No paid API call was made.
- Built a wheel and installed it in the user's independent Wayland client.
  A second PTY run targets the actual ~/.local/bin/wayland launcher, without
  PYTHONPATH or an activated development environment, and passes.
- No Tyrell files or running Tyrell processes were modified.
- No merge, live trade, real IBKR paper order or deployment.

The default terminal command now opens the UI; JSON CLI subcommands remain for
automation. The UI service supports separate stop/restart commands. Broker/feed
integration limits documented above remain unchanged.

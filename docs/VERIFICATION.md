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


## Read-only broker observer — 2026-09-27

Version 0.2.1: 109 tests pass, plus lint, formatting and type checks. New fake-broker
tests verify that missing configuration never contacts Gateway, stale quotes are
not published as current, account mismatch disconnects, transient failure reconnects,
stale UI observations show disconnected, positions remain visible during quote failure, and valid
quotes never mark execution ready. The real local prerequisite probe found no
Gateway/TWS paper listener on 4002/7497 and no configured paper account. No real
broker connection or order is claimed.


## Global connections — 2026-09-27

Version 0.2.2: 111 tests pass. OpenAI and IBKR connection settings and the broker
connect/disconnect control are now in F10 → Connections, shared by all roles.
Regression tests verify removal from Setup, global model application, navigation
to the final connection field, and cancelling edits without losing a chat draft.
No credential migration or account changes were needed.


## Coordinator and autonomy — 2026-09-27

Version 0.3.0: **118 tests pass**, Ruff passes and mypy passes for 45 source files.
Tests prove independent specialist envelopes, bounded four-plus-one routing,
explicit missing/failed roles, interruption, and preservation of user-renamed roles.

A complete autonomous simulation drives price events into the strategy detector,
specialist reports, typed proposal, deterministic risk, entry, fill and protective
exit with no user prompt. Model responses and fills are controlled test fixtures.
A separate test rejects all analysis/orders for incomplete broker state, and another
verifies position monitoring continues while reasoning is pending. This is not
real IBKR paper or real OpenAI verification.


## Official-client authentication (0.5.0)

The local credential form and Client Portal Java proxy are removed. Codex CLI
owns login; Wayland uses stateless, read-only, isolated structured CLI requests.
A real Codex request using the existing ChatGPT CLI login returned the expected
connection-test answer. Native IB Gateway 10.50 was installed and discovered.
No real broker account login or order has been submitted by the implementation.

Regression coverage includes CLI process timeouts/cleanup, rejecting tool activity,
credential-environment exclusion, using existing login without relogin, opening the
official IBKR client, and accepting only verified DU paper accounts during discovery.
The installed terminal is exercised through a PTY, including F10 navigation.

Validation completed for 0.5.0: 129 tests passed, Ruff check/format and mypy passed.
The installed `~/.local/bin/wayland` passed the PTY test. Its real analytical role
returned a Codex response and persisted the response audit. The installed package
contains no `browser_setup` module. Official IB Gateway was opened through the
connection action; broker login still requires the user's paper credentials.

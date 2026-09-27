# Operations

## Prerequisites and isolation

Python >=3.12 and the declared extras are required for development/model/Gateway
features. Keep runtime dependencies in a dedicated venv. No model provider is
called by status/demo/tests. Configure a model and `OPENAI_API_KEY` explicitly
before opting into real reasoning. Never copy CLI authentication or development
agent sessions into Wayland.

Use a dedicated data root, ordinarily `~/.wayland`, and a single execution service
per broker account. Back up the full SQLite database through SQLite backup tooling
or after clean shutdown; copying only the main file while WAL is active is unsafe.
Audit includes model inputs/news: keep it private, define retention and avoid
supplying sensitive material. SDK exception bodies and environment values are not
logged. Configuration examples contain no account numbers or secrets.

## IB Gateway paper inspection

Install/sign in to IB Gateway's **paper** environment separately. API is loopback
only, paper port 4002 (Gateway) or 7497 (TWS), with a stable nonzero client ID.
Set the explicit paper account in both `ibkr_account` and `account_allowlist`.
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
production news ingestion, continuous IBKR quote pump, remote control endpoint or
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

"""Order-free diagnostic runner. Never constructs an execution engine or order command."""

import asyncio
import json
from contextlib import closing
from pathlib import Path
from typing import Any

from .audit import AuditStore
from .broker.diagnostics import diagnostic
from .broker.discovery import observation_account
from .broker.ibkr import IbkrBroker
from .broker.research import NativeResearch
from .data import DataPipeline
from .models import utcnow
from .orchestration_demo import run_demo


async def model_check(root, settings):
    from .providers.cli_transport import analyze

    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["OK"]}},
        "required": ["status"],
        "additionalProperties": False,
    }
    response = await analyze(
        root,
        settings,
        "Connectivity diagnostic only. Return status OK. No tools or trading.",
        {"diagnostic": True},
        schema,
    )
    if response.output != {"status": "OK"}:
        raise ValueError("Invalid diagnostic response")
    return {
        "status": "OK",
        "reason": "Real model request completed",
        "verification": "real model",
        "model": response.model,
    }


async def run_doctor(
    root: Path, settings, factory=IbkrBroker, source_factory=NativeResearch, model_probe=model_check
):
    report: dict[str, Any] = {"timestamp": utcnow().isoformat(), "order_free": True, "checks": {}}
    checks = report["checks"]

    def fail(name, operation, error):
        checks[name] = {"status": "FAIL", **diagnostic(operation, error)}

    async def models():
        try:
            checks["OpenAI"] = await asyncio.wait_for(
                model_probe(root, settings), settings.analysis_timeout_seconds + 5
            )
        except Exception as error:  # noqa: BLE001 -- independent provider failure, sanitized diagnostics
            fail("OpenAI", "Codex CLI structured diagnostic", error)
        try:
            record = await run_demo(root / "data/doctor")
            if len(record["specialists"]) != 4 or any(
                r["status"] != "COMPLETED" for r in record["specialists"].values()
            ):
                raise ValueError("Fixture chain incomplete")
            checks["Agent orchestration"] = {
                "status": "OK",
                "reason": "Four isolated fixture requests completed; this is not a live specialist analysis",
                "verification": "fixture",
                "run_id": record["coordinator_run_id"],
            }
        except Exception as error:  # noqa: BLE001 -- independent provider failure, sanitized diagnostics
            fail("Agent orchestration", "fixture orchestration", error)

    model_task = asyncio.create_task(models())
    # A separate client ID avoids stealing the operator session; no routing binding exists.
    config = settings.model_copy(
        update={"ibkr_client_id": settings.ibkr_client_id + 1000, "ibkr_paper_orders": False}
    )
    broker = factory(config)
    checks["Account allowlist"] = {
        "status": "OK" if config.ibkr_account and config.ibkr_account in config.account_allowlist else "FAIL",
        "reason": "Configured account allowlist membership (identifier redacted)",
    }
    checks["IBKR account"] = {"status": "UNKNOWN", "reason": "Not verified against connected Gateway"}
    data: dict = {}
    try:
        await asyncio.wait_for(broker.connect(), 30)
        checks["IBKR connection"] = {
            "status": "OK",
            "reason": "Separate read-only diagnostic connection",
            "verification": "real broker" if factory is IbkrBroker else "injected test broker",
        }
        checks["IBKR account"] = {
            "status": "PAPER" if observation_account(config.ibkr_account) else "UNKNOWN",
            "reason": "Account verified by Gateway and paper adapter",
        }
        source = source_factory(broker)

        async def reconcile():
            try:
                await broker.read_state(completed=True)
                with closing(AuditStore(root / "state/wayland.sqlite")) as store:
                    state = store.get("operating_state")
                    heartbeat = store.get("heartbeat")
                    portfolio = store.get("portfolio") or {}
                from .data import stamp

                current = (
                    heartbeat
                    and 0 <= (utcnow() - stamp(heartbeat)).total_seconds() <= 30
                    and portfolio.get("account") == config.ibkr_account
                    and portfolio.get("healthy")
                    and portfolio.get("complete")
                )
                checks["Reconciliation"] = {
                    "status": "OK" if state == "READY" and current else "FAIL",
                    "reason": "Broker readback completed; "
                    + (
                        "current runtime reconciliation verified"
                        if state == "READY" and current
                        else "no current READY reconciliation in runtime; diagnostic does not change trading state"
                    ),
                }
            except Exception as error:  # noqa: BLE001 -- independent provider failure, sanitized diagnostics
                fail("Reconciliation", "broker.read_state including completed orders", error)

        try:
            data, _ = await asyncio.gather(DataPipeline(source, config).collect(), reconcile())
            checks["ORCL qualification"] = source.qualification or {
                "status": "FAIL",
                "reason": "No qualified ORCL contract returned",
            }
            checks["Options chain"] = source.chain or {
                "status": "FAIL",
                "reason": "No chain received; inspect qualification/connection diagnostics",
            }
            for label, key in [
                ("ORCL quote", "quote"),
                ("Historical OHLCV", "history"),
                ("Options quotes", "options"),
                ("News provider", "news"),
            ]:
                checks[label] = data["checks"][key]
            checks["Market data age"] = {
                "status": str(data["checks"].get("quote", {}).get("age_seconds", "unavailable")),
                "unit": "seconds",
            }
            report["data_policy"] = data["entry_policy"]
        finally:
            await source.close()
    except Exception as error:  # noqa: BLE001 -- independent provider failure, sanitized diagnostics
        fail("IBKR connection", "connectAsync", error)
        for key in (
            "ORCL qualification",
            "ORCL quote",
            "Historical OHLCV",
            "Options chain",
            "Options quotes",
            "News provider",
            "Reconciliation",
        ):
            checks[key] = {
                "status": "FAIL",
                "reason": "IBKR connection not verified; operation not attempted",
            }
        checks["Market data age"] = {"status": "unavailable"}
    finally:
        report["broker_diagnostics"] = list(getattr(broker, "diagnostics", []))
        await broker.close()
    await model_task
    checks["Operating state"] = {
        "status": "SAFE"
        if checks["Reconciliation"]["status"] != "OK"
        else (
            "NORMAL"
            if all(
                c["status"] == "OK" for k, c in checks.items() if k not in ("IBKR account", "Market data age")
            )
            and data.get("entry_policy", {}).get("allowed")
            else "DEGRADED"
        ),
        "reason": "Diagnostic assessment only; does not enable trading",
    }
    checks["Live trading"] = {"status": "LOCKED", "reason": "LIVE hard lock unchanged"}
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs/doctor.json").write_text(json.dumps(report, indent=2))
    return report


def render(report):
    rows = []
    for key in (
        "OpenAI",
        "Agent orchestration",
        "IBKR connection",
        "IBKR account",
        "Account allowlist",
        "ORCL qualification",
        "ORCL quote",
        "Market data age",
        "Historical OHLCV",
        "Options chain",
        "Options quotes",
        "News provider",
        "Reconciliation",
        "Operating state",
        "Live trading",
    ):
        c = report["checks"][key]
        rows.append(f"{key:22} {c['status']}" + (f" · {c['reason']}" if c.get("reason") else ""))
    for d in report.get("broker_diagnostics", []):
        rows.append(f"IBKR {d['code']} · {d['operation']} · {d['reason']}")
    return "\n".join(rows)

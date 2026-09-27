"""Thin operator CLI. It never discovers development CLI accounts or agents."""

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from .audit import AuditStore, encode
from .broker.ibkr import IbkrBroker
from .broker.paper import PaperBroker
from .config import Settings, app_root, example_config, load_settings
from .demo import fixture
from .execution import ExecutionEngine
from .models import Action, Direction, TradeProposal, utcnow


def status(root: Path) -> dict:
    store = AuditStore(root / "state/wayland.sqlite")
    try:
        return {
            "mode": store.get("mode", "PAPER"),
            "live_locked": True,
            "operating_state": store.get("operating_state", "RECONCILING"),
            "state_reasons": store.get("state_reasons", ["not_started"]),
            "kill_switch": store.get("kill_switch", False),
            "heartbeat": store.get("heartbeat"),
            "note": "Stored snapshot; inspect heartbeat before treating state as current",
            "market": store.get("market"),
            "setup": store.get("setup:ORCL"),
            "latest_decision": store.get("latest_decision"),
            "portfolio": store.get("portfolio"),
            "order_intents": [
                {k: row[k] for k in ("intent_id", "status", "broker_id")} for row in store.intents()
            ],
            "recent_events": store.recent(10),
        }
    finally:
        store.close()


async def demo(root: Path) -> dict:
    # Separate demo DBs avoid mixing a simulation with configured broker state.
    directory = root / "data/demo"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = Settings(account_allowlist=("SIMULATED-PAPER",))
    broker = PaperBroker(directory / "broker.sqlite")
    store = AuditStore(directory / "wayland.sqlite")
    engine = ExecutionEngine(store, broker, settings)
    try:
        await engine.reconcile()
        market, candidate, proposal = fixture()
        result = await engine.execute(proposal, market, candidate)
        if result.broker_id and result.state == "OPEN":
            await broker.fill(result.broker_id)
            await engine.reconcile()
            if engine.portfolio is None or not engine.portfolio.positions:
                raise RuntimeError("Demo fill was not reconciled")
            position = engine.portfolio.positions[0]
            exit_proposal = TradeProposal(
                proposal_id=proposal.proposal_id + "-exit",
                snapshot_id=market.snapshot_id,
                timestamp=utcnow(),
                symbol="ORCL",
                action=Action.EXIT,
                direction=Direction.NEUTRAL,
                instrument=position.candidate.instrument,
                candidate_id=None,
                position_id=position.position_id,
                quantity=position.quantity,
                estimated_cost_sek=None,
                max_loss_sek=None,
                confidence=1.0,
                thesis="Close synthetic demo",
                invalidation="",
                invalidation_price=None,
            )
            exit_result = await engine.execute(exit_proposal, market, None)
            if exit_result.broker_id:
                await broker.fill(exit_result.broker_id)
            await engine.reconcile()
        return {
            "simulation_only": True,
            "live_locked": True,
            "entry": result.model_dump(mode="json"),
            "operating_state": engine.state,
            "portfolio": engine.portfolio.model_dump(mode="json") if engine.portfolio else None,
            "database": str(store.path),
        }
    finally:
        await engine.close()


async def inspect_ibkr(settings: Settings, market: bool) -> dict:
    broker = IbkrBroker(settings)
    try:
        await broker.connect()
        result = await broker.inspect()
        if market:
            result["market"] = (await broker.market_data()).model_dump(mode="json")
            result["option_chains"] = await broker.option_chains()
        return result
    finally:
        await broker.close()


async def run_monitor(root: Path, settings: Settings, backend: str, once: bool) -> None:
    from .providers.codex import CodexProvider
    from .service import WaylandService

    broker = (
        PaperBroker(root / "data/paper-broker.sqlite") if backend == "simulator" else IbkrBroker(settings)
    )
    if isinstance(broker, IbkrBroker):
        await broker.connect()
    store = AuditStore(root / "state/wayland.sqlite")
    store.set("mode", settings.mode)
    try:
        engine = ExecutionEngine(store, broker, settings)
    except Exception:
        await broker.close()
        store.close()
        raise
    provider = CodexProvider(settings, store)
    service = WaylandService(engine, provider)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        if once:
            await engine.reconcile()
            print(encode({"state": engine.state, "reasons": store.get("state_reasons")}))
        else:
            # This command monitors only. Enabling an external quote/news feed is a separate integration.
            await service.monitor(stop)
    finally:
        await provider.close()
        await engine.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wayland: paper-first research runtime; LIVE is hard-locked")
    parser.add_argument("--root", type=Path, help="Independent application data root (default ~/.wayland)")
    parser.add_argument("--config", type=Path, help="Validated JSON settings; no secrets")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("dashboard", help="Open the interactive Wayland terminal")
    commands.add_parser(
        "ui-service", help="Run the internal operator service (normally started automatically)"
    )
    commands.add_parser("service-stop", help="Stop the independent operator service")
    commands.add_parser("service-restart", help="Restart the operator service with the current environment")
    commands.add_parser("status", help="Stored operational state, orders, positions and recent audit (JSON)")
    commands.add_parser("demo", help="Run synthetic entry/risk/fill/exit flow in isolated demo databases")
    commands.add_parser("config-example", help="Print safe default settings")
    commands.add_parser("audit", help="Print recent persisted audit events")
    runs = commands.add_parser("runs", help="Inspect persisted coordinator/specialist runs")
    runs.add_argument("--run-id")
    runs.add_argument("--json", action="store_true")
    runs.add_argument("--demo", action="store_true", help="Inspect isolated fixture runs")
    chain = commands.add_parser(
        "orchestration-demo", help="Test the agent chain with fixtures; no broker or model calls"
    )
    chain.add_argument("--json", action="store_true")
    kill = commands.add_parser(
        "kill", help="Persist entry kill switch; risk-reducing exits remain policy-gated"
    )
    kill.add_argument("value", choices=("on", "off"))
    ibkr = commands.add_parser("ibkr-inspect", help="Opt-in read-only paper Gateway inspection")
    ibkr.add_argument("--market", action="store_true")
    monitor = commands.add_parser(
        "monitor", help="Monitor/reconcile positions; does not start automated entries"
    )
    monitor.add_argument("--backend", choices=("simulator", "ibkr"), default="simulator")
    monitor.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "config-example":
            print(example_config())
            return
        root = app_root(args.root)
        config_path = args.config or root / "config/settings.json"
        settings = load_settings(config_path) if config_path.exists() else load_settings(args.config)
        if args.command in ("service-stop", "service-restart"):
            import time

            from .tui.client import ensure_service, request, socket_path

            try:
                request(root, "shutdown")
            except (OSError, RuntimeError):
                pass
            deadline = time.monotonic() + 5
            while socket_path(root).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if socket_path(root).exists():
                raise RuntimeError("Operator service did not stop")
            if args.command == "service-restart":
                ensure_service(root)
            print(
                "Wayland operator service "
                + ("restarted" if args.command == "service-restart" else "stopped")
            )
            return
        if args.command in (None, "dashboard"):
            from .tui.app import run

            run(root)
            return
        if args.command == "ui-service":
            from .operator import serve

            serve(root)
            return
        if args.command in ("runs", "orchestration-demo"):
            from .orchestration import RunJournal, render_run

            if args.command == "orchestration-demo":
                from .orchestration_demo import run_demo

                record = asyncio.run(run_demo(root))
            else:
                path = root / (
                    "data/orchestration-demo/wayland.sqlite" if args.demo else "state/wayland.sqlite"
                )
                record = RunJournal(path).read(args.run_id)
            print(encode(record) if args.json else render_run(record))
        elif args.command == "status":
            print(encode(status(root)))
        elif args.command == "demo":
            print(encode(asyncio.run(demo(root))))
        elif args.command == "ibkr-inspect":
            print(encode(asyncio.run(inspect_ibkr(settings, args.market))))
        elif args.command == "monitor":
            asyncio.run(run_monitor(root, settings, args.backend, args.once))
        else:
            store = AuditStore(root / "state/wayland.sqlite")
            try:
                if args.command == "kill":
                    with store.transaction():
                        store.set("kill_switch", args.value == "on")
                        store.event("kill_switch", {"enabled": args.value == "on"})
                    print("Entry kill switch " + args.value + "; reconciliation required before new entries")
                else:
                    print(encode(store.recent(50)))
            finally:
                store.close()
    except Exception as error:  # noqa: BLE001 -- fail closed without exposing SDK payloads
        # Avoid printing SDK/broker exception strings which may contain sensitive request data.
        print(
            "Wayland stopped safely: " + type(error).__name__ + ". Check configuration and prerequisites.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

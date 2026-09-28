"""Read-only connection supervisor, separate from verified execution state."""

import asyncio
import threading

from ..market import candidates, fresh
from ..models import utcnow
from .ibkr import IbkrBroker


class BrokerObserver:
    def __init__(self, runtime, factory=IbkrBroker, discovery=None):
        self.runtime, self.factory = runtime, factory
        self.discovery = discovery
        self.stop = threading.Event()
        self.thread = None
        self.autonomy = None
        self.data_task = None

    def start(self):
        if self.thread and self.thread.is_alive():
            if self.stop.is_set():
                raise ValueError("Disconnect is finishing. Retry Connect shortly.")
            return
        self.stop.clear()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()

    def publish(self, status, **values):
        observation = {
            "status": status,
            "timestamp": utcnow().isoformat(),
            "connected": False,
            "read_only": True,
            "execution_ready": False,
            **values,
        }
        with self.runtime.store() as store:
            previous = store.get("broker:observation", {})
            store.set("broker:observation", observation)
            if previous.get("status") != status:
                store.event("broker.connection", {"status": status, "read_only": observation["read_only"]})

    async def pause(self, seconds):
        deadline = asyncio.get_running_loop().time() + seconds
        while not self.stop.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

    async def research(self, broker, config):
        from ..data import DataPipeline
        from .research import NativeResearch

        cache: dict = {}
        while not self.stop.is_set():
            source = NativeResearch(broker)
            try:
                data = await DataPipeline(source, config, cache).collect()
                data["qualification"] = source.qualification
                data["options_chain"] = source.chain
                data["broker_diagnostics"] = list(broker.diagnostics)
                with self.runtime.store() as store:
                    store.set("data:evidence", data)
                    store.event(
                        "data.collected", {"checks": data["checks"], "entry_policy": data["entry_policy"]}
                    )
            finally:
                await source.close()
            await self.pause(10)

    async def sample(self, broker, config):
        try:
            inspection = await broker.inspect()
        except Exception as error:
            from .diagnostics import diagnostic

            client = getattr(broker, "ib", None)
            if client is None or not client.isConnected():
                raise
            details = diagnostic("inspect positions/open orders/executions", error)
            inspection = {
                "execution_ready": False,
                "verified": False,
                "diagnostic": details,
                "reason": details["reason"],
            }
            with self.runtime.store() as store:
                store.event("broker.inspection_failed", details)
        routing = getattr(config, "ibkr_paper_orders", False)
        status = "Connected · paper routing configured" if routing else "Connected · paper read-only"
        if self.autonomy is not None:
            await self.autonomy.engine.reconcile()
        if getattr(broker, "gateway_read_only", False):
            inspection["reason"] = (
                "Gateway blocks paper orders: Configure → Settings → API → Settings → disable Read-Only API"
            )
        elif self.autonomy is not None:
            inspection["reason"] = (
                ", ".join(self.autonomy.engine.store.get("state_reasons", [])) or "Broker state verified"
            )
        inspection["execution_ready"] = False
        self.publish(
            status,
            connected=True,
            read_only=not routing,
            inspection=inspection,
            execution_ready=inspection.get("execution_ready", False),
            data_status="Fetching ORCL / USDSEK",
        )
        try:
            market = await asyncio.wait_for(broker.market_data(), 25)
            if not fresh(market.timestamp, utcnow(), config.max_market_data_age_seconds) or not fresh(
                market.fx_timestamp, utcnow(), config.max_fx_age_seconds
            ):
                raise ValueError("stale quotes")
            from ..data import assess

            with self.runtime.store() as store:
                evidence = store.get("data:evidence")
            data_ready = evidence is not None and assess(evidence, config)["entry_policy"]["allowed"]
            inspection["execution_ready"] = bool(
                data_ready
                and routing
                and self.autonomy is not None
                and self.autonomy.engine.state == "READY"
                and candidates(market, config, utcnow())
            )
            self.publish(
                status,
                connected=True,
                read_only=not routing,
                inspection=inspection,
                execution_ready=inspection.get("execution_ready", False),
                market=market.model_dump(mode="json"),
                data_status="Live quotes received",
            )
            if self.autonomy is not None:
                self.autonomy.offer(market)
        except Exception as error:  # noqa: BLE001 -- redact broker messages, preserve verified positions
            self.publish(
                status,
                connected=True,
                read_only=not routing,
                inspection=inspection,
                execution_ready=inspection.get("execution_ready", False),
                data_status="Quotes unavailable or stale ("
                + type(error).__name__
                + "). Check market hours and data permissions.",
            )

    async def run(self):
        broker = None
        try:
            while not self.stop.is_set():
                config = self.runtime.settings()
                if self.discovery:
                    try:
                        discovered = await self.discovery(config)
                        if discovered and "accounts" in discovered:
                            self.publish(
                                "Choose a detected paper account in Connections",
                                accounts=discovered["accounts"],
                            )
                            await self.pause(3)
                            continue
                        if discovered:
                            current = config.model_dump(mode="json")
                            differs = any(
                                current[key] != ([value] if key == "account_allowlist" else value)
                                for key, value in discovered.items()
                            )
                            if differs:
                                self.runtime.dispatch("trading_settings", patch=discovered)
                                config = self.runtime.settings()
                    except Exception:  # noqa: BLE001 -- no broker payloads in the UI
                        self.publish("Paper account verification failed · check IBKR login and API settings")
                        await self.pause(3)
                        continue
                if not config.ibkr_account or config.ibkr_account not in config.account_allowlist:
                    self.publish("Waiting for Paper Trading login in IB Gateway / TWS")
                    await self.pause(3)
                    continue
                self.publish("Connecting to local paper Gateway")
                broker = self.factory(config)
                try:
                    await broker.connect()
                    if isinstance(broker, IbkrBroker):
                        self.data_task = asyncio.create_task(self.research(broker, config))
                    from ..autonomy import AutonomousSession

                    try:
                        self.autonomy = AutonomousSession(self.runtime, broker, config)
                    except RuntimeError:
                        with self.runtime.store() as store:
                            store.set(
                                "autonomy",
                                {"status": "BLOCKED", "reason": "Another execution owner is active"},
                            )
                    while not self.stop.is_set() and self.runtime.settings() == config:
                        await self.sample(broker, config)
                        await self.pause(2)
                except Exception as error:  # noqa: BLE001 -- broker exceptions may contain account data
                    self.publish(
                        "Disconnected · "
                        + type(error).__name__
                        + " · check Gateway login, paper port and account"
                    )
                finally:
                    if self.data_task is not None:
                        self.data_task.cancel()
                        await asyncio.gather(self.data_task, return_exceptions=True)
                        self.data_task = None
                    if self.autonomy is not None:
                        await self.autonomy.close()
                        self.autonomy = None
                    await broker.close()
                    broker = None
                await self.pause(3)
        finally:
            if broker is not None:
                await broker.close()
            self.publish("Disconnected · monitoring stopped")
            with self.runtime.store() as store:
                store.set("autonomy", {"status": "OFFLINE", "reason": "Broker observer stopped"})

"""Read-only connection supervisor, separate from verified execution state."""

import asyncio
import threading

from ..market import fresh
from ..models import utcnow
from .ibkr import IbkrBroker


class BrokerObserver:
    def __init__(self, runtime, factory=IbkrBroker):
        self.runtime, self.factory = runtime, factory
        self.stop = threading.Event()
        self.thread = None
        self.autonomy = None

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
                store.event("broker.connection", {"status": status, "read_only": True})

    async def pause(self, seconds):
        deadline = asyncio.get_running_loop().time() + seconds
        while not self.stop.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

    async def sample(self, broker, config):
        inspection = await broker.inspect()
        self.publish(
            "Connected · paper read-only",
            connected=True,
            inspection=inspection,
            data_status="Fetching ORCL / USDSEK",
        )
        try:
            market = await broker.market_data()
            if not fresh(market.timestamp, utcnow(), config.max_market_data_age_seconds) or not fresh(
                market.fx_timestamp, utcnow(), config.max_fx_age_seconds
            ):
                raise ValueError("stale quotes")
            self.publish(
                "Connected · paper read-only",
                connected=True,
                inspection=inspection,
                market=market.model_dump(mode="json"),
                data_status="Live quotes received",
            )
            if self.autonomy is not None:
                self.autonomy.offer(market)
        except Exception as error:  # noqa: BLE001 -- redact broker messages, preserve verified positions
            self.publish(
                "Connected · paper read-only",
                connected=True,
                inspection=inspection,
                data_status="Quotes unavailable or stale ("
                + type(error).__name__
                + "). Check market hours and data permissions.",
            )

    async def run(self):
        broker = None
        try:
            while not self.stop.is_set():
                config = self.runtime.settings()
                if not config.ibkr_account or config.ibkr_account not in config.account_allowlist:
                    self.publish("Setup required · enter and allowlist the paper account")
                    await self.pause(2)
                    continue
                self.publish("Connecting to local paper Gateway")
                broker = self.factory(config)
                try:
                    await broker.connect()
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

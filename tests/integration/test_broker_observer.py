import asyncio
from datetime import timedelta

from wayland.broker.observer import BrokerObserver
from wayland.config import app_root
from wayland.demo import fixture
from wayland.models import utcnow
from wayland.operator import OperatorRuntime


class Broker:
    def __init__(self, market=None):
        self.market = market
        self.closed = False

    async def inspect(self):
        return {
            "positions": [{"symbol": "ORCL", "con_id": 1, "quantity": "2", "average_cost": "100"}],
            "open_orders": [],
            "execution_count": 0,
        }

    async def market_data(self):
        if self.market is None:
            raise TimeoutError
        return self.market

    async def close(self):
        self.closed = True


def test_quotes_unavailable_preserves_positions_and_blocks_execution(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    observer = BrokerObserver(runtime)
    asyncio.run(observer.sample(Broker(), runtime.settings()))
    t = runtime.dispatch("snapshot")["trading"]
    assert t["brokerObservation"]["inspection"]["positions"][0]["quantity"] == "2"
    assert t["brokerObservation"]["connected"]
    assert not t["brokerObservation"]["execution_ready"]
    assert t["operating_state"] == "RECONCILING"
    assert t["portfolio"] is None


def test_stale_quotes_are_not_published_as_current(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    market, _, _ = fixture()
    market = market.model_copy(update={"timestamp": utcnow() - timedelta(minutes=2)})
    asyncio.run(BrokerObserver(runtime).sample(Broker(market), runtime.settings()))
    assert runtime.dispatch("snapshot")["trading"]["market"] is None


def test_live_quotes_display_without_marking_execution_ready(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    market, _, _ = fixture()
    asyncio.run(BrokerObserver(runtime).sample(Broker(market), runtime.settings()))
    t = runtime.dispatch("snapshot")["trading"]
    assert t["market"]["symbol"] == "ORCL"
    assert not t["brokerObservation"]["execution_ready"]


def test_no_account_does_not_contact_gateway(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    calls = []
    observer = BrokerObserver(runtime, factory=lambda config: calls.append(config))

    async def stop(_):
        observer.shutdown()

    observer.pause = stop
    asyncio.run(observer.run())
    assert not calls


def test_disconnect_reconnects_and_closes_each_client(tmp_path):
    from wayland.config import Settings

    runtime = OperatorRuntime(app_root(tmp_path))
    config = Settings(ibkr_account="DU-FIXTURE", account_allowlist=("DU-FIXTURE",))
    runtime.settings = lambda: config
    clients = []
    delays = []

    class ReconnectingBroker(Broker):
        async def connect(self):
            if len(clients) == 1:
                raise ConnectionError("sensitive raw broker error")

        async def inspect(self):
            observer.shutdown()
            return await super().inspect()

    def factory(settings):
        client = ReconnectingBroker()
        clients.append(client)
        return client

    observer = BrokerObserver(runtime, factory=factory)

    async def no_wait(seconds):
        delays.append(seconds)

    observer.pause = no_wait
    asyncio.run(observer.run())
    assert len(clients) == 2
    assert all(client.closed for client in clients)
    assert 3 in delays
    with runtime.store() as store:
        assert "sensitive raw broker error" not in str(store.recent())
        assert not store.get("broker:observation")["connected"]


def test_ui_marks_old_observation_disconnected(tmp_path):
    runtime = OperatorRuntime(app_root(tmp_path))
    with runtime.store() as store:
        store.set(
            "broker:observation",
            {
                "timestamp": (utcnow() - timedelta(minutes=2)).isoformat(),
                "connected": True,
                "status": "Connected",
            },
        )
    observation = runtime.dispatch("snapshot")["trading"]["brokerObservation"]
    assert not observation["connected"]
    assert "stale" in observation["status"]

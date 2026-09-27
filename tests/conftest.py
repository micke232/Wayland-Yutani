import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from wayland.audit import AuditStore
from wayland.broker.paper import PaperBroker
from wayland.config import Settings
from wayland.demo import fixture
from wayland.execution import ExecutionEngine


@pytest.fixture
def sample():
    return fixture()


@pytest.fixture
def settings():
    return Settings(account_allowlist=("SIMULATED-PAPER",))


@pytest.fixture
def engine(tmp_path, settings):
    store = AuditStore(tmp_path / "wayland.sqlite")
    broker = PaperBroker(tmp_path / "broker.sqlite")
    engine = ExecutionEngine(store, broker, settings)
    yield engine
    asyncio.run(engine.close())

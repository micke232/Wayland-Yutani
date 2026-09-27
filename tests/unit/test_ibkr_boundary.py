import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from wayland.broker.ibkr import IbkrBroker
from wayland.config import Settings
from wayland.models import TradingMode


def test_gateway_is_readonly_and_account_verified():
    client = SimpleNamespace(connectAsync=AsyncMock(), managedAccounts=lambda: ["DUR123"], disconnect=Mock())
    settings = Settings(ibkr_account="DUR123", account_allowlist=("DUR123",))
    broker = IbkrBroker(settings, client)
    asyncio.run(broker.connect())
    assert broker.verified
    assert client.connectAsync.call_args.kwargs["readonly"] is True
    with pytest.raises(RuntimeError, match="not enabled"):
        asyncio.run(broker.submit(None))


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": TradingMode.LIVE},
        {"live_enabled": True},
        {"ibkr_port": 4001},
        {"ibkr_host": "0.0.0.0"},
        {"ibkr_account": "U-LIVE"},
        {"account_allowlist": ()},
    ],
)
def test_invalid_live_or_unallowlisted_gateway_never_connects(patch):
    settings = Settings(ibkr_account="DUR123", account_allowlist=("DUR123",)).model_copy(update=patch)
    client = SimpleNamespace(connectAsync=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(IbkrBroker(settings, client).connect())
    client.connectAsync.assert_not_called()


def test_reported_account_mismatch_disconnects_without_verification():
    client = SimpleNamespace(connectAsync=AsyncMock(), managedAccounts=lambda: ["DU456"], disconnect=Mock())
    config = Settings(ibkr_account="DUR123", account_allowlist=("DUR123",))
    broker = IbkrBroker(config, client)
    with pytest.raises(ValueError):
        asyncio.run(broker.connect())
    assert not broker.verified
    client.disconnect.assert_called_once()

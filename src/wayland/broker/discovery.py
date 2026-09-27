"""Detect a local paper session without accepting live accounts or routing orders."""

import re
import socket


def observation_account(account: str) -> bool:
    """Accepted DU/DUR account identifiers for read-only observation, not execution proof."""
    return re.fullmatch(r"DUR?[0-9]+", account) is not None


async def discover(settings, client_factory=None):
    if (
        settings.live_enabled
        or settings.mode != "PAPER"
        or settings.ibkr_host not in ("127.0.0.1", "localhost", "::1")
    ):
        raise ValueError("Only local paper connections may be discovered")
    if client_factory is None:
        from ib_async import IB

        client_factory = IB
    for port in dict.fromkeys((settings.ibkr_port, 4002, 7497)):
        if port not in (4002, 7497):
            continue
        try:
            with socket.create_connection((settings.ibkr_host, port), timeout=0.2):
                pass
        except OSError:
            continue
        client = client_factory()
        try:
            await client.connectAsync(
                settings.ibkr_host, port, clientId=settings.ibkr_client_id, readonly=True, timeout=3
            )
            accounts = client.managedAccounts()
            if not accounts or any(not observation_account(a) for a in accounts):
                raise ValueError("Gateway did not report exclusively paper accounts")
            if settings.ibkr_account:
                if settings.ibkr_account not in accounts:
                    raise ValueError("Configured account does not match the logged-in paper session")
                chosen = settings.ibkr_account
            elif len(accounts) == 1:
                chosen = accounts[0]
            else:
                return {"accounts": accounts, "port": port}
            return {"ibkr_account": chosen, "account_allowlist": chosen, "ibkr_port": port}
        except (TimeoutError, OSError):
            continue
        finally:
            client.disconnect()
    return None

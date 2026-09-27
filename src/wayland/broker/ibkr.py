"""Read-only IB Gateway adapter. Real order routing remains gated pending paper verification."""

import asyncio
import math
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..models import (
    MarketSnapshot,
    OptionContract,
    OptionQuote,
    OrderCommand,
    PortfolioState,
    TradingMode,
    utcnow,
)
from .discovery import observation_account


def finite(value: Any) -> Decimal:
    if not isinstance(value, (int, float, Decimal)) or not math.isfinite(value):
        raise ValueError("market data unavailable; verify permissions and subscriptions")
    return Decimal(str(value))


def midpoint(bid: Any, ask: Any) -> Decimal:
    bid, ask = finite(bid), finite(ask)
    if bid <= 0 or ask < bid:
        raise ValueError("invalid or crossed market quote")
    return (bid + ask) / 2


class IbkrBroker:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings, self.ib = settings, client
        self.verified = False

    async def connect(self) -> None:
        s = self.settings
        if s.mode != TradingMode.PAPER or s.live_enabled:
            raise ValueError("LIVE remains locked")
        if (
            not observation_account(s.ibkr_account)
            or s.ibkr_account not in s.account_allowlist
            or s.ibkr_host not in ("127.0.0.1", "localhost", "::1")
            or s.ibkr_port not in (4002, 7497)
        ):
            raise ValueError("explicit allowlisted paper account and loopback paper endpoint required")
        if self.ib is None:
            from ib_async import IB

            self.ib = IB()
        await self.ib.connectAsync(
            s.ibkr_host,
            s.ibkr_port,
            clientId=s.ibkr_client_id,
            timeout=10,
            readonly=True,
            account=s.ibkr_account,
        )
        if s.ibkr_account not in self.ib.managedAccounts():
            self.ib.disconnect()
            raise ValueError("configured paper account was not reported by Gateway")
        self.verified = True

    async def inspect(self) -> dict:
        if not self.verified or not self.ib.isConnected():
            raise ConnectionError("Gateway disconnected; reconnect and verify account")
        # Explicitly request all open orders; client-specific cache is not account authority.
        positions, orders, executions = await asyncio.wait_for(
            asyncio.gather(
                self.ib.reqPositionsAsync(), self.ib.reqAllOpenOrdersAsync(), self.ib.reqExecutionsAsync()
            ),
            15,
        )
        account = self.settings.ibkr_account
        return {
            "paper_account_verified": True,
            "read_only": True,
            "positions": [
                {
                    "con_id": p.contract.conId,
                    "symbol": p.contract.symbol,
                    "quantity": str(p.position),
                    "average_cost": str(p.avgCost),
                }
                for p in positions
                if p.account == account
            ],
            "open_orders": [
                {
                    "order_id": t.order.orderId,
                    "perm_id": t.order.permId,
                    "order_ref": t.order.orderRef,
                    "status": t.orderStatus.status,
                }
                for t in orders
                if t.order.account == account
            ],
            "execution_count": sum(f.execution.acctNumber == account for f in executions),
            "execution_ready": False,
            "reason": "IBKR fill-history completeness and combo lifecycle require real paper verification",
        }

    async def market_data(self, symbol: str = "ORCL") -> MarketSnapshot:
        from ib_async import Forex, Stock

        if symbol not in self.settings.allowed_symbols or not self.verified or not self.ib.isConnected():
            raise ValueError("symbol or connection not verified")
        contracts = await asyncio.wait_for(
            self.ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"), Forex("USDSEK")), 15
        )
        if len(contracts) != 2 or any(c.conId <= 0 for c in contracts):
            raise ValueError("contract qualification failed")
        tickers = await asyncio.wait_for(self.ib.reqTickersAsync(*contracts), 15)
        if len(tickers) != 2 or any(t.marketDataType != 1 or t.time is None for t in tickers):
            raise ValueError("live market data permission or timestamp missing")
        stock, fx = tickers
        return MarketSnapshot(
            snapshot_id=str(uuid.uuid4()),
            symbol=symbol,
            timestamp=stock.time,
            price=midpoint(stock.bid, stock.ask),
            usd_sek=midpoint(fx.bid, fx.ask),
            fx_timestamp=fx.time,
        )

    async def option_chains(self, symbol: str = "ORCL") -> list[dict]:
        from ib_async import Stock

        if not self.verified or not self.ib.isConnected() or symbol not in self.settings.allowed_symbols:
            raise ValueError("symbol or connection not verified")
        qualified = await asyncio.wait_for(self.ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD")), 15)
        if len(qualified) != 1 or qualified[0].conId <= 0:
            raise ValueError("underlying qualification failed")
        chains = await asyncio.wait_for(
            self.ib.reqSecDefOptParamsAsync(symbol, "", "STK", qualified[0].conId), 15
        )
        return [
            {
                "exchange": c.exchange,
                "trading_class": c.tradingClass,
                "multiplier": c.multiplier,
                "expirations": sorted(c.expirations),
                "strikes": sorted(c.strikes),
            }
            for c in chains
        ]

    async def option_quote(self, symbol: str, expiry: date, strike: Decimal) -> OptionQuote:
        from ib_async import Option

        if not self.verified or not self.ib.isConnected() or symbol not in self.settings.allowed_symbols:
            raise ValueError("symbol or connection not verified")
        contract = Option(symbol, expiry.strftime("%Y%m%d"), float(strike), "P", "SMART", currency="USD")
        contracts = await asyncio.wait_for(self.ib.qualifyContractsAsync(contract), 15)
        if len(contracts) != 1 or contracts[0].conId <= 0:
            raise ValueError("option qualification failed")
        c = contracts[0]
        tickers = await asyncio.wait_for(self.ib.reqTickersAsync(c), 15)
        if len(tickers) != 1 or tickers[0].marketDataType != 1 or tickers[0].time is None:
            raise ValueError("live option data unavailable")
        ticker = tickers[0]

        def count(value):
            return (
                int(value)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
                else None
            )

        return OptionQuote(
            contract=OptionContract(
                con_id=c.conId,
                symbol=c.symbol,
                strike=Decimal(str(c.strike)),
                expiry=date.fromisoformat(c.lastTradeDateOrContractMonth[:8]),
                multiplier=int(c.multiplier),
                currency=c.currency,
            ),
            bid=finite(ticker.bid),
            ask=finite(ticker.ask),
            timestamp=ticker.time,
            volume=count(ticker.volume),
            open_interest=count(ticker.putOpenInterest),
        )

    async def snapshot(self) -> PortfolioState:
        await self.inspect()
        # Do not convert an incomplete recent execution window into an empty/verified portfolio.
        return PortfolioState(
            account=self.settings.ibkr_account,
            mode=TradingMode.PAPER,
            timestamp=utcnow(),
            healthy=self.ib.isConnected(),
            complete=False,
            pnl_date=utcnow().date(),
        )

    async def submit(self, command: OrderCommand):
        raise RuntimeError("IBKR execution not enabled: paper lifecycle verification is outstanding")

    async def cancel(self, broker_id: str) -> None:
        raise RuntimeError("IBKR execution not enabled: use Gateway to manage existing orders")

    async def close(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
        self.verified = False

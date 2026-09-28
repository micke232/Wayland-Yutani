"""Local paper Gateway adapter; live routing is hard-locked."""

import asyncio
import math
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..models import (
    InstrumentType,
    MarketSnapshot,
    OptionContract,
    OptionQuote,
    OrderCommand,
    PortfolioState,
    TradingMode,
    utcnow,
)
from .diagnostics import BrokerPermissionError, api_diagnostic
from .discovery import observation_account
from .routing import PaperRouting


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
    refresh_before_execution = True
    supported_instruments = (InstrumentType.LONG_PUT,)

    def __init__(self, settings: Settings, client: Any = None):
        self.settings, self.ib = settings, client
        self.verified = False
        self.routing: PaperRouting | None = None
        self.state_lock = asyncio.Lock()
        self.pnl: Any = None
        self.pnl_time: datetime | None = None
        self.gateway_read_only = False
        self.diagnostics: list[dict] = []
        self.read_only_at: datetime | None = None
        self.completed_future: Any = None
        self.chains_cache: dict[str, tuple[datetime, list[dict]]] = {}

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
        if hasattr(self.ib, "errorEvent"):
            self.ib.errorEvent += self.api_error
        await self.ib.connectAsync(
            s.ibkr_host,
            s.ibkr_port,
            clientId=s.ibkr_client_id,
            timeout=10,
            readonly=not s.ibkr_paper_orders,
            account=s.ibkr_account,
        )
        if s.ibkr_account not in self.ib.managedAccounts():
            self.ib.disconnect()
            raise ValueError("configured paper account was not reported by Gateway")
        self.verified = True

    def api_error(self, request_id, code, message, *args):
        entry = api_diagnostic(request_id, code, message)
        if entry:
            self.diagnostics.append(entry)
            self.diagnostics = self.diagnostics[-40:]
        if code == 321 and "read-only" in message.lower():
            self.gateway_read_only = True
            self.read_only_at = utcnow()
            if self.completed_future is not None and not self.completed_future.done():
                self.completed_future.set_exception(BrokerPermissionError(BrokerPermissionError.reason))

    def bind_store(self, store):
        self.routing = PaperRouting(self, store)

    async def read_state(self, completed=True):
        if not self.verified or not self.ib.isConnected():
            raise ConnectionError("Gateway disconnected")
        async with self.state_lock:
            if completed and self.read_only_at and (utcnow() - self.read_only_at).total_seconds() < 30:
                raise BrokerPermissionError(BrokerPermissionError.reason)
            if completed:
                self.gateway_read_only = False
            self.completed_future = asyncio.get_running_loop().create_future() if completed else None
            requests = [
                asyncio.ensure_future(call)
                for call in (
                    self.ib.reqPositionsAsync(),
                    self.ib.reqAllOpenOrdersAsync(),
                    self.ib.reqExecutionsAsync(),
                    self.ib.reqCompletedOrdersAsync(False) if completed else asyncio.sleep(0, result=[]),
                )
            ]
            group = asyncio.gather(*requests)
            try:
                if self.completed_future is not None:
                    done, _ = await asyncio.wait(
                        (group, self.completed_future), timeout=12, return_when=asyncio.FIRST_COMPLETED
                    )
                    if self.completed_future in done:
                        return self.completed_future.result()  # raises the sanitized denial
                    if group not in done:
                        raise TimeoutError()
                    return group.result()
                return await asyncio.wait_for(group, 12)
            finally:
                if self.completed_future is not None and not self.completed_future.done():
                    self.completed_future.cancel()
                if not group.done():
                    group.cancel()
                await asyncio.gather(group, return_exceptions=True)
                for request in requests:
                    if not request.done():
                        request.cancel()
                await asyncio.gather(*requests, return_exceptions=True)

    async def account_pnl(self):
        from ib_async import Forex

        if self.pnl is None:

            def updated(value):
                if value.account == self.settings.ibkr_account:
                    self.pnl_time = utcnow()

            self.ib.pnlEvent += updated
            self.pnl = self.ib.reqPnL(self.settings.ibkr_account)
        if self.pnl_time is None or (utcnow() - self.pnl_time).total_seconds() > 60:
            raise ValueError("fresh broker PnL is unavailable")
        currencies = {
            v.value
            for v in self.ib.accountValues(self.settings.ibkr_account)
            if v.tag == "$LEDGER-RealCurrency" and v.currency == "BASE"
        }
        if len(currencies) != 1:
            raise ValueError("account base currency is unverified")
        currency = currencies.pop()
        rate = Decimal(1)
        if currency != "SEK":
            if currency not in ("USD", "EUR", "GBP"):
                raise ValueError("unsupported PnL conversion currency")
            contracts = await asyncio.wait_for(self.ib.qualifyContractsAsync(Forex(currency + "SEK")), 5)
            quotes = await asyncio.wait_for(self.ib.reqTickersAsync(*contracts), 5)
            if (
                len(quotes) != 1
                or quotes[0].marketDataType != 1
                or quotes[0].time is None
                or not (0 <= (utcnow() - quotes[0].time).total_seconds() <= self.settings.max_fx_age_seconds)
            ):
                raise ValueError("account PnL FX quote unavailable")
            rate = midpoint(quotes[0].bid, quotes[0].ask)
        return finite(self.pnl.realizedPnL) * rate, finite(self.pnl.unrealizedPnL) * rate

    async def inspect(self) -> dict:
        if not self.verified or not self.ib.isConnected():
            raise ConnectionError("Gateway disconnected; reconnect and verify account")
        # Explicitly request all open orders; client-specific cache is not account authority.
        positions, orders, executions, _ = await self.read_state(completed=False)
        account = self.settings.ibkr_account
        return {
            "paper_account_verified": True,
            "read_only": not self.settings.ibkr_paper_orders,
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
            "reason": "Native long puts require verified orders, executions, positions and fresh account PnL; combos blocked",
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
        options = await self.selected_options(symbol, midpoint(stock.bid, stock.ask))
        return MarketSnapshot(
            snapshot_id=str(uuid.uuid4()),
            symbol=symbol,
            timestamp=stock.time,
            price=midpoint(stock.bid, stock.ask),
            usd_sek=midpoint(fx.bid, fx.ask),
            fx_timestamp=fx.time,
            options=options,
        )

    async def selected_options(self, symbol, price):
        # Small, bounded chain selection. Always retain contracts of owned positions.
        cached = self.chains_cache.get(symbol)
        if cached and (utcnow() - cached[0]).total_seconds() < 900:
            chains = cached[1]
        else:
            chains = await self.option_chains(symbol)
            self.chains_cache[symbol] = (utcnow(), chains)
        eligible = [c for c in chains if c["exchange"] == "SMART" and c["multiplier"] == "100"]
        if not eligible:
            return ()
        chain = eligible[0]
        expiries = [
            date.fromisoformat(e)
            for e in chain["expirations"]
            if self.settings.min_dte
            <= (date.fromisoformat(e) - utcnow().date()).days
            <= self.settings.max_dte
        ][:1]
        strikes = sorted(chain["strikes"], key=lambda k: abs(Decimal(str(k)) - price))[:3]
        selected = {(e, Decimal(str(k))) for e in expiries for k in strikes}
        if self.routing is not None:
            held = {
                p["position_id"]
                for p in self.routing.store.get("verified_portfolio", {}).get("positions", [])
            }
            for c in self.routing.commands().values():
                for leg in c.contracts:
                    if c.position_id in held and leg.symbol == symbol and leg.expiry >= utcnow().date():
                        selected.add((leg.expiry, leg.strike))
        if len(selected) > 16:
            raise ValueError("option monitoring capacity exceeded")
        results = await asyncio.gather(
            *(self.option_quote(symbol, expiry, strike) for expiry, strike in sorted(selected)),
            return_exceptions=True,
        )
        return tuple(q for q in results if isinstance(q, OptionQuote))

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
        if (
            c.right != "P"
            or c.currency != "USD"
            or c.multiplier != "100"
            or c.symbol != symbol
            or Decimal(str(c.strike)) != strike
            or date.fromisoformat(c.lastTradeDateOrContractMonth[:8]) != expiry
        ):
            raise ValueError("qualified option terms conflict")
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
        if self.routing is not None:
            return await self.routing.snapshot()
        await self.inspect()
        return PortfolioState(
            account=self.settings.ibkr_account,
            mode=TradingMode.PAPER,
            timestamp=utcnow(),
            healthy=self.ib.isConnected(),
            complete=False,
            pnl_date=utcnow().date(),
        )

    async def submit(self, command: OrderCommand):
        if self.routing is None or not self.settings.ibkr_paper_orders:
            raise RuntimeError("IBKR execution not enabled")
        return await self.routing.submit(command)

    async def cancel(self, broker_id: str) -> None:
        if self.routing is None or not self.settings.ibkr_paper_orders:
            raise RuntimeError("IBKR execution not enabled")
        await self.routing.cancel(broker_id)

    async def close(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
        self.verified = False

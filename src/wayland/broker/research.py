"""Order-free IBKR research endpoints. News is a distinct subscribed headline feed."""

import asyncio
import uuid
from datetime import timedelta

from ..data import DataUnavailable, number, stamp, technical
from ..models import utcnow
from .diagnostics import SafeDiagnostic
from .ibkr import finite, midpoint


class ResearchRequestError(RuntimeError, SafeDiagnostic):
    def __init__(self, operation, error):
        self.operation = operation
        self.reason = f"{operation}: {type(error).__name__}; see sanitized IBKR diagnostics for entitlement/connection errors"
        super().__init__(self.reason)


class NativeResearch:
    def __init__(self, broker):
        self.broker, self.ib, self.settings = broker, broker.ib, broker.settings
        self.tasks = {}
        self.qualification = None
        self.chain = None

    async def request(self, operation, call, timeout):
        try:
            return await asyncio.wait_for(call, timeout)
        except Exception as error:  # noqa: BLE001 -- never expose SDK error payloads
            raise ResearchRequestError(operation, error) from None

    async def once(self, key, call):
        if key not in self.tasks:
            self.tasks[key] = asyncio.create_task(call())
        return await asyncio.shield(self.tasks[key])

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def stock(self):
        async def get():
            from ib_async import Stock

            if not self.broker.verified or not self.ib.isConnected():
                raise ConnectionError()
            if "ORCL" not in self.settings.allowed_symbols:
                raise DataUnavailable("ORCL is not allowlisted")
            contracts = await self.request(
                "qualifyContractsAsync(ORCL)",
                self.ib.qualifyContractsAsync(Stock("ORCL", "SMART", "USD")),
                10,
            )
            if len(contracts) != 1 or contracts[0].conId <= 0:
                raise DataUnavailable("ORCL qualification returned no unique conId")
            self.qualification = {
                "status": "OK",
                "con_id": contracts[0].conId,
                "symbol": "ORCL",
                "source": "IBKR",
            }
            return contracts[0]

        return await self.once("stock", get)

    async def ticker(self):
        async def get():
            contract = await self.stock()
            rows = await self.request("reqTickersAsync(ORCL)", self.ib.reqTickersAsync(contract), 12)
            if len(rows) != 1 or rows[0].time is None:
                raise DataUnavailable("ORCL snapshot missing quote/timestamp; check market-data entitlements")
            return rows[0]

        return await self.once("ticker", get)

    async def quote(self):
        t = await self.ticker()
        try:
            bid, ask = finite(t.bid), finite(t.ask)
            price = midpoint(t.bid, t.ask)
        except ValueError:
            raise DataUnavailable(
                "ORCL bid/ask unavailable or crossed; check market-data entitlements"
            ) from None

        def optional(v):
            try:
                return str(number(v))
            except (ValueError, ArithmeticError):
                return None

        result = {
            "snapshot_id": str(uuid.uuid4()),
            "symbol": "ORCL",
            "timestamp": stamp(t.time).isoformat(),
            "bid": str(bid),
            "ask": str(ask),
            "last": optional(t.last),
            "price": str(price),
            "spread": str(ask - bid),
            "volume": optional(t.volume),
            "source": "IBKR",
            "timestamp_kind": "snapshot_receipt; exchange quote timestamp not supplied",
            "connected": self.ib.isConnected(),
            "live": t.marketDataType == 1,
            "data_type": {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}.get(
                t.marketDataType, "unknown"
            ),
            "market_session": "UNKNOWN",
            "usd_sek": None,
            "fx_timestamp": None,
        }
        # Session derives from broker contract schedules, never weekday heuristics.
        try:
            details = await asyncio.wait_for(self.ib.reqContractDetailsAsync(await self.stock()), 5)
            now = utcnow()
            if details:
                sessions = details[0].liquidSessions()
                covered = any(
                    s.start.date() <= now.astimezone(s.start.tzinfo).date() <= s.end.date() for s in sessions
                )
                result["market_session"] = (
                    "REGULAR"
                    if any(s.start <= now <= s.end for s in sessions)
                    else ("OUTSIDE_RTH" if covered else "UNKNOWN")
                )
                result["session_source"] = "IBKR liquidHours"
        except Exception as error:  # noqa: BLE001 -- preserve other provider data
            result["session_error"] = type(error).__name__
        try:
            from ib_async import Forex

            fx = await asyncio.wait_for(self.ib.qualifyContractsAsync(Forex("USDSEK")), 3)
            rows = await asyncio.wait_for(self.ib.reqTickersAsync(*fx), 3) if len(fx) == 1 else []
            if len(rows) == 1 and rows[0].time and rows[0].marketDataType == 1:
                result.update(
                    usd_sek=str(midpoint(rows[0].bid, rows[0].ask)),
                    fx_timestamp=stamp(rows[0].time).isoformat(),
                )
        except Exception as error:  # noqa: BLE001 -- preserve stock quote without FX
            result["fx_error"] = type(error).__name__
        return result

    async def history(self):
        rows = await self.request(
            "reqHistoricalDataAsync(ORCL,5mins,TRADES)",
            self.ib.reqHistoricalDataAsync(
                await self.stock(),
                endDateTime="",
                durationStr="3 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                timeout=15,
            ),
            18,
        )
        try:
            return technical(
                [
                    {
                        "timestamp": b.date,
                        "open": b.open,
                        "high": b.high,
                        "low": b.low,
                        "close": b.close,
                        "volume": b.volume,
                    }
                    for b in rows
                ],
                self.settings,
                utcnow(),
            )
        except ValueError:
            raise DataUnavailable(
                "OHLCV missing/invalid: require 20 closed 5-minute bars in latest session"
            ) from None

    async def options(self):
        stock = await self.stock()
        cached = self.broker.chains_cache.get("ORCL")
        if cached and (utcnow() - cached[0]).total_seconds() < 900:
            chains = cached[1]
        else:
            raw = await self.request(
                "reqSecDefOptParamsAsync(ORCL)",
                self.ib.reqSecDefOptParamsAsync("ORCL", "", "STK", stock.conId),
                10,
            )
            chains = [
                {
                    "exchange": c.exchange,
                    "trading_class": c.tradingClass,
                    "multiplier": c.multiplier,
                    "expirations": sorted(c.expirations),
                    "strikes": sorted(c.strikes),
                }
                for c in raw
            ]
            self.broker.chains_cache["ORCL"] = (utcnow(), chains)
        eligible = [c for c in chains if c["exchange"] == "SMART" and c["multiplier"] == "100"]
        self.chain = {
            "status": "OK" if eligible else "FAIL",
            "count": len(eligible),
            "reason": "Qualified SMART chains" if eligible else "Empty/unsupported options chain",
        }
        if not eligible:
            raise DataUnavailable("Empty/unsupported options chain")
        ticker = await self.ticker()
        price = midpoint(ticker.bid, ticker.ask)
        quotes = await self.broker.selected_options("ORCL", price)
        if not quotes:
            raise DataUnavailable(
                "Options chain exists but no usable qualified quotes within DTE/strike selection; check permissions"
            )
        return {
            "source": "IBKR",
            "timestamp": utcnow().isoformat(),
            "chain_count": len(eligible),
            "quotes": [q.model_dump(mode="json") for q in quotes],
        }

    async def news(self):
        providers = await self.request("reqNewsProvidersAsync", self.ib.reqNewsProvidersAsync(), 5)
        if not providers:
            raise DataUnavailable(
                "No IBKR news providers available; a separate news entitlement/feed is required"
            )
        now = utcnow()
        rows = await self.request(
            "reqHistoricalNewsAsync(ORCL)",
            self.ib.reqHistoricalNewsAsync(
                (await self.stock()).conId,
                "+".join(p.code for p in providers),
                now - timedelta(hours=24),
                now,
                50,
            ),
            15,
        )
        names = {p.code: p.name for p in providers}
        result = []
        for n in rows or []:
            # IBKR historical-news timestamps are UTC (documented SDK contract).
            from datetime import UTC

            time = n.time.replace(tzinfo=UTC) if n.time.tzinfo is None else n.time
            if (
                n.providerCode in names
                and n.articleId
                and n.headline
                and 0 <= (now - time).total_seconds() <= 86400
            ):
                result.append(
                    {
                        "symbol": "ORCL",
                        "timestamp": time.isoformat(),
                        "retrieved_at": now.isoformat(),
                        "provider": n.providerCode,
                        "provider_name": names[n.providerCode],
                        "article_id": n.articleId,
                        "source_reference": f"IBKR news:{n.providerCode}:{n.articleId}",
                        "headline": n.headline,
                        "content_type": "headline_only",
                    }
                )
        if not result:
            raise DataUnavailable(
                "No sourced ORCL headlines in last 24h; news unavailable, not evidence of no catalysts"
            )
        return result

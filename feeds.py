#!/usr/bin/env python3
"""Pluggable market-data feeds for the paper-trading engine.

Every feed speaks the same language -- historical ``Bar``s and a latest
``Quote`` -- so strategies never know (or care) whether their prices came
from Yahoo Finance, Polymarket's public API, or a CSV on disk. Fail-soft:
a feed that cannot produce data returns empty/None and the engine simply
skips that symbol, loudly, instead of trading on stale air.

Paper only: feeds are read-only. Nothing here can place an order.
"""

from __future__ import annotations

import csv
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


@dataclass
class Bar:
    ts: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Quote:
    ts: datetime
    symbol: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class MarketDataFeed(ABC):
    """Common contract for every market-data source."""

    name: str = "base"
    # What this feed provides: "stocks" | "options" | "predictions".
    # The engine only runs strategies whose asset_classes include it.
    asset_class: str = "stocks"

    @abstractmethod
    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        """Daily bars, oldest first. Empty list when unavailable."""

    @abstractmethod
    def latest_quote(self, symbol: str) -> Optional[Quote]:
        """Best current quote, or None when unavailable."""


class YahooFeed(MarketDataFeed):
    """US equities (and anything else Yahoo covers) via yfinance. Free, no key."""

    name = "yahoo"

    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        try:
            import yfinance as yf
        except ImportError:
            return []
        try:
            df = yf.download(symbol, start=start.isoformat(),
                             end=end.isoformat(), progress=False,
                             auto_adjust=False)
        except Exception:
            return []
        if df is None or df.empty:
            return []
        bars: list[Bar] = []
        for ts, row in df.iterrows():
            try:
                bars.append(Bar(
                    ts=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    symbol=symbol,
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0)),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return bars

    def latest_quote(self, symbol: str) -> Optional[Quote]:
        try:
            import yfinance as yf
            t = yf.Ticker(symbol)
            px = t.fast_info.get("last_price")
            if px is None:
                hist = t.history(period="1d")
                if hist.empty:
                    return None
                px = float(hist["Close"].iloc[-1])
            px = float(px)
        except Exception:
            return None
        spread = px * 0.0001  # ~1bp synthetic spread off the last print
        return Quote(ts=datetime.now(), symbol=symbol,
                     bid=px - spread / 2, ask=px + spread / 2)


class PolymarketFeed(MarketDataFeed):
    """Prediction-market prices via Polymarket's *public* Gamma API. No key needed.

    ``symbol`` is a market slug (e.g. ``"btc-up-or-down-january-31-5pm-et"``).
    Quotes are the YES-token price of outcome index 0. History comes from the
    CLOB price-history endpoint on a best-effort basis; it returns [] rather
    than raising when the market cannot be resolved.
    """

    name = "polymarket"
    asset_class = "predictions"
    GAMMA = "https://gamma-api.polymarket.com"
    CLOB = "https://clob.polymarket.com"

    def _get(self, url: str, params: dict | None = None, timeout: int = 15):
        import requests
        r = requests.get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _resolve(self, slug: str) -> Optional[dict]:
        try:
            data = self._get(f"{self.GAMMA}/markets", {"slug": slug})
            return data[0] if data else None
        except Exception:
            return None

    def latest_quote(self, symbol: str) -> Optional[Quote]:
        m = self._resolve(symbol)
        if not m:
            return None
        try:
            prices = json.loads(m["outcomePrices"])
            px = float(prices[0])
        except (KeyError, ValueError, TypeError, IndexError):
            return None
        spread = max(px * 0.002, 0.001)
        return Quote(ts=datetime.now(), symbol=symbol,
                     bid=max(px - spread / 2, 0.001),
                     ask=min(px + spread / 2, 0.999))

    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        m = self._resolve(symbol)
        if not m:
            return []
        condition_id = m.get("conditionId")
        if not condition_id:
            return []
        try:
            pts = self._get(
                f"{self.CLOB}/prices-history",
                {"market": condition_id,
                 "startTs": int(datetime(start.year, start.month, start.day).timestamp()),
                 "endTs": int(datetime(end.year, end.month, end.day).timestamp()),
                 "fidelity": 60},
            )
        except Exception:
            return []
        bars: list[Bar] = []
        for p in pts.get("history", []):
            try:
                ts = datetime.fromtimestamp(int(p["t"]))
                px = float(p["p"])
                bars.append(Bar(ts=ts, symbol=symbol, open=px, high=px,
                                low=px, close=px, volume=0.0))
            except (KeyError, ValueError, TypeError):
                continue
        return sorted(bars, key=lambda b: b.ts)


class PolymarketUSFeed(MarketDataFeed):
    """Polymarket US (polymarket.us): the CFTC-regulated, fiat (USD) venue.

    A *separate product* from polymarket.com -- different hosts, auth, and
    SDKs; nothing is carried over from the .com feed. Public market data via
    https://gateway.polymarket.us, no key needed.

    ``symbol`` is a market slug. Quotes track outcome index 0: top-of-book is
    used when the spread is sane (< 50c), otherwise the ``outcomePrices`` mid
    with a synthetic spread (US books are often extremely thin).

    Limitation: the US gateway exposes no public price-history endpoint, so
    ``history()`` returns []. This feed is for live/paper-forward use;
    backtest US markets through the CSV feed.
    """

    name = "polymarket_us"
    asset_class = "predictions"
    GATEWAY = "https://gateway.polymarket.us"

    def _get(self, path: str, params: dict | None = None, timeout: int = 15):
        import requests
        r = requests.get(self.GATEWAY + path, params=params or {},
                         timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _market(self, slug: str) -> Optional[dict]:
        try:
            ms = self._get("/v1/markets", {"slug": slug}).get("markets", [])
            return ms[0] if ms else None
        except Exception:
            return None

    def _book(self, slug: str) -> Optional[dict]:
        try:
            return self._get(f"/v1/markets/{slug}/book").get("marketData")
        except Exception:
            return None

    @staticmethod
    def _px(entry: dict) -> Optional[float]:
        try:
            return float(entry["px"]["value"])
        except (KeyError, TypeError, ValueError):
            return None

    def latest_quote(self, symbol: str) -> Optional[Quote]:
        now = datetime.now()
        book = self._book(symbol)
        if book:
            bids = [p for p in (self._px(b) for b in book.get("bids", []))
                    if p is not None]
            offers = [p for p in (self._px(o) for o in book.get("offers", []))
                      if p is not None]
            if bids and offers:
                bid, ask = max(bids), min(offers)
                if 0 < bid < ask < 1 and (ask - bid) < 0.50:
                    return Quote(ts=now, symbol=symbol, bid=bid, ask=ask)
        m = self._market(symbol)
        if m:
            try:
                px = float(json.loads(m["outcomePrices"])[0])
                spread = max(px * 0.02, 0.005)
                return Quote(ts=now, symbol=symbol,
                             bid=max(px - spread / 2, 0.001),
                             ask=min(px + spread / 2, 0.999))
            except (KeyError, ValueError, TypeError, IndexError):
                pass
        return None

    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        return []  # no public history endpoint on the US gateway


class CsvFeed(MarketDataFeed):
    """Bars from ``<SYMBOL>.csv`` files: ts,symbol,open,high,low,close,volume.

    The backtesting workhorse -- deterministic, offline, and the easiest way
    to feed the engine synthetic or recorded data.
    """

    name = "csv"

    def __init__(self, directory: str, asset_class: str = "stocks"):
        self.directory = directory
        self.asset_class = asset_class

    def _path(self, symbol: str) -> str:
        import os
        return os.path.join(self.directory, f"{symbol}.csv")

    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        bars: list[Bar] = []
        try:
            with open(self._path(symbol), newline="") as f:
                for row in csv.DictReader(f):
                    ts = datetime.fromisoformat(row["ts"])
                    if not (start <= ts.date() <= end):
                        continue
                    bars.append(Bar(
                        ts=ts, symbol=row.get("symbol", symbol),
                        open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        volume=float(row.get("volume", 0.0)),
                    ))
        except (FileNotFoundError, KeyError, ValueError):
            return []
        return sorted(bars, key=lambda b: b.ts)

    def latest_quote(self, symbol: str) -> Optional[Quote]:
        bars = self.history(symbol, date(1970, 1, 1), date(2100, 1, 1))
        if not bars:
            return None
        px = bars[-1].close
        spread = px * 0.0001
        return Quote(ts=bars[-1].ts, symbol=symbol,
                     bid=px - spread / 2, ask=px + spread / 2)


def build_feed(kind: str, **kwargs) -> MarketDataFeed:
    kinds = {"yahoo": YahooFeed, "polymarket": PolymarketFeed,
             "polymarket_us": PolymarketUSFeed, "csv": CsvFeed}
    if kind not in kinds:
        raise ValueError(f"unknown feed {kind!r}; choose from {sorted(kinds)}")
    return kinds[kind](**kwargs)

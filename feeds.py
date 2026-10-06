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

import base64
import csv
import json
import os
import socket
import ssl as _ssl
import struct
import threading
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional


@dataclass
class Bar:
    ts: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    # Live top-of-book when the feed provides it (e.g. polymarket_ws).
    # The engine fills buys at the ask and sells at the bid when present;
    # historical bars leave these None and fills use close +/- slippage.
    bid: Optional[float] = None
    ask: Optional[float] = None


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

    def settlements(self, symbols: list[str], today: date) -> list[tuple[str, float]]:
        """(symbol, payout_per_share) for held positions whose market has
        resolved. Empty by default; prediction-market feeds override it so
        the engine can cash out expired markets."""
        return []


# ---------------------------------------------------------------------------
# Gamma resolution helper (shared by the prediction feeds)
# ---------------------------------------------------------------------------

# slug -> (payout_or_None, checked_today); decisive payouts stick permanently
# (checked_today = date.max), undecided ones are re-checked when `today`
# advances -- a market can resolve months after a backtest's early steps.
_gamma_payout_cache: dict[str, tuple[Optional[float], date]] = {}


def _parse_payout(market: dict, today: date) -> Optional[float]:
    """1.0 if the market resolved Yes, 0.0 if No, None if not (yet) decisive.

    Pure function of a Gamma market dict -- unit-testable without network.
    A payout is reported only for *expired* markets with decisive
    outcomePrices; anything ambiguous is left marked at last price.
    """
    try:
        end_raw = market.get("endDate")
        end_d = None
        if end_raw:
            end_d = datetime.fromisoformat(
                str(end_raw).replace("Z", "+00:00")).date()
        if end_d is not None and end_d >= today:
            return None
        prices = json.loads(market.get("outcomePrices") or "[]")
        y, n = float(prices[0]), float(prices[1])
    except (TypeError, ValueError, IndexError):
        return None
    if y > 0.999 and n < 0.001:
        return 1.0
    if n > 0.999 and y < 0.001:
        return 0.0
    return None


def gamma_yes_payout(slug: str, today: date) -> Optional[float]:
    """Decisive Yes-token payout for a market slug, or None.

    Cached per (slug, today): decisive resolutions stick permanently,
    ambiguous ones are re-fetched when `today` moves (resolution can land
    long after expiry).
    """
    hit = _gamma_payout_cache.get(slug)
    if hit is not None:
        payout, checked = hit
        if payout is not None or checked == today:
            return payout
    try:
        import requests
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"slug": slug}, timeout=15)
        r.raise_for_status()
        data = r.json()
        payout = _parse_payout(data[0], today) if data else None
        _gamma_payout_cache[slug] = (
            payout, date.max if payout is not None else today)
        return payout
    except Exception:
        return None


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

    def settlements(self, symbols: list[str], today: date) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for s in symbols:
            slug, _, side = s.partition(":")
            payout = gamma_yes_payout(slug, today)
            if payout is None:
                continue
            if side.strip().upper() == "NO":
                payout = 1.0 - payout
            out.append((s, payout))
        return out


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


# ---------------------------------------------------------------------------
# Minimal WebSocket client (stdlib only)
# ---------------------------------------------------------------------------

def _proxy_parts() -> Optional[tuple]:
    """(host, port, user, password) from *_proxy env vars, or None."""
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
                "all_proxy", "ALL_PROXY"):
        raw = os.environ.get(var)
        if not raw:
            continue
        u = urllib.parse.urlparse(raw)
        if u.hostname:
            return u.hostname, u.port or 3128, u.username, u.password
    return None


class _WSClient:
    """Bare-minimum RFC 6455 client: proxy CONNECT, TLS, masked text frames,
    ping/pong, and an application-level PING heartbeat. Just enough for
    Polymarket's market channel -- not a general-purpose library."""

    def __init__(self, url: str):
        u = urllib.parse.urlparse(url)
        if u.scheme != "wss":
            raise ValueError("only wss:// is supported")
        self.host = u.hostname
        self.port = u.port or 443
        self.path = u.path or "/"
        self.sock: Optional[socket.socket] = None

    def connect(self, timeout: int = 15) -> None:
        proxy = _proxy_parts()
        if proxy:
            phost, pport, puser, ppass = proxy
            raw = socket.create_connection((phost, pport), timeout=timeout)
            # Try without proxy credentials first: some egress proxies
            # 407 requests that *carry* (stale) credentials yet allow the
            # same source IP through unauthenticated.
            for with_auth in (False, True):
                auth = ""
                if with_auth and puser:
                    tok = base64.b64encode(
                        f"{puser}:{ppass or ''}".encode()).decode()
                    auth = f"Proxy-Authorization: Basic {tok}\r\n"
                raw.sendall(
                    f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                    f"Host: {self.host}:{self.port}\r\n{auth}\r\n".encode())
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = raw.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                status = resp.split(b"\r\n", 1)[0]
                if b" 200 " in status:
                    break
                if b" 407 " in status and not with_auth and puser:
                    continue
                raw.close()
                raise ConnectionError(f"proxy CONNECT failed: {status!r}")
            sock = raw
        else:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=timeout)
        sock = _ssl.create_default_context().wrap_socket(
            sock, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                break
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            sock.close()
            raise ConnectionError(f"websocket upgrade failed: {resp[:80]!r}")
        self.sock = sock

    def send_text(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + mask +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    def _recv_text(self) -> Optional[str]:
        """Next complete text message; None on clean close."""
        frags: list[bytes] = []
        while True:
            h = self._recv_exact(2)
            fin, op = h[0] & 0x80, h[0] & 0x0F
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(ln) if ln else b""
            if op == 0x8:
                return None
            if op == 0x9:  # ping -> pong
                self._send_ctrl(0xA, payload)
                continue
            if op == 0xA:  # pong
                continue
            if op in (0x1, 0x0):
                frags.append(payload)
                if fin:
                    return b"".join(frags).decode("utf-8", "replace")

    def _send_ctrl(self, op: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        hdr = bytes([0x80 | op, 0x80 | len(payload)]) + mask
        self.sock.sendall(hdr + bytes(b ^ mask[i % 4]
                                      for i, b in enumerate(payload)))

    def recv_loop(self, on_text: Callable[[str], None],
                  stop: threading.Event) -> None:
        """Block reading messages until stop is set, the socket closes, or
        nothing arrives for 120s (stale connection -> reconnect)."""
        self.sock.settimeout(1.0)
        last_rx = time.time()
        while not stop.is_set():
            try:
                msg = self._recv_text()
            except socket.timeout:
                if time.time() - last_rx > 120:
                    raise ConnectionError("websocket stale")
                continue
            if msg is None:
                raise ConnectionError("websocket closed by server")
            last_rx = time.time()
            if msg == "PONG":
                continue
            on_text(msg)

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


class PolymarketWSFeed(MarketDataFeed):
    """Live order-book feed over Polymarket's public CLOB WebSocket.

    ``wss://ws-subscriptions-clob.polymarket.com/ws/market`` -- no API key.
    Keeps a real-time book per token (snapshot + deltas) and serves
    ``latest_quote()`` from the live top-of-book instead of a polled REST
    quote, so spreads, depth and prints are the market's own, tick by tick.

    ``symbol`` is a market slug for the YES token, or ``"slug:NO"`` for the
    NO token (matches the YES=outcome-0 convention of PolymarketFeed).

    ``history()`` is [] -- a socket is live-only by nature. Use
    ``collect_ticks()`` to record a session, then backtest the recording
    through the CSV feed.
    """

    name = "polymarket_ws"
    asset_class = "predictions"
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    GAMMA = "https://gamma-api.polymarket.com"

    def __init__(self, symbols: Optional[list] = None):
        self._symbols = list(symbols or [])
        self._books: dict = {}        # asset_id -> {bids, asks, last, ts}
        self._asset_of: dict = {}     # symbol -> asset_id
        self._wanted: set = set()     # asset_ids to (re)subscribe
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws: Optional[_WSClient] = None
        self._listeners: list = []    # fn(symbol, top_dict)
        self._last_top: dict = {}     # asset_id -> (bid, ask) last notified

    # -- connection management -------------------------------------------
    def _ensure_started(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="polymarket-ws")
            self._thread.start()
        for s in self._symbols:
            self._subscribe_symbol(s)

    def _worker(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = _WSClient(self.WS_URL)
                ws.connect()
                with self._lock:
                    self._ws = ws
                    wanted = sorted(self._wanted)
                if wanted:
                    ws.send_text(json.dumps({"type": "market",
                                             "assets_ids": wanted}))
                    print(f"  [ws] connected, subscribed to {len(wanted)} token(s)")
                else:
                    print("  [ws] connected (no tokens wanted yet)")
                hb = threading.Thread(target=self._heartbeat, args=(ws,),
                                      daemon=True)
                hb.start()
                ws.recv_loop(self._on_text, self._stop)
                backoff = 1.0
            except Exception as e:
                if not self._stop.is_set():
                    print(f"  [ws] dropped ({e}); reconnecting in {backoff:g}s")
            finally:
                with self._lock:
                    self._ws = None
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    def _heartbeat(self, ws: _WSClient) -> None:
        # Application-level keepalive: the market channel expects a "PING"
        # text frame every ~10s and answers "PONG".
        while not self._stop.is_set():
            time.sleep(10)
            try:
                ws.send_text("PING")
            except Exception:
                break

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws:
            ws.close()

    # -- subscription -----------------------------------------------------
    def _resolve_token(self, symbol: str) -> Optional[str]:
        slug, _, side = symbol.partition(":")
        idx = 1 if side.strip().upper() == "NO" else 0
        try:
            import requests
            r = requests.get(f"{self.GAMMA}/markets", params={"slug": slug},
                             timeout=15)
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            tids = json.loads(data[0].get("clobTokenIds", "[]"))
            return tids[idx] if idx < len(tids) else None
        except Exception:
            return None

    def _subscribe_symbol(self, symbol: str) -> None:
        with self._lock:
            if symbol in self._asset_of:
                return
        aid = self._resolve_token(symbol)
        if not aid:
            return
        with self._lock:
            self._asset_of[symbol] = aid
            new = aid not in self._wanted
            self._wanted.add(aid)
            ws = self._ws
        # The channel accepts subscribe deltas without reconnecting.
        if new and ws is not None:
            try:
                ws.send_text(json.dumps({"assets_ids": [aid],
                                         "operation": "subscribe"}))
            except Exception:
                pass  # the worker (re)subscribes the full wanted set

    # -- message handling ---------------------------------------------------
    def _on_text(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        for e in (data if isinstance(data, list) else [data]):
            if not isinstance(e, dict) or "asset_id" not in e:
                continue
            aid = e["asset_id"]
            if "changes" in e:
                self._apply_delta(aid, e["changes"])
            elif any(k in e for k in ("bids", "asks", "buys", "sells")):
                self._apply_snapshot(aid, e)
            elif e.get("event_type") == "last_trade_price":
                self._apply_last(aid, e)

    @staticmethod
    def _levels(entries) -> dict:
        out = {}
        for lv in entries or []:
            try:
                out[float(lv["price"])] = float(lv["size"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _ts(raw) -> Optional[datetime]:
        try:
            return datetime.fromtimestamp(int(raw) / 1000.0)
        except (TypeError, ValueError):
            return None

    def _apply_snapshot(self, aid: str, e: dict) -> None:
        bids = self._levels(e.get("bids") or e.get("buys"))
        asks = self._levels(e.get("asks") or e.get("sells"))
        with self._lock:
            b = self._books.setdefault(
                aid, {"bids": {}, "asks": {}, "last": None, "ts": None})
            b["bids"], b["asks"] = bids, asks
            b["ts"] = self._ts(e.get("timestamp")) or datetime.now()
        self._notify(aid)

    def _apply_delta(self, aid: str, changes) -> None:
        with self._lock:
            b = self._books.setdefault(
                aid, {"bids": {}, "asks": {}, "last": None, "ts": None})
            for ch in changes or []:
                try:
                    px, sz = float(ch["price"]), float(ch["size"])
                except (KeyError, TypeError, ValueError):
                    continue
                side = b["bids"] if ch.get("side") == "BUY" else b["asks"]
                if sz == 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
            b["ts"] = datetime.now()
        self._notify(aid)

    def _apply_last(self, aid: str, e: dict) -> None:
        try:
            px = float(e["price"])
        except (KeyError, TypeError, ValueError):
            return
        with self._lock:
            b = self._books.setdefault(
                aid, {"bids": {}, "asks": {}, "last": None, "ts": None})
            b["last"] = px
        self._notify(aid)

    def _top(self, aid: str) -> Optional[dict]:
        b = self._books.get(aid)
        if not b or not b["bids"] or not b["asks"]:
            return None
        bid, ask = max(b["bids"]), min(b["asks"])
        return {"bid": bid, "ask": ask,
                "bid_size": b["bids"][bid], "ask_size": b["asks"][ask],
                "last": b["last"], "ts": b["ts"] or datetime.now()}

    def _notify(self, aid: str) -> None:
        with self._lock:
            top = self._top(aid)
            if not top:
                return
            key = (top["bid"], top["ask"])
            if self._last_top.get(aid) == key:
                return
            self._last_top[aid] = key
            # symbol lookup needs the reverse map; rebuild cheaply
            sym = next((s for s, a in self._asset_of.items() if a == aid),
                       aid)
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(sym, top)
            except Exception:
                pass

    # -- MarketDataFeed interface -------------------------------------------
    def latest_quote(self, symbol: str) -> Optional[Quote]:
        self._ensure_started()
        self._subscribe_symbol(symbol)
        deadline = time.time() + 10
        while time.time() < deadline:
            with self._lock:
                aid = self._asset_of.get(symbol)
                top = self._top(aid) if aid else None
                last = (self._books.get(aid) or {}).get("last") if aid else None
            if top and top["bid"] > 0 and top["ask"] > top["bid"]:
                return Quote(ts=top["ts"], symbol=symbol,
                             bid=top["bid"], ask=top["ask"])
            if last:
                spread = max(last * 0.01, 0.002)
                return Quote(ts=datetime.now(), symbol=symbol,
                             bid=max(last - spread / 2, 0.001),
                             ask=min(last + spread / 2, 0.999))
            time.sleep(0.2)
        return None

    def history(self, symbol: str, start: date, end: date) -> list[Bar]:
        return []  # live-only; record with collect_ticks(), backtest via CSV

    def settlements(self, symbols: list[str], today: date) -> list[tuple[str, float]]:
        # Same Gamma resolution source as PolymarketFeed; ":NO" suffix flips.
        out: list[tuple[str, float]] = []
        for s in symbols:
            slug, _, side = s.partition(":")
            payout = gamma_yes_payout(slug, today)
            if payout is None:
                continue
            if side.strip().upper() == "NO":
                payout = 1.0 - payout
            out.append((s, payout))
        return out

    # -- tick recording -------------------------------------------------------
    def collect_ticks(self, symbols: list, seconds: float,
                     out_csv: str) -> tuple:
        """Record top-of-book ticks for ``seconds`` and write them to CSV.

        Columns: ts,symbol,bid,ask,bid_size,ask_size,last -- one row per
        top-of-book change. Resample to bars (e.g. with pandas) and backtest
        through the CSV feed.
        """
        self._ensure_started()
        for s in symbols:
            self._subscribe_symbol(s)
        rows: list = []

        def _listen(sym: str, top: dict) -> None:
            rows.append({"ts": top["ts"].isoformat(), "symbol": sym,
                         "bid": top["bid"], "ask": top["ask"],
                         "bid_size": top["bid_size"],
                         "ask_size": top["ask_size"],
                         "last": top["last"] if top["last"] is not None
                         else ""})

        with self._lock:
            self._listeners.append(_listen)
        try:
            time.sleep(seconds)
        finally:
            with self._lock:
                if _listen in self._listeners:
                    self._listeners.remove(_listen)
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "symbol", "bid", "ask",
                                              "bid_size", "ask_size", "last"])
            w.writeheader()
            w.writerows(rows)
        return out_csv, len(rows)


class CsvFeed(MarketDataFeed):
    """Bars from ``<SYMBOL>.csv`` files: ts,symbol,open,high,low,close,volume.

    The backtesting workhorse -- deterministic, offline, and the easiest way
    to feed the engine synthetic or recorded data. Files may carry optional
    ``bid``/``ask`` columns (as written by ``resample.py``); when present the
    engine's fills cross that spread exactly like live quotes.
    """

    name = "csv"

    def __init__(self, directory: str, asset_class: str = "stocks"):
        self.directory = directory
        self.asset_class = asset_class

    def _path(self, symbol: str) -> str:
        import os
        return os.path.join(self.directory, f"{symbol}.csv")

    @staticmethod
    def _opt_float(row: dict, key: str) -> Optional[float]:
        try:
            return float(row[key])
        except (KeyError, TypeError, ValueError):
            return None

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
                        bid=self._opt_float(row, "bid"),
                        ask=self._opt_float(row, "ask"),
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
             "polymarket_us": PolymarketUSFeed,
             "polymarket_ws": PolymarketWSFeed, "csv": CsvFeed}
    if kind not in kinds:
        raise ValueError(f"unknown feed {kind!r}; choose from {sorted(kinds)}")
    return kinds[kind](**kwargs)

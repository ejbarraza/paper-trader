#!/usr/bin/env python3
"""Strategy loops for the paper-trading engine.

Every strategy speaks the same interface: it sees one bar at a time plus a
read-only ``Ctx`` (cash, equity, positions, recent closes) and emits zero or
more ``Signal``s. The engine -- never the strategy -- decides whether a signal
may trade, via the shared risk arbiter. Strategies are deliberately dumb
about risk; that is the arbiter's job.

Ships with three loops:
- ``VrpPutSellingStrategy`` -- sells the scanner's top-VRP cash-secured puts.
- ``MomentumStrategy`` -- golden/death-cross trend following on spot.
- ``MeanReversionStrategy`` -- z-score fade on spot, long or short.
"""

from __future__ import annotations

import csv
import glob
import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from feeds import Bar
from pricing import bs_put_price


@dataclass
class Signal:
    strategy: str
    symbol: str
    action: str  # "buy" | "sell" | "sell_put" | "buy_put_close"
    quantity: float  # shares, or contracts for put actions
    strike: Optional[float] = None
    expiry: Optional[date] = None
    limit_price: Optional[float] = None
    reduce_only: bool = False  # exits bypass the arbiter's kill-switches
    note: str = ""
    meta: dict = field(default_factory=dict)  # strategy-private extras (e.g. entry IV)


@dataclass
class Ctx:
    """Read-only view the engine hands each strategy per bar."""
    cash: float
    equity: float
    today: date
    positions: dict  # engine position keys -> qty
    closes: dict[str, list[float]]  # symbol -> closes ending at current bar
    put_details: dict  # ("put",symbol,strike,expiry_iso) -> {"iv","premium","entry_ts"}


class Strategy(ABC):
    name: str = "base"
    # Asset classes this loop can trade: "stocks" | "options" | "predictions".
    # The engine only runs a strategy on a feed whose asset class is in here.
    asset_classes: frozenset[str] = frozenset({"stocks"})
    blurb: str = ""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: Ctx) -> list[Signal]:
        """Inspect one bar, emit zero or more signals."""


# --------------------------------------------------------------------------
# 1. VRP cash-secured put selling -- wired to the options scanner's output
# --------------------------------------------------------------------------

class VrpPutSellingStrategy(Strategy):
    """Sells the scanner's top-VRP puts and manages them to expiry.

    Reads ``<TICKER>_csp.csv`` files written by the options scanner, opens the
    top-N by VRP when flat, takes profit at ``profit_take_frac`` of premium,
    and otherwise holds to ``min_dte_to_close``. Marks come from Black-Scholes
    with entry-time IV (documented assumption: no historical chains).
    """

    name = "vrp_puts"
    asset_classes = frozenset({"options"})
    blurb = "Sells the scanner's top-VRP cash-secured puts"

    def __init__(self, scanner_dir: str, top_n: int = 3,
                 dte_min: int = 30, dte_max: int = 45, min_vrp: float = 0.0,
                 profit_take_frac: float = 0.5, min_dte_to_close: int = 7,
                 risk_free: float = 0.04, reentry_days: int = 7):
        self.scanner_dir = scanner_dir
        self.top_n = top_n
        self.dte_min = dte_min
        self.dte_max = dte_max
        self.min_vrp = min_vrp
        self.profit_take_frac = profit_take_frac
        self.min_dte_to_close = min_dte_to_close
        self.risk_free = risk_free
        self.reentry_days = reentry_days
        self._cands: dict[str, list[dict]] = {}
        self._last_entry: dict[str, date] = {}
        self._load()

    def _load(self) -> None:
        for path in glob.glob(os.path.join(self.scanner_dir, "*_csp.csv")):
            symbol = os.path.basename(path)[: -len("_csp.csv")]
            cands: list[dict] = []
            try:
                with open(path, newline="") as f:
                    for row in csv.DictReader(f):
                        try:
                            dte = float(row["DTE"])
                            vrp = float(row["vrp"])
                            if not (self.dte_min <= dte <= self.dte_max):
                                continue
                            if vrp < self.min_vrp:
                                continue
                            cands.append({
                                "strike": float(row["strike"]),
                                "expiry": date.fromisoformat(row["expiration"]),
                                "bid": float(row["bid"]),
                                "iv": float(row.get("impliedVolatility") or 0.5),
                                "vrp": vrp,
                                "dte": dte,
                            })
                        except (KeyError, ValueError):
                            continue
            except FileNotFoundError:
                continue
            cands.sort(key=lambda c: c["vrp"], reverse=True)
            if cands:
                self._cands[symbol] = cands

    def _open_puts(self, symbol: str, positions: dict) -> list[tuple]:
        return [k for k, q in positions.items()
                if len(k) == 4 and k[0] == "put" and k[1] == symbol and q < 0]

    def on_bar(self, bar: Bar, ctx: Ctx) -> list[Signal]:
        if bar.symbol not in self._cands:
            return []
        out: list[Signal] = []
        # 1) manage existing shorts
        for key in self._open_puts(bar.symbol, ctx.positions):
            _, _, strike, expiry_iso = key
            expiry = date.fromisoformat(expiry_iso)
            contracts = -ctx.positions[key]
            det = ctx.put_details.get(key, {})
            iv = det.get("iv", 0.5)
            premium = det.get("premium", 0.0)
            dte = (expiry - ctx.today).days
            T = max(dte, 0) / 365.0
            mark = bs_put_price(bar.close, strike, T, self.risk_free, iv)
            sig = Signal(strategy=self.name, symbol=bar.symbol,
                         action="buy_put_close", quantity=contracts,
                         strike=strike, expiry=expiry,
                         limit_price=mark, reduce_only=True)
            if dte <= self.min_dte_to_close:
                sig.note = f"close: {dte} DTE <= {self.min_dte_to_close}"
                out.append(sig)
            elif premium > 0 and (premium - mark) / premium >= self.profit_take_frac:
                sig.note = (f"profit take: captured "
                            f"{(premium - mark) / premium:.0%} of premium")
                out.append(sig)
        if out:
            return out  # manage first; no new entries while managing
        # 2) enter when flat and cooled down
        last = self._last_entry.get(bar.symbol)
        if last is not None and (ctx.today - last).days < self.reentry_days:
            return []
        for cand in self._cands[bar.symbol][: self.top_n]:
            out.append(Signal(
                strategy=self.name, symbol=bar.symbol, action="sell_put",
                quantity=1, strike=cand["strike"], expiry=cand["expiry"],
                limit_price=cand["bid"],
                note=f"vrp={cand['vrp']:.3f} iv={cand['iv']:.2f}",
                meta={"iv": cand["iv"]}))
        if out:
            self._last_entry[bar.symbol] = ctx.today
        return out


# --------------------------------------------------------------------------
# 2 & 3. Classic spot loops -- prove the framework is market-agnostic
# --------------------------------------------------------------------------

def _sma(xs: list[float], n: int) -> Optional[float]:
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


class MomentumStrategy(Strategy):
    """Golden-cross / death-cross trend following, long-only spot."""

    name = "momentum"
    asset_classes = frozenset({"stocks", "predictions"})
    blurb = "Golden/death-cross trend following, long-only spot"

    def __init__(self, fast: int = 20, slow: int = 50,
                 allocation_frac: float = 0.10):
        self.fast = fast
        self.slow = slow
        self.allocation_frac = allocation_frac

    def on_bar(self, bar: Bar, ctx: Ctx) -> list[Signal]:
        xs = ctx.closes.get(bar.symbol, [])
        if len(xs) < self.slow + 1:
            return []
        f_now, s_now = _sma(xs, self.fast), _sma(xs, self.slow)
        f_prev, s_prev = _sma(xs[:-1], self.fast), _sma(xs[:-1], self.slow)
        if None in (f_now, s_now, f_prev, s_prev):
            return []
        pos = ctx.positions.get(("spot", bar.symbol), 0)
        if f_prev <= s_prev and f_now > s_now and pos <= 0:
            qty = math.floor(ctx.equity * self.allocation_frac / bar.close)
            if qty > 0:
                return [Signal(self.name, bar.symbol, "buy", qty,
                               note=f"golden cross {self.fast}/{self.slow}")]
        if f_prev >= s_prev and f_now < s_now and pos > 0:
            return [Signal(self.name, bar.symbol, "sell", pos,
                           reduce_only=True, note=f"death cross {self.fast}/{self.slow}")]
        return []


class MeanReversionStrategy(Strategy):
    """Z-score fade on spot, long or short with a hard stop."""

    name = "meanrev"
    asset_classes = frozenset({"stocks", "predictions"})
    blurb = "Z-score fade on spot, long or short with a hard stop"

    def __init__(self, lookback: int = 20, z_entry: float = 2.0,
                 z_exit: float = 0.5, z_stop: float = 3.5,
                 allocation_frac: float = 0.05):
        self.lookback = lookback
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.z_stop = z_stop
        self.allocation_frac = allocation_frac

    def on_bar(self, bar: Bar, ctx: Ctx) -> list[Signal]:
        xs = ctx.closes.get(bar.symbol, [])
        if len(xs) < self.lookback:
            return []
        window = xs[-self.lookback:]
        mean = sum(window) / len(window)
        var = sum((x - mean) ** 2 for x in window) / len(window)
        if var <= 0:
            return []
        z = (bar.close - mean) / math.sqrt(var)
        pos = ctx.positions.get(("spot", bar.symbol), 0)
        qty = math.floor(ctx.equity * self.allocation_frac / bar.close)
        if pos == 0 and qty > 0:
            if z <= -self.z_entry:
                return [Signal(self.name, bar.symbol, "buy", qty,
                               note=f"long z={z:.2f}")]
            if z >= self.z_entry:
                return [Signal(self.name, bar.symbol, "sell", qty,
                               note=f"short z={z:.2f}")]
        if pos > 0 and (z >= -self.z_exit or z <= -self.z_stop):
            return [Signal(self.name, bar.symbol, "sell", pos,
                           reduce_only=True, note=f"exit long z={z:.2f}")]
        if pos < 0 and (z <= self.z_exit or z >= self.z_stop):
            return [Signal(self.name, bar.symbol, "buy", -pos,
                           reduce_only=True, note=f"cover short z={z:.2f}")]
        return []


# --------------------------------------------------------------------------
# 4. Endgame sweep -- the classic prediction-market edge, mechanical leg
# --------------------------------------------------------------------------

class EndgameSweepStrategy(Strategy):
    """Buy near-certain outcomes for pennies in a market's final window.

    In the last ``endgame_minutes`` before a market's endDate, if the ask is
    at or below ``max_price`` (default 5c), buy ``size_usd`` worth and hold
    to resolution -- the engine settles via the feed's ``settlements()``.

    This is the *mechanical* leg of the classic endgame trade. The real edge
    comes from knowing the outcome is already decided (news/oracle), which is
    NOT modeled here. Paper P&L therefore includes markets that resolve
    against the position; the backtest hit-rate is the honest metric.
    """

    name = "endgame_sweep"
    asset_classes = frozenset({"predictions"})
    blurb = "Buys sub-5c outcomes in a market's final window, holds to resolve"

    def __init__(self, symbols: Optional[list] = None,
                 end_dates: Optional[dict] = None,
                 endgame_minutes: int = 120, max_price: float = 0.05,
                 size_usd: float = 100.0):
        self.symbols = list(symbols or [])
        self.endgame_minutes = endgame_minutes
        self.max_price = max_price
        self.size_usd = size_usd
        # slug -> naive-UTC end datetime. Injected for tests; otherwise
        # fetched once from Gamma at construction.
        self._ends: dict[str, datetime] = dict(end_dates or {})
        if not self._ends:
            self._fetch_ends()

    @staticmethod
    def _to_naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    def _fetch_ends(self) -> None:
        import requests
        for s in self.symbols:
            slug = s.split(":")[0]
            if slug in self._ends:
                continue
            try:
                r = requests.get("https://gamma-api.polymarket.com/markets",
                                 params={"slug": slug}, timeout=15)
                r.raise_for_status()
                data = r.json()
                if not data or not data[0].get("endDate"):
                    continue
                end = datetime.fromisoformat(
                    str(data[0]["endDate"]).replace("Z", "+00:00"))
                self._ends[slug] = self._to_naive_utc(end)
            except Exception:
                continue

    def on_bar(self, bar: Bar, ctx: Ctx) -> list[Signal]:
        slug = bar.symbol.split(":")[0]
        end = self._ends.get(slug)
        if end is None:
            return []
        now = self._to_naive_utc(bar.ts)
        mins_left = (end - now).total_seconds() / 60.0
        if not (0 < mins_left <= self.endgame_minutes):
            return []
        if ctx.positions.get(("spot", bar.symbol), 0) != 0:
            return []  # one endgame position per symbol at a time
        ask = bar.ask if bar.ask else bar.close
        if not ask or ask <= 0 or ask > self.max_price:
            return []
        qty = math.floor(self.size_usd / ask)
        if qty <= 0:
            return []
        return [Signal(self.name, bar.symbol, "buy", qty,
                       note=f"endgame: {mins_left:.0f}m to end, ask={ask:.4f}")]


STRATEGY_INFO: dict[str, dict] = {
    "vrp": {"class": VrpPutSellingStrategy},
    "momentum": {"class": MomentumStrategy},
    "meanrev": {"class": MeanReversionStrategy},
    "endgame": {"class": EndgameSweepStrategy},
}


def describe_strategies() -> list[dict]:
    """Name, asset classes, and blurb for every registered strategy."""
    return [{"name": spec,
             "strategy": info["class"].name,
             "asset_classes": sorted(info["class"].asset_classes),
             "blurb": info["class"].blurb}
            for spec, info in STRATEGY_INFO.items()]


def build_strategy(spec: str, **kwargs) -> Strategy:
    """``spec`` like ``"vrp"``, ``"momentum"``, ``"meanrev"``."""
    if spec not in STRATEGY_INFO:
        raise ValueError(f"unknown strategy {spec!r}; choose from {sorted(STRATEGY_INFO)}")
    return STRATEGY_INFO[spec]["class"](**kwargs)

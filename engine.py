#!/usr/bin/env python3
"""Paper execution engine.

Replays history (or paper-trades live quotes) bar by bar:
  1. settle expired options (assignment or worthless),
  2. ask each strategy loop for signals,
  3. run every signal through the shared risk arbiter,
  4. simulate fills with slippage + fees,
  5. mark everything to market and write it to the ledger.

No real orders, no wallets, no keys -- by construction, not by config.
The engine has no code path that can touch a live market.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from feeds import Bar, MarketDataFeed
from ledger import Ledger
from pricing import bs_put_price
from risk import PortfolioState, RiskArbiter, RiskConfig
from strategies import Ctx, Signal, Strategy


def _pkey(key: tuple) -> str:
    return ":".join(str(p) for p in key)


class PaperEngine:
    def __init__(self, feed: MarketDataFeed, strategies: list[Strategy],
                 arbiter: RiskArbiter, ledger: Ledger,
                 capital: float = 100_000.0,
                 slippage_bps: float = 5.0,
                 fee_per_contract: float = 1.30,
                 fee_per_share: float = 0.0,
                 risk_free: float = 0.04):
        self.feed = feed
        self.strategies = strategies
        self.arbiter = arbiter
        self.ledger = ledger
        self.capital = capital
        self.slippage = slippage_bps / 10_000.0
        self.fee_per_contract = fee_per_contract
        self.fee_per_share = fee_per_share
        self.risk_free = risk_free

        self.cash = capital
        self.positions: dict[tuple, float] = {}   # ("spot",sym) | ("put",sym,K,exp) -> qty
        self.put_details: dict[tuple, dict] = {}  # put key -> {"iv","premium"}
        self.closes: dict[str, list[float]] = defaultdict(list)
        self.latest: dict[str, Bar] = {}
        self.strategy_exposure: dict[str, float] = defaultdict(float)
        self.peak_equity = capital
        self.day_start_equity = capital
        self._day: Optional[date] = None
        self.equity = capital

    # -- helpers ------------------------------------------------------
    def _spot_qty(self, symbol: str) -> float:
        return self.positions.get(("spot", symbol), 0.0)

    def _put_mark(self, key: tuple, S: float, today: date) -> float:
        _, _, strike, exp_iso = key
        det = self.put_details.get(key, {})
        iv = det.get("iv", 0.5)
        T = max((date.fromisoformat(exp_iso) - today).days, 0) / 365.0
        return bs_put_price(S, strike, T, self.risk_free, iv)

    def _marks(self, today: date) -> dict[tuple, float]:
        """position key -> total marked value."""
        out: dict[tuple, float] = {}
        for key, qty in self.positions.items():
            if qty == 0:
                continue
            if key[0] == "spot":
                bar = self.latest.get(key[1])
                px = bar.close if bar else 0.0
                out[key] = qty * px
            else:  # short put liability
                bar = self.latest.get(key[1])
                S = bar.close if bar else 0.0
                out[key] = qty * self._put_mark(key, S, today) * 100.0
        return out

    def _symbol_exposure(self, today: date) -> dict[str, float]:
        exp: dict[str, float] = defaultdict(float)
        for key, qty in self.positions.items():
            if qty == 0:
                continue
            if key[0] == "spot":
                bar = self.latest.get(key[1])
                px = bar.close if bar else 0.0
                exp[key[1]] += abs(qty) * px
            else:
                exp[key[1]] += abs(qty) * float(key[2]) * 100.0
        return dict(exp)

    # -- fills ----------------------------------------------------------
    def _est_fill(self, signal: Signal, bar: Bar) -> float:
        if signal.action in ("buy", "sell"):
            return bar.close * (1 + self.slippage if signal.action == "buy"
                                else 1 - self.slippage)
        if signal.action == "sell_put":
            px = signal.limit_price or bar.close * 0.05
            return px * (1 - self.slippage)
        if signal.action == "buy_put_close":
            key = ("put", signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            S = bar.close
            mark = self._put_mark(key, S, bar.ts.date())
            return mark * (1 + self.slippage)
        return 0.0

    def _apply_fill(self, signal: Signal, fill: float, ts: datetime) -> None:
        qty = signal.quantity
        if signal.action == "buy":
            cost = qty * fill + self.fee_per_share * qty
            self.cash -= cost
            self.positions[("spot", signal.symbol)] = \
                self._spot_qty(signal.symbol) + qty
            self.strategy_exposure[signal.strategy] += qty * fill
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "buy", qty, fill, -cost, signal.note)
        elif signal.action == "sell":
            proceeds = qty * fill - self.fee_per_share * qty
            self.cash += proceeds
            self.positions[("spot", signal.symbol)] = \
                self._spot_qty(signal.symbol) - qty
            self.strategy_exposure[signal.strategy] = max(
                0.0, self.strategy_exposure[signal.strategy] - qty * fill)
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "sell", qty, fill, proceeds, signal.note)
        elif signal.action == "sell_put":
            contracts = int(qty)
            key = ("put", signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            premium = contracts * fill * 100.0
            fees = self.fee_per_contract * contracts
            self.cash += premium - fees
            self.positions[key] = self.positions.get(key, 0.0) - contracts
            self.put_details[key] = {"iv": signal.meta.get("iv", 0.5),
                                     "premium": fill}
            self.strategy_exposure[signal.strategy] += \
                contracts * signal.strike * 100.0
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "sell_put", contracts, fill,
                                     premium - fees,
                                     f"K={signal.strike} exp={signal.expiry} {signal.note}")
        elif signal.action == "buy_put_close":
            contracts = int(qty)
            key = ("put", signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            cost = contracts * fill * 100.0 + self.fee_per_contract * contracts
            self.cash -= cost
            self.positions[key] = self.positions.get(key, 0.0) + contracts
            if abs(self.positions[key]) < 1e-9:
                del self.positions[key]
                self.put_details.pop(key, None)
            self.strategy_exposure[signal.strategy] = max(
                0.0, self.strategy_exposure[signal.strategy]
                - contracts * signal.strike * 100.0)
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "buy_put_close", contracts, fill,
                                     -cost, signal.note)

    # -- expiry ---------------------------------------------------------
    def _settle_expiries(self, today: date, ts: datetime) -> None:
        for key in [k for k in self.positions
                    if len(k) == 4 and k[0] == "put"]:
            _, sym, strike, exp_iso = key
            if date.fromisoformat(exp_iso) > today:
                continue
            qty = self.positions.pop(key)
            self.put_details.pop(key, None)
            contracts = abs(qty)
            bar = self.latest.get(sym)
            S = bar.close if bar else strike
            if S < strike:
                # assigned: buy the shares at the strike
                shares = 100 * contracts
                self.positions[("spot", sym)] = self._spot_qty(sym) + shares
                cost = strike * shares
                self.cash -= cost
                self.ledger.record_trade(ts, "engine", sym, "expiry_assign",
                                         shares, strike, -cost,
                                         f"put K={strike} assigned, S={S:.2f}")
            else:
                self.ledger.record_trade(ts, "engine", sym, "expiry_worthless",
                                         contracts, 0.0, 0.0,
                                         f"put K={strike} expired, S={S:.2f}")
            self.strategy_exposure["vrp_puts"] = max(
                0.0, self.strategy_exposure.get("vrp_puts", 0.0)
                - contracts * strike * 100.0)

    # -- main loop ------------------------------------------------------
    def run(self, symbols: list[str], start: date, end: date) -> dict:
        histories = {}
        for s in symbols:
            bars = self.feed.history(s, start, end)
            if not bars:
                print(f"  [feed] no bars for {s}; skipping")
                continue
            histories[s] = bars
        if not histories:
            return {"error": "no data for any symbol"}
        timeline = sorted({b.ts for bars in histories.values() for b in bars})
        by_ts: dict[datetime, dict[str, Bar]] = defaultdict(dict)
        for s, bars in histories.items():
            for b in bars:
                by_ts[b.ts][s] = b

        n_signals = n_fills = n_rejected = 0
        for ts in timeline:
            today = ts.date()
            if self._day != today:
                self._day = today
                self.day_start_equity = self.equity
            todays = by_ts[ts]
            for s, b in todays.items():
                self.latest[s] = b
                self.closes[s].append(b.close)
            self._settle_expiries(today, ts)

            marks = self._marks(today)
            self.equity = self.cash + sum(marks.values())
            self.peak_equity = max(self.peak_equity, self.equity)

            ctx = Ctx(cash=self.cash, equity=self.equity, today=today,
                      positions=dict(self.positions),
                      closes={s: list(c) for s, c in self.closes.items()},
                      put_details=dict(self.put_details))
            for strat in self.strategies:
                for bar in todays.values():
                    try:
                        signals = strat.on_bar(bar, ctx) or []
                    except Exception as e:  # a broken strategy never kills the loop
                        print(f"  [strategy:{strat.name}] error on {bar.symbol}: {e}")
                        continue
                    for sig in signals:
                        n_signals += 1
                        if sig.symbol not in todays:
                            continue
                        est = self._est_fill(sig, todays[sig.symbol])
                        state = PortfolioState(
                            cash=self.cash, equity=self.equity,
                            peak_equity=self.peak_equity,
                            day_start_equity=self.day_start_equity,
                            strategy_exposure=dict(self.strategy_exposure),
                            symbol_exposure=self._symbol_exposure(today))
                        ok, reason = self.arbiter.check(sig, est, state)
                        if not ok:
                            n_rejected += 1
                            self.ledger.record_risk_event(
                                ts, sig.strategy, sig.symbol, sig.action, reason)
                            continue
                        self._apply_fill(sig, est, ts)
                        n_fills += 1
                        # refresh marks after the fill for the next signal
                        marks = self._marks(today)
                        self.equity = self.cash + sum(marks.values())
                        self.peak_equity = max(self.peak_equity, self.equity)

            self.ledger.record_equity(ts, self.equity, self.cash)
            snap = {}
            for key, qty in self.positions.items():
                if qty == 0:
                    continue
                if key[0] == "spot":
                    bar = self.latest.get(key[1])
                    snap[_pkey(key)] = (qty, bar.close if bar else 0.0)
                else:
                    bar = self.latest.get(key[1])
                    S = bar.close if bar else 0.0
                    snap[_pkey(key)] = (qty, -self._put_mark(key, S, today) * 100.0)
            self.ledger.snapshot_positions(ts, snap)

        return {"signals": n_signals, "fills": n_fills, "rejected": n_rejected,
                "final_equity": self.equity, "cash": self.cash,
                "return_pct": (self.equity / self.capital - 1) * 100}

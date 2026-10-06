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

import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from feeds import Bar, MarketDataFeed
from ledger import Ledger
from pricing import bs_call_price, bs_put_price
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
        self.positions: dict[tuple, float] = {}   # ("spot",sym) |
                                                  # ("put"|"call",sym,K,exp) -> qty
                                                  # (negative = short)
        self.put_details: dict[tuple, dict] = {}  # put key -> {"iv","premium"}
        self.call_details: dict[tuple, dict] = {}  # call key -> {"iv","premium"}
        self.closes: dict[str, list[float]] = defaultdict(list)
        self.latest: dict[str, Bar] = {}
        self.strategy_exposure: dict[str, float] = defaultdict(float)
        self.peak_equity = capital
        self.day_start_equity = capital
        self._day: Optional[date] = None
        self.equity = capital
        self._n_signals = self._n_fills = self._n_rejected = 0
        # position key -> strategy that opened it / deployed notional, so
        # settlement can release the right strategy's budget (approximate
        # under netting, like the arbiter's own accounting).
        self._key_strategy: dict[tuple, str] = {}
        self._key_exposure: dict[tuple, float] = {}

    # -- helpers ------------------------------------------------------
    def _spot_qty(self, symbol: str) -> float:
        return self.positions.get(("spot", symbol), 0.0)

    def _put_mark(self, key: tuple, S: float, today: date) -> float:
        _, _, strike, exp_iso = key
        det = self.put_details.get(key, {})
        iv = det.get("iv", 0.5)
        T = max((date.fromisoformat(exp_iso) - today).days, 0) / 365.0
        return bs_put_price(S, strike, T, self.risk_free, iv)

    def _call_mark(self, key: tuple, S: float, today: date) -> float:
        _, _, strike, exp_iso = key
        det = self.call_details.get(key, {})
        iv = det.get("iv", 0.5)
        T = max((date.fromisoformat(exp_iso) - today).days, 0) / 365.0
        return bs_call_price(S, strike, T, self.risk_free, iv)

    def _opt_mark(self, key: tuple, S: float, today: date) -> float:
        """Black-Scholes mark for a ("put"|"call", sym, K, exp) key."""
        if key[0] == "call":
            return self._call_mark(key, S, today)
        return self._put_mark(key, S, today)

    def _marks(self, today: date) -> dict[tuple, float]:
        """position key -> total marked value (sign-correct: long positive,
        short negative)."""
        out: dict[tuple, float] = {}
        for key, qty in self.positions.items():
            if qty == 0:
                continue
            if key[0] == "spot":
                bar = self.latest.get(key[1])
                px = bar.close if bar else 0.0
                out[key] = qty * px
            else:  # option, long or short
                bar = self.latest.get(key[1])
                S = bar.close if bar else 0.0
                out[key] = qty * self._opt_mark(key, S, today) * 100.0
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
            elif qty < 0:  # short option: the assignment obligation
                exp[key[1]] += abs(qty) * float(key[2]) * 100.0
            else:  # long option: premium at risk, marked
                bar = self.latest.get(key[1])
                S = bar.close if bar else 0.0
                exp[key[1]] += abs(qty) * self._opt_mark(key, S, today) * 100.0
        return dict(exp)

    # -- fills ----------------------------------------------------------
    def _est_fill(self, signal: Signal, bar: Bar) -> float:
        # Live bars carry the real top-of-book: lift the ask on buys, hit
        # the bid on sells. Historical bars fall back to close +/- slippage.
        if signal.action == "buy":
            px = bar.ask if bar.ask else bar.close
            return px * (1 + self.slippage)
        if signal.action == "sell":
            px = bar.bid if bar.bid else bar.close
            return px * (1 - self.slippage)
        if signal.action == "sell_put":
            px = signal.limit_price or bar.close * 0.05
            return px * (1 - self.slippage)
        if signal.action == "buy_put_close":
            key = ("put", signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            S = bar.close
            mark = self._put_mark(key, S, bar.ts.date())
            return mark * (1 + self.slippage)
        if signal.action in ("buy_put", "buy_call"):
            # Opening a long option: pay the ask. Strategies pass the
            # scanner's ask as limit_price; fallback is a BS mark on the
            # signal's IV (never the underlying's price -- that's not an
            # option quote).
            kind = "put" if signal.action == "buy_put" else "call"
            if signal.limit_price:
                return signal.limit_price * (1 + self.slippage)
            S = bar.close
            iv = (signal.meta or {}).get("iv", 0.5)
            T = max((signal.expiry - bar.ts.date()).days, 0) / 365.0
            pricer = bs_put_price if kind == "put" else bs_call_price
            return pricer(S, signal.strike, T, self.risk_free, iv) \
                * (1 + self.slippage)
        if signal.action in ("sell_put_close", "sell_call_close"):
            # Closing a long option: hit the bid, modeled as mark - slippage.
            kind = "put" if signal.action == "sell_put_close" else "call"
            key = (kind, signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            S = bar.close
            mark = self._opt_mark(key, S, bar.ts.date())
            return mark * (1 - self.slippage)
        return 0.0

    def _apply_fill(self, signal: Signal, fill: float, ts: datetime) -> None:
        qty = signal.quantity
        if signal.action == "buy":
            cost = qty * fill + self.fee_per_share * qty
            self.cash -= cost
            key = ("spot", signal.symbol)
            self.positions[key] = self._spot_qty(signal.symbol) + qty
            self.strategy_exposure[signal.strategy] += qty * fill
            self._key_strategy[key] = signal.strategy
            self._key_exposure[key] = self._key_exposure.get(key, 0.0) + qty * fill
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "buy", qty, fill, -cost, signal.note)
        elif signal.action == "sell":
            proceeds = qty * fill - self.fee_per_share * qty
            self.cash += proceeds
            key = ("spot", signal.symbol)
            new_qty = self._spot_qty(signal.symbol) - qty
            self.positions[key] = new_qty
            self.strategy_exposure[signal.strategy] = max(
                0.0, self.strategy_exposure[signal.strategy] - qty * fill)
            if new_qty < -1e-9:
                self._key_strategy[key] = signal.strategy  # opened a short
                self._key_exposure[key] = self._key_exposure.get(key, 0.0) + qty * fill
            else:
                self._key_exposure[key] = max(
                    0.0, self._key_exposure.get(key, 0.0) - qty * fill)
                if abs(new_qty) < 1e-9:
                    self._key_strategy.pop(key, None)
                    self._key_exposure.pop(key, None)
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
            self._key_strategy[key] = signal.strategy
            self._key_exposure[key] = self._key_exposure.get(key, 0.0) \
                + contracts * signal.strike * 100.0
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "sell_put", contracts, fill,
                                     premium - fees,
                                     f"K={signal.strike} exp={signal.expiry} {signal.note}")
        elif signal.action in ("buy_put", "buy_call"):
            # Opening a long option: max loss is the premium paid.
            kind = "put" if signal.action == "buy_put" else "call"
            contracts = int(qty)
            key = (kind, signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            premium = contracts * fill * 100.0
            fees = self.fee_per_contract * contracts
            self.cash -= premium + fees
            self.positions[key] = self.positions.get(key, 0.0) + contracts
            det = self.put_details if kind == "put" else self.call_details
            det[key] = {"iv": signal.meta.get("iv", 0.5), "premium": fill}
            self.strategy_exposure[signal.strategy] += premium
            self._key_strategy[key] = signal.strategy
            self._key_exposure[key] = self._key_exposure.get(key, 0.0) + premium
            self.ledger.record_trade(
                ts, signal.strategy, signal.symbol, signal.action,
                contracts, fill, -(premium + fees),
                f"{'P' if kind == 'put' else 'C'}={signal.strike} "
                f"exp={signal.expiry} {signal.note}")
        elif signal.action in ("sell_put_close", "sell_call_close"):
            # Closing a long option.
            kind = "put" if signal.action == "sell_put_close" else "call"
            contracts = int(qty)
            key = (kind, signal.symbol, signal.strike,
                   signal.expiry.isoformat())
            pos_before = self.positions.get(key, 0.0)
            proceeds = contracts * fill * 100.0 \
                - self.fee_per_contract * contracts
            self.cash += proceeds
            self.positions[key] = pos_before - contracts
            if abs(self.positions[key]) < 1e-9:
                del self.positions[key]
                (self.put_details if kind == "put"
                 else self.call_details).pop(key, None)
            released = self._key_exposure.get(key, 0.0) * (
                contracts / pos_before if pos_before > 0 else 0.0)
            self.strategy_exposure[signal.strategy] = max(
                0.0, self.strategy_exposure[signal.strategy] - released)
            self._key_exposure[key] = max(
                0.0, self._key_exposure.get(key, 0.0) - released)
            if key not in self.positions:
                self._key_strategy.pop(key, None)
                self._key_exposure.pop(key, None)
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     signal.action, contracts, fill,
                                     proceeds, signal.note)
        elif signal.action == "buy_put_close":
            # Closing a short put: pay up (mark + slippage).
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
            self._key_exposure[key] = max(
                0.0, self._key_exposure.get(key, 0.0)
                - contracts * signal.strike * 100.0)
            if key not in self.positions:
                self._key_strategy.pop(key, None)
                self._key_exposure.pop(key, None)
            self.ledger.record_trade(ts, signal.strategy, signal.symbol,
                                     "buy_put_close", contracts, fill,
                                     -cost, signal.note)

    # -- expiry ---------------------------------------------------------
    def _settle_expiries(self, today: date, ts: datetime) -> None:
        for key in [k for k in self.positions
                    if len(k) == 4 and k[0] in ("put", "call")]:
            kind, sym, strike, exp_iso = key
            if date.fromisoformat(exp_iso) > today:
                continue
            qty = self.positions.pop(key)
            (self.put_details if kind == "put"
             else self.call_details).pop(key, None)
            contracts = abs(qty)
            bar = self.latest.get(sym)
            S = bar.close if bar else strike
            # The expiry P&L belongs to the strategy that opened the position,
            # so FIFO attribution matches it against that strategy's lots.
            strat = self._key_strategy.pop(key, "engine")
            if qty < 0:
                # Short put: assignment below the strike, worthless above.
                # (The engine never opens short calls.)
                if S < strike:
                    shares = 100 * contracts
                    self.positions[("spot", sym)] = \
                        self._spot_qty(sym) + shares
                    cost = strike * shares
                    self.cash -= cost
                    self.ledger.record_trade(ts, strat, sym,
                                             "expiry_assign", shares,
                                             strike, -cost,
                                             f"put K={strike} assigned, "
                                             f"S={S:.2f}")
                else:
                    self.ledger.record_trade(ts, strat, sym,
                                             "expiry_worthless", contracts,
                                             0.0, 0.0,
                                             f"put K={strike} expired, "
                                             f"S={S:.2f}")
                released = self._key_exposure.pop(key, 0.0) \
                    or contracts * strike * 100.0
            else:
                # Long option: collect intrinsic value at expiry.
                intrinsic = max(strike - S, 0.0) if kind == "put" \
                    else max(S - strike, 0.0)
                value = contracts * intrinsic * 100.0
                self.cash += value
                self.ledger.record_trade(
                    ts, strat, sym, f"expiry_{kind}_long",
                    contracts, intrinsic, value,
                    f"{kind} K={strike} expired, S={S:.2f}")
                released = self._key_exposure.pop(key, 0.0)
            self.strategy_exposure[strat] = max(
                0.0, self.strategy_exposure.get(strat, 0.0) - released)

    # -- prediction-market resolution ------------------------------------
    def _settle_predictions(self, today: date, ts: datetime) -> None:
        """Cash out expired prediction-market positions at resolved payout.

        Only *decisive* Gamma resolutions settle (1.0/0.0); ambiguous markets
        stay marked at last price. Shorts settle symmetrically: a short Yes
        that resolves Yes pays $1/share.
        """
        if self.feed.asset_class != "predictions":
            return
        held = [key[1] for key, qty in self.positions.items()
                if key[0] == "spot" and qty != 0]
        if not held:
            return
        try:
            payouts = self.feed.settlements(held, today)
        except Exception as e:
            print(f"  [engine] settlements lookup failed: {e}")
            return
        for symbol, payout in payouts:
            key = ("spot", symbol)
            qty = self.positions.pop(key, 0.0)
            if qty == 0:
                continue
            cash_delta = qty * payout
            self.cash += cash_delta
            strat = self._key_strategy.pop(key, None)
            released = self._key_exposure.pop(key, 0.0)
            if strat:
                self.strategy_exposure[strat] = max(
                    0.0, self.strategy_exposure.get(strat, 0.0) - released)
            won = (qty > 0) == (payout >= 1.0)
            self.ledger.record_trade(ts, strat or "engine", symbol, "resolve",
                                     qty, payout, cash_delta,
                                     "prediction resolved "
                                     f"({'win' if won else 'loss'})")

    def _compatible_strategies(self) -> list[Strategy]:
        """Strategies this feed can serve; mismatches are skipped loudly."""
        compatible = [s for s in self.strategies
                      if self.feed.asset_class in s.asset_classes]
        for s in self.strategies:
            if s not in compatible:
                print(f"  [engine] skipping '{s.name}': needs "
                      f"{sorted(s.asset_classes)}, feed '{self.feed.name}' "
                      f"provides '{self.feed.asset_class}'")
        return compatible

    def _reset_counters(self) -> None:
        self._n_signals = self._n_fills = self._n_rejected = 0

    def _step(self, ts: datetime, todays: dict[str, Bar],
              strategies: list[Strategy]) -> None:
        """One engine step: settle, mark, signal, arbitrate, fill, ledger.

        Shared by backtest replay and the live loop -- the only difference
        is where the bars come from.
        """
        today = ts.date()
        if self._day != today:
            self._day = today
            self.day_start_equity = self.equity
        for s, b in todays.items():
            self.latest[s] = b
            self.closes[s].append(b.close)
        self._settle_expiries(today, ts)
        self._settle_predictions(today, ts)

        marks = self._marks(today)
        self.equity = self.cash + sum(marks.values())
        self.peak_equity = max(self.peak_equity, self.equity)

        ctx = Ctx(cash=self.cash, equity=self.equity, today=today,
                  positions=dict(self.positions),
                  closes={s: list(c) for s, c in self.closes.items()},
                  put_details=dict(self.put_details),
                  call_details=dict(self.call_details))
        for strat in strategies:
            for bar in todays.values():
                try:
                    signals = strat.on_bar(bar, ctx) or []
                except Exception as e:  # a broken strategy never kills the loop
                    print(f"  [strategy:{strat.name}] error on {bar.symbol}: {e}")
                    continue
                for sig in signals:
                    self._n_signals += 1
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
                        self._n_rejected += 1
                        self.ledger.record_risk_event(
                            ts, sig.strategy, sig.symbol, sig.action, reason)
                        continue
                    self._apply_fill(sig, est, ts)
                    self._n_fills += 1
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
            else:  # option, long or short: mark is the unsigned option
                   # value; the qty sign carries the long/short direction
                bar = self.latest.get(key[1])
                S = bar.close if bar else 0.0
                snap[_pkey(key)] = (qty,
                                    self._opt_mark(key, S, today) * 100.0)
        self.ledger.snapshot_positions(ts, snap)

    def _summary(self) -> dict:
        return {"signals": self._n_signals, "fills": self._n_fills,
                "rejected": self._n_rejected, "final_equity": self.equity,
                "cash": self.cash,
                "return_pct": (self.equity / self.capital - 1) * 100}

    # -- live-mode heartbeat & alerts --------------------------------------
    def _heartbeat_path(self) -> str:
        import os
        d = os.path.dirname(os.path.abspath(self.ledger.path)) or "."
        return os.path.join(d, "heartbeat.json")

    def _write_heartbeat(self, step: int) -> None:
        """Dead-man's switch: the dashboard shows LIVE/STALE from this file."""
        if self.ledger.path == ":memory:":
            return
        import json as _json
        try:
            with open(self._heartbeat_path(), "w") as f:
                _json.dump({"ts": datetime.now().isoformat(), "mode": "live",
                            "equity": self.equity, "cash": self.cash,
                            "step": step, "fills": self._n_fills}, f)
        except Exception:
            pass

    @staticmethod
    def _alert(alert_url: Optional[str], event: str, payload: dict) -> None:
        """POST a JSON event to a webhook (Slack/Discord/ntfy...). Best-effort."""
        if not alert_url:
            return
        import json as _json
        import urllib.request
        try:
            req = urllib.request.Request(
                alert_url, data=_json.dumps({"event": event, **payload}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  [alert] POST failed: {e}")

    # -- main loops -----------------------------------------------------
    def run(self, symbols: list[str], start: date, end: date) -> dict:
        """Backtest: replay historical bars."""
        strategies = self._compatible_strategies()
        if not strategies:
            return {"error": "no strategy compatible with "
                             f"{self.feed.asset_class} feed"}
        self._reset_counters()

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

        for ts in timeline:
            self._step(ts, by_ts[ts], strategies)
        return self._summary()

    def _seed_closes(self, symbols: list[str], lookback_days: int = 400,
                     max_bars: int = 500) -> None:
        """Warm the strategies' recent-close series from feed history when
        the feed has one (Yahoo does; the WS socket doesn't -- those loops
        simply warm up live)."""
        end = date.today()
        start = end - timedelta(days=lookback_days)
        for s in symbols:
            try:
                bars = self.feed.history(s, start, end) or []
            except Exception:
                bars = []
            for b in bars[-max_bars:]:
                self.closes[s].append(b.close)
            if bars:
                print(f"  [engine] seeded {len(bars[-max_bars:])} closes for {s}")

    def run_live(self, symbols: list[str], interval_s: float = 60.0,
                 duration_s: Optional[float] = None,
                 alert_url: Optional[str] = None) -> dict:
        """Paper-forward: step the engine on live quotes until stopped.

        Every ``interval_s`` seconds each symbol is quoted via
        ``feed.latest_quote()``; the mid becomes the bar's close with the
        real bid/ask attached, so fills cross the actual spread. Runs until
        ``duration_s`` elapses or Ctrl-C. Strategies warm up from feed
        history when available. Writes a heartbeat file for the dashboard's
        dead-man's switch and POSTs session/kill-switch events to
        ``alert_url`` when given.
        """
        strategies = self._compatible_strategies()
        if not strategies:
            return {"error": "no strategy compatible with "
                             f"{self.feed.asset_class} feed"}
        self._reset_counters()
        self._seed_closes(symbols)

        # Warm up: make sure every symbol has a live book before step 1,
        # so the first round isn't skipped on a slow socket handshake.
        print("  [live] warming up quotes...")
        live_symbols = []
        for s in symbols:
            q = self.feed.latest_quote(s)
            if q is None:
                print(f"  [live] WARNING: no quote for {s} after warm-up; "
                      f"it will be retried each round")
                continue
            live_symbols.append(s)
            print(f"  [live] {s}: bid={q.bid:.4f} ask={q.ask:.4f}")
        if not live_symbols:
            return {"error": "no live quotes for any symbol"}
        symbols = live_symbols

        t0 = time.time()
        step = 0
        alerted: set[str] = set()
        print(f"  [live] every {interval_s:g}s; "
              f"{'for ' + str(duration_s) + 's' if duration_s else 'until Ctrl-C'}")
        self._alert(alert_url, "live_start",
                    {"symbols": symbols, "interval_s": interval_s,
                     "capital": self.capital})
        try:
            while True:
                if duration_s and time.time() - t0 >= duration_s:
                    break
                ts = datetime.now()
                todays: dict[str, Bar] = {}
                for s in symbols:
                    try:
                        q = self.feed.latest_quote(s)
                    except Exception as e:
                        print(f"  [feed] quote error for {s}: {e}")
                        continue
                    if q is None:
                        if step % 10 == 0:
                            print(f"  [feed] no quote for {s}; skipping")
                        continue
                    mid = q.mid
                    todays[s] = Bar(ts=q.ts, symbol=s, open=mid, high=mid,
                                    low=mid, close=mid, volume=0.0,
                                    bid=q.bid, ask=q.ask)
                if todays:
                    self._step(ts, todays, strategies)
                    step += 1
                    self._write_heartbeat(step)
                    for r in self.ledger.risk_events(limit=20):
                        if ("killswitch" in r["reason"]
                                and r["reason"] not in alerted):
                            alerted.add(r["reason"])
                            self._alert(alert_url, "killswitch",
                                        {"reason": r["reason"],
                                         "strategy": r["strategy"],
                                         "symbol": r["symbol"],
                                         "equity": self.equity})
                    if step % 10 == 0:
                        print(f"  [live] step {step}: equity=${self.equity:,.2f} "
                              f"fills={self._n_fills}")
                else:
                    print("  [live] no quotes this round; waiting")
                if duration_s and time.time() - t0 >= duration_s:
                    break
                time.sleep(interval_s)
        except KeyboardInterrupt:
            print("\n  [live] stopped by user")
        self._alert(alert_url, "live_stop", {"summary": self._summary()})
        return self._summary()

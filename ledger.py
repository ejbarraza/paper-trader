#!/usr/bin/env python3
"""SQLite ledger: every trade, every risk decision, every equity mark.

The dashboard reads only this file -- the engine is never required to be
running to inspect what happened. Delete the file to start over.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
                action TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
                cash_delta REAL NOT NULL, note TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS equity(
                ts TEXT PRIMARY KEY, equity REAL NOT NULL, cash REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS risk_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL,
                action TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions(
                ts TEXT NOT NULL, pkey TEXT NOT NULL, qty REAL NOT NULL,
                mark REAL NOT NULL,
                PRIMARY KEY (ts, pkey));
            CREATE TABLE IF NOT EXISTS snapshots(
                ts TEXT PRIMARY KEY);
            -- A/E tracker: decision-time expected premium per opening trade.
            -- expected is signed dollars: + premium the signal expected to
            -- collect (short_put opens at the scanner bid), - premium it
            -- expected to pay (long opens at the ask) or spend (spot buys).
            -- Backward compatible: old DBs simply have no rows here.
            CREATE TABLE IF NOT EXISTS ae_expected(
                trade_id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                strategy TEXT NOT NULL,
                symbol TEXT NOT NULL,
                leg TEXT NOT NULL,
                expected REAL NOT NULL);
        """)
        self._db.commit()

    # -- writes ---------------------------------------------------------
    def record_trade(self, ts: datetime, strategy: str, symbol: str,
                     action: str, qty: float, price: float,
                     cash_delta: float, note: str = "") -> int:
        """Record a trade; returns the trade row id (used to link A/E marks)."""
        cur = self._db.execute(
            "INSERT INTO trades(ts,strategy,symbol,action,qty,price,cash_delta,note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts.isoformat(), strategy, symbol, action, qty, price,
             cash_delta, note))
        self._db.commit()
        return cur.lastrowid

    def record_ae_mark(self, ts: datetime, strategy: str, symbol: str,
                       leg: str, expected: float, trade_id: int) -> None:
        """Record the decision-time expected premium for an opening trade.

        leg: short_put | long_put | long_call | spot. expected is signed
        dollars (see ae_expected schema). Called by the engine for every
        opening fill whose signal carried a limit price.
        """
        self._db.execute(
            "INSERT OR REPLACE INTO ae_expected"
            "(trade_id,ts,strategy,symbol,leg,expected)"
            " VALUES (?,?,?,?,?,?)",
            (trade_id, ts.isoformat(), strategy, symbol, leg, expected))
        self._db.commit()

    def record_equity(self, ts: datetime, equity: float, cash: float) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO equity(ts,equity,cash) VALUES (?,?,?)",
            (ts.isoformat(), equity, cash))
        self._db.commit()

    def record_risk_event(self, ts: datetime, strategy: str, symbol: str,
                          action: str, reason: str) -> None:
        self._db.execute(
            "INSERT INTO risk_events(ts,strategy,symbol,action,reason)"
            " VALUES (?,?,?,?,?)",
            (ts.isoformat(), strategy, symbol, action, reason))
        self._db.commit()

    def snapshot_positions(self, ts: datetime, marks: dict[str, tuple[float, float]]) -> None:
        """marks: pkey -> (qty, mark_price). The ts is recorded even when
        marks is empty, so latest_positions() knows the account went
        flat instead of showing the previous non-empty snapshot."""
        iso = ts.isoformat()
        self._db.execute("DELETE FROM positions WHERE ts=?", (iso,))
        self._db.executemany(
            "INSERT INTO positions(ts,pkey,qty,mark) VALUES (?,?,?,?)",
            [(iso, k, q, m) for k, (q, m) in marks.items()])
        self._db.execute("INSERT OR IGNORE INTO snapshots(ts) VALUES (?)",
                         (iso,))
        self._db.commit()

    # -- reads (dashboard) ----------------------------------------------
    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def equity_curve(self) -> list[dict]:
        return self._all("SELECT ts,equity,cash FROM equity ORDER BY ts")

    def trades(self, limit: int = 500) -> list[dict]:
        return self._all("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))

    def risk_events(self, limit: int = 500) -> list[dict]:
        return self._all("SELECT * FROM risk_events ORDER BY ts DESC LIMIT ?", (limit,))

    def latest_positions(self) -> list[dict]:
        snap = self._all(
            "SELECT ts FROM snapshots ORDER BY ts DESC LIMIT 1")
        if snap:
            latest_ts = snap[0]["ts"]
        else:
            # DBs written before the snapshots table existed.
            legacy = self._all("SELECT MAX(ts) AS ts FROM positions")
            latest_ts = legacy[0]["ts"] if legacy else None
        if not latest_ts:
            return []
        return self._all(
            "SELECT pkey,qty,mark FROM positions WHERE ts=?",
            (latest_ts,))

    def strategy_pnl(self) -> list[dict]:
        return self._all("""
            SELECT strategy,
                   SUM(CASE WHEN cash_delta > 0 THEN cash_delta ELSE 0 END) AS gross_in,
                   SUM(CASE WHEN cash_delta < 0 THEN cash_delta ELSE 0 END) AS gross_out,
                   SUM(cash_delta) AS net,
                   COUNT(*) AS n_trades
            FROM trades GROUP BY strategy ORDER BY net DESC""")

    def strategy_stats(self) -> list[dict]:
        """Per-strategy attribution with FIFO-matched win/loss.

        ``net`` is total cash flow (exact, includes open positions' costs).
        Wins/losses come from FIFO-matching closes against opens per
        (strategy, symbol): spot buy/sell/resolve, short-put sell/close/
        expire, long-put buy/close/expire, long-call buy/close/expire.
        An assigned put keeps its premium as realized and opens a stock lot
        at the strike (exactly what the engine records).

        Realized P&L is in dollars throughout (option lots are scaled by
        100x -- contracts x per-share points). ``expectancy`` is mean
        realized P&L per closed round trip; ``profit_factor`` is gross
        wins / gross losses.
        """
        stats, _, _, _ = self._fifo_walk()
        return stats

    def realized_series(self) -> list[dict]:
        """Cumulative realized P&L per strategy after each trade, in time
        order: [{"ts", "strategy", "realized"}]. A step function over
        closed trades only -- the raw material for judging edge. Open
        positions contribute nothing until they close."""
        _, series, _, _ = self._fifo_walk(collect_series=True)
        return series

    def _fifo_walk(self, collect_series: bool = False,
                   collect_leg_series: bool = False):
        trades = self._all("SELECT * FROM trades ORDER BY id")
        # (strategy, symbol) -> list of [qty, price]; qty<0 means short/open put
        spot_lots: dict[tuple, list] = {}
        put_lots: dict[tuple, list] = {}       # short puts: sell_put opens
        long_put_lots: dict[tuple, list] = {}  # long puts: buy_put opens
        long_call_lots: dict[tuple, list] = {}  # long calls: buy_call opens
        stats: dict[str, dict] = {}

        def st(s):
            return stats.setdefault(s, {"strategy": s, "net": 0.0,
                                        "n_trades": 0, "wins": 0, "losses": 0,
                                        "realized": 0.0,
                                        "gross_win": 0.0, "gross_loss": 0.0})

        def match(lots, key, qty, price, pnl_sign):
            """Close `qty` against FIFO lots; returns realized P&L."""
            realized = 0.0
            book = lots.setdefault(key, [])
            while qty > 1e-9 and book:
                lq, lp = book[0]
                m = min(qty, abs(lq))
                realized += pnl_sign * m * (price - lp) * (1 if lq > 0 else -1)
                qty -= m
                if abs(lq) - m < 1e-9:
                    book.pop(0)
                else:
                    book[0][0] = lq - m * (1 if lq > 0 else -1)
            return realized, qty

        series: list[dict] = []
        leg_series: list[dict] = []
        for t in trades:
            s, sym, a = t["strategy"], t["symbol"], t["action"]
            q, px, cd = t["qty"], t["price"], t["cash_delta"]
            row = st(s)
            row["net"] += cd
            row["n_trades"] += 1
            key = (s, sym)
            realized_before = row["realized"]
            leg = None  # which book this trade closed against, if any
            if a == "buy":                # cover shorts first, then open long
                realized, rem = match(spot_lots, key, q, px, +1)
                row["realized"] += realized
                self._bump_wl(row, realized)
                leg = "spot"
                if rem > 1e-9:
                    spot_lots.setdefault(key, []).append([rem, px])
            elif a == "sell":
                # close longs first, then open short
                realized, rem = match(spot_lots, key, q, px, +1)
                row["realized"] += realized
                self._bump_wl(row, realized)
                leg = "spot"
                if rem > 1e-9:
                    spot_lots.setdefault(key, []).append([-rem, px])
            elif a == "resolve":
                realized, _ = match(spot_lots, key, abs(q), px, +1)
                # shorts: realized sign flips via lot sign in match()
                row["realized"] += realized
                self._bump_wl(row, realized)
                leg = "spot"
            elif a == "sell_put":
                put_lots.setdefault(key, []).append([q, px])
            elif a == "buy_put_close":
                realized, _ = match(put_lots, key, q, px, -1)
                realized *= 100.0  # contracts x per-share points -> dollars
                row["realized"] += realized
                self._bump_wl(row, realized)
                leg = "short_put"
            elif a == "buy_put":
                long_put_lots.setdefault(key, []).append([q, px])
            elif a == "sell_put_close":
                realized, _ = match(long_put_lots, key, q, px, +1)
                realized *= 100.0
                row["realized"] += realized
                self._bump_wl(row, realized)
                leg = "long_put"
            elif a == "buy_call":
                long_call_lots.setdefault(key, []).append([q, px])
            elif a == "sell_call_close":
                realized, _ = match(long_call_lots, key, q, px, +1)
                realized *= 100.0
                row["realized"] += realized
                self._bump_wl(row, realized)
                leg = "long_call"
            elif a == "expiry_worthless":
                for lq, lp in put_lots.pop(key, []):
                    realized = lq * lp * 100.0
                    row["realized"] += realized
                    self._bump_wl(row, realized)
                leg = "short_put"
            elif a == "expiry_assign":
                for lq, lp in put_lots.pop(key, []):
                    realized = lq * lp * 100.0  # premium kept
                    row["realized"] += realized
                    self._bump_wl(row, realized)
                spot_lots.setdefault(key, []).append([q, px])  # shares @ strike
                leg = "short_put"
            elif a in ("expiry_put_long", "expiry_call_long"):
                lots = long_put_lots if a == "expiry_put_long" \
                    else long_call_lots
                for lq, lp in lots.pop(key, []):
                    # collected intrinsic (px) vs premium paid (lp)
                    realized = lq * (px - lp) * 100.0
                    row["realized"] += realized
                    self._bump_wl(row, realized)
                leg = "long_put" if a == "expiry_put_long" else "long_call"
            if collect_leg_series:
                delta = row["realized"] - realized_before
                if leg is not None and abs(delta) > 1e-9:
                    leg_series.append({"ts": t["ts"], "strategy": s,
                                       "leg": leg, "delta": round(delta, 2)})
            if collect_series:
                series.append({"ts": t["ts"], "strategy": s,
                               "realized": round(row["realized"], 2)})
        out = []
        for r in stats.values():
            w, l = r["wins"], r["losses"]
            r["win_rate"] = (w / (w + l)) if (w + l) else None
            r["expectancy"] = (r["realized"] / (w + l)) if (w + l) else None
            gl = r["gross_loss"]
            r["profit_factor"] = (r["gross_win"] / -gl) if gl < -1e-9 else None
            out.append(r)
        # Net open qty per (book, strategy, symbol), for the audit harness.
        # Books: spot (signed shares), put (short puts; positive = open
        # short), long_put / long_call (positive = open long).
        open_lots: dict[tuple, float] = {}
        for book_name, book in (("spot", spot_lots), ("put", put_lots),
                                ("long_put", long_put_lots),
                                ("long_call", long_call_lots)):
            for (s, sym), lots in book.items():
                net = sum(lq for lq, _ in lots)
                if abs(net) > 1e-9:
                    open_lots[(book_name, s, sym)] = net
        return (sorted(out, key=lambda r: r["net"], reverse=True),
                series, open_lots, leg_series)

    def ae_summary(self) -> list[dict]:
        """Actual-vs-expected premium per (strategy, leg, month).

        Expected: decision-time premium the signal acted on, recorded by
        the engine at every opening fill (scanner bid for premium
        collection, ask for premium paid). Realized: FIFO-matched closed
        P&L per leg. ae_pct = realized/expected*100.

        Read it on premium-collecting legs (short_put): A/E near 100%
        means the signal's premium survived fills, spread and fees;
        sustained drift below 100% is the earliest signal that edge is
        decaying or costs are eating it. ae_pct is None when there is no
        positive expected base or no closed round trips in the month --
        an open position contributes nothing until it closes.
        """
        marks = self._all(
            "SELECT strategy, leg, substr(ts, 1, 7) AS month,"
            " SUM(expected) AS expected, COUNT(*) AS n_opens"
            " FROM ae_expected GROUP BY strategy, leg, month")
        _, _, _, leg_series = self._fifo_walk(collect_leg_series=True)
        real: dict[tuple, dict] = {}
        for e in leg_series:
            k = (e["strategy"], e["leg"], e["ts"][:7])
            r = real.setdefault(k, {"realized": 0.0, "n_closes": 0})
            r["realized"] += e["delta"]
            r["n_closes"] += 1
        out = []
        keys = {(m["strategy"], m["leg"], m["month"]) for m in marks} | set(real)
        for strategy, leg, month in sorted(keys):
            m = next((x for x in marks
                      if (x["strategy"], x["leg"], x["month"])
                      == (strategy, leg, month)),
                     {"expected": 0.0, "n_opens": 0})
            r = real.get((strategy, leg, month),
                         {"realized": 0.0, "n_closes": 0})
            expected = m["expected"]
            ae_pct = None
            if expected > 1e-9 and r["n_closes"] > 0:
                ae_pct = round(r["realized"] / expected * 100.0, 1)
            out.append({"strategy": strategy, "leg": leg, "month": month,
                        "expected": round(expected, 2),
                        "realized": round(r["realized"], 2),
                        "ae_pct": ae_pct, "n_opens": m["n_opens"],
                        "n_closes": r["n_closes"]})
        return out

    def implied_open_qty(self) -> dict[tuple, float]:
        """Net open qty per (book, strategy, symbol) implied by the trade
        tape. See _fifo_walk for book conventions."""
        _, _, lots, _ = self._fifo_walk()
        return lots

    def rejection_summary(self) -> dict:
        """Aggregate arbiter rejections: total, by reason, by strategy."""
        rows = self._all(
            "SELECT reason, strategy, COUNT(*) AS n FROM risk_events "
            "GROUP BY reason, strategy ORDER BY n DESC")
        by_reason: dict[str, int] = {}
        for r in rows:
            by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + r["n"]
        return {"total": sum(by_reason.values()),
                "by_reason": by_reason,
                "by_strategy_reason": rows}

    @staticmethod
    def _bump_wl(row: dict, realized: float) -> None:
        if abs(realized) < 1e-9:
            return
        if realized > 0:
            row["wins"] += 1
            row["gross_win"] += realized
        else:
            row["losses"] += 1
            row["gross_loss"] += realized

    def portfolio_summary(self) -> dict:
        """Equity, return, and max drawdown from the equity curve."""
        curve = self.equity_curve()
        if not curve:
            return {"equity": 0.0, "cash": 0.0, "return_pct": 0.0,
                    "max_drawdown_pct": 0.0, "n_points": 0}
        eq = [c["equity"] for c in curve]
        peak, max_dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak)
        return {"equity": eq[-1], "cash": curve[-1]["cash"],
                "return_pct": (eq[-1] / eq[0] - 1) * 100 if eq[0] else 0.0,
                "max_drawdown_pct": max_dd * 100, "n_points": len(eq)}

    def close(self) -> None:
        self._db.close()

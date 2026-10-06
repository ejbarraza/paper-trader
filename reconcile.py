#!/usr/bin/env python3
"""Ledger audit harness: asserts the ledger's internal invariants.

A drift guard, NOT a correctness proof -- it shares the engine's own
semantics (FIFO books, mark conventions), so a semantic bug re-seeded
into the engine would still pass. What it catches: dropped or
double-counted cash postings, equity/mark decomposition breaks, and
position rows that disagree with the trade tape.

Checks:
  1. cash_walk -- between consecutive equity snapshots, cash moves
     exactly by the sum of trade cash_deltas in that window.
  2. equity_decomposition -- equity == cash + sum(qty*mark) at every
     snapshot ts present in both tables.
  3. open_lots -- net open qty per (kind, symbol) implied by the trade
     tape (FIFO books, aggregated over strategies) matches the latest
     position snapshot.

Usage:
    python reconcile.py --db paper.db          # exit 0 = pass, 1 = fail
    run.py backtest ... --reconcile            # audit runs at end of run
    run.py reconcile --db paper.db             # same via the CLI
"""

from __future__ import annotations

import argparse
import sqlite3

from ledger import Ledger

CASH_TOL = 0.01   # a penny: cash is dollars, float dust is not a failure
QTY_TOL = 1e-6


def _rows(db: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(sql, args)]


def check_cash_walk(db_path: str) -> tuple[bool, str]:
    """Every cash movement between equity snapshots is explained by the
    trade tape's cash_deltas."""
    db = sqlite3.connect(db_path)
    try:
        eq = _rows(db, "SELECT ts, cash FROM equity ORDER BY ts")
        if len(eq) < 2:
            return True, "fewer than 2 equity snapshots; nothing to walk"
        trades = _rows(db, "SELECT ts, cash_delta FROM trades ORDER BY ts")
        worst = 0.0
        worst_ts = ""
        prev_ts, prev_cash = eq[0]["ts"], eq[0]["cash"]
        for row in eq[1:]:
            ts, cash = row["ts"], row["cash"]
            delta = sum(t["cash_delta"] for t in trades
                        if prev_ts < t["ts"] <= ts)
            expected = prev_cash + delta
            drift = abs(cash - expected)
            if drift > worst:
                worst, worst_ts = drift, ts
            prev_ts, prev_cash = ts, cash
        if worst > CASH_TOL:
            return False, (f"cash drift ${worst:,.2f} at {worst_ts} "
                           f"(tolerance ${CASH_TOL:,.2f})")
        return True, (f"{len(eq)} snapshots walked, max drift "
                     f"${worst:,.4f}")
    finally:
        db.close()


def check_equity_decomposition(db_path: str) -> tuple[bool, str]:
    """equity == cash + sum(qty*mark) at every snapshot timestamp."""
    db = sqlite3.connect(db_path)
    try:
        eq = {r["ts"]: r for r in
              _rows(db, "SELECT ts, equity, cash FROM equity")}
        pos = _rows(db, "SELECT ts, qty, mark FROM positions")
        by_ts: dict[str, float] = {}
        for p in pos:
            by_ts[p["ts"]] = by_ts.get(p["ts"], 0.0) + p["qty"] * p["mark"]
        if not by_ts:
            return True, "no position snapshots; nothing to decompose"
        worst = 0.0
        worst_ts = ""
        n = 0
        for ts, posval in by_ts.items():
            if ts not in eq:
                continue
            n += 1
            drift = abs(eq[ts]["equity"] - (eq[ts]["cash"] + posval))
            if drift > worst:
                worst, worst_ts = drift, ts
        if worst > CASH_TOL:
            return False, (f"equity decomposition drift ${worst:,.2f} "
                           f"at {worst_ts}")
        return True, f"{n} snapshots decomposed, max drift ${worst:,.4f}"
    finally:
        db.close()


def check_open_lots(db_path: str) -> tuple[bool, str]:
    """Trade-tape-implied open qty matches the latest position snapshot.

    FIFO books are per (strategy, symbol); engine positions net across
    strategies and are per strike/expiry. Both sides are aggregated to
    (kind, symbol): kind in {spot, put, call}. Short puts are negative
    in the engine and positive in the FIFO book, hence the sign flip.
    """
    led = Ledger(db_path)
    implied: dict[tuple[str, str], float] = {}
    for (book, _strat, sym), net in led.implied_open_qty().items():
        if book == "spot":
            key, qty = ("spot", sym), net
        elif book == "put":
            key, qty = ("put", sym), -net
        elif book == "long_put":
            key, qty = ("put", sym), net
        elif book == "long_call":
            key, qty = ("call", sym), net
        else:
            continue
        implied[key] = implied.get(key, 0.0) + qty
    snap: dict[tuple[str, str], float] = {}
    for p in led.latest_positions():
        parts = p["pkey"].split(":")
        key = (parts[0], parts[1])
        snap[key] = snap.get(key, 0.0) + p["qty"]
    problems = []
    for key in sorted(set(implied) | set(snap)):
        a, b = implied.get(key, 0.0), snap.get(key, 0.0)
        if abs(a - b) > QTY_TOL:
            problems.append(f"{key[0]}:{key[1]} tape={a:g} snapshot={b:g}")
    if problems:
        return False, "; ".join(problems)
    return True, (f"{len(set(implied) | set(snap))} (kind, symbol) groups "
                  f"tie out")


CHECKS = [
    ("cash_walk", check_cash_walk),
    ("equity_decomposition", check_equity_decomposition),
    ("open_lots", check_open_lots),
]


def audit(db_path: str, verbose: bool = True) -> bool:
    """Run all checks. Returns True iff every check passes."""
    ok_all = True
    for name, fn in CHECKS:
        try:
            ok, detail = fn(db_path)
        except Exception as e:  # a broken check is itself a red flag
            ok, detail = False, f"check raised {type(e).__name__}: {e}"
        ok_all = ok_all and ok
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok_all


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit a paper-trader ledger.")
    ap.add_argument("--db", required=True)
    args = ap.parse_args()
    print(f"reconciling {args.db}")
    ok = audit(args.db)
    print("RECONCILE:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

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
        """)
        self._db.commit()

    # -- writes ---------------------------------------------------------
    def record_trade(self, ts: datetime, strategy: str, symbol: str,
                     action: str, qty: float, price: float,
                     cash_delta: float, note: str = "") -> None:
        self._db.execute(
            "INSERT INTO trades(ts,strategy,symbol,action,qty,price,cash_delta,note)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts.isoformat(), strategy, symbol, action, qty, price,
             cash_delta, note))
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
        """marks: pkey -> (qty, mark_price)."""
        iso = ts.isoformat()
        self._db.execute("DELETE FROM positions WHERE ts=?", (iso,))
        self._db.executemany(
            "INSERT INTO positions(ts,pkey,qty,mark) VALUES (?,?,?,?)",
            [(iso, k, q, m) for k, (q, m) in marks.items()])
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
        return self._all("""
            SELECT pkey,qty,mark FROM positions
            WHERE ts = (SELECT MAX(ts) FROM positions)""")

    def strategy_pnl(self) -> list[dict]:
        return self._all("""
            SELECT strategy,
                   SUM(CASE WHEN cash_delta > 0 THEN cash_delta ELSE 0 END) AS gross_in,
                   SUM(CASE WHEN cash_delta < 0 THEN cash_delta ELSE 0 END) AS gross_out,
                   SUM(cash_delta) AS net,
                   COUNT(*) AS n_trades
            FROM trades GROUP BY strategy ORDER BY net DESC""")

    def close(self) -> None:
        self._db.close()

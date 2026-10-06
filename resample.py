#!/usr/bin/env python3
"""Tick resampler: turn recorded top-of-book ticks into OHLC bars.

``PolymarketWSFeed.collect_ticks()`` writes one row per top-of-book change:
``ts,symbol,bid,ask,bid_size,ask_size,last``. This module buckets those ticks
into fixed-width bars so the recording can be backtested through the CSV
feed:

    python resample.py ticks.csv --out bars.csv --freq 60

Bar columns: ``ts,symbol,open,high,low,close,volume,bid,ask`` -- OHLC on the
mid price, plus the bucket's average bid/ask so spread-aware strategies (and
the engine's live-style fills) keep working on resampled data.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone


def resample_ticks(in_csv: str, out_csv: str, freq_s: int = 60) -> int:
    """Bucket ticks into ``freq_s``-second OHLC bars. Returns bar count."""
    buckets: dict[tuple[int, str], dict] = {}
    with open(in_csv, newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(row["ts"])
                bid, ask = float(row["bid"]), float(row["ask"])
            except (KeyError, ValueError, TypeError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            epoch = int(ts.timestamp())
            key = (epoch - epoch % freq_s, row.get("symbol", ""))
            b = buckets.get(key)
            mid = (bid + ask) / 2.0
            if b is None:
                buckets[key] = {"open": mid, "high": mid, "low": mid,
                                "close": mid, "bid": bid, "ask": ask, "n": 1}
            else:
                b["high"] = max(b["high"], mid)
                b["low"] = min(b["low"], mid)
                b["close"] = mid
                b["bid"] += bid
                b["ask"] += ask
                b["n"] += 1
    rows = []
    for (epoch, symbol), b in sorted(buckets.items()):
        rows.append({
            "ts": datetime.fromtimestamp(epoch, tz=timezone.utc)
                          .replace(tzinfo=None).isoformat(),
            "symbol": symbol,
            "open": round(b["open"], 6), "high": round(b["high"], 6),
            "low": round(b["low"], 6), "close": round(b["close"], 6),
            "volume": 0,
            "bid": round(b["bid"] / b["n"], 6),
            "ask": round(b["ask"] / b["n"], 6),
        })
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "symbol", "open", "high",
                                          "low", "close", "volume",
                                          "bid", "ask"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Resample tick CSV to bars.")
    ap.add_argument("input", help="ticks.csv from --collect")
    ap.add_argument("--out", default="bars.csv")
    ap.add_argument("--freq", type=int, default=60,
                    help="bar width in seconds")
    args = ap.parse_args()
    n = resample_ticks(args.input, args.out, args.freq)
    print(f"{n} bars -> {args.out}")


if __name__ == "__main__":
    main()

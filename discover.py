#!/usr/bin/env python3
"""Market discovery: find tradeable prediction markets instead of hand-feeding slugs.

Scans Polymarket's public Gamma API for active markets and filters to the
liquid, order-book-enabled subset a strategy loop can actually trade:

    python discover.py --min-volume 100000 --min-liquidity 10000 --max-markets 25

Prints a table of ``slug | question | volume | liquidity | endDate``.
Feed the slugs straight into ``run.py --symbols``.
"""

from __future__ import annotations

import argparse
import json


def _fnum(m: dict, key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _token_ids(m: dict) -> list:
    try:
        return json.loads(m.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return []


def is_tradeable(m: dict, min_volume: float, min_liquidity: float) -> bool:
    """Pure filter: liquid, active, and actually tradeable on the CLOB."""
    if not m.get("slug") or not m.get("question"):
        return False
    if not m.get("enableOrderBook"):
        return False
    if len(_token_ids(m)) < 2:
        return False
    if _fnum(m, "volumeNum") < min_volume:
        return False
    if _fnum(m, "liquidityNum") < min_liquidity:
        return False
    return True


def summarize(m: dict) -> dict:
    return {"slug": m["slug"], "question": m["question"],
            "volume": _fnum(m, "volumeNum"),
            "liquidity": _fnum(m, "liquidityNum"),
            "endDate": m.get("endDate")}


def fetch_markets(limit: int = 100, max_pages: int = 10) -> list[dict]:
    """Paginate the public Gamma /markets endpoint (active, open markets)."""
    import requests
    out: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"active": "true", "closed": "false",
                                 "limit": limit, "offset": offset},
                         timeout=20)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return out


def discover_markets(min_volume: float = 100_000.0,
                     min_liquidity: float = 10_000.0,
                     max_markets: int = 25) -> list[dict]:
    found = [summarize(m) for m in fetch_markets()
             if is_tradeable(m, min_volume, min_liquidity)]
    found.sort(key=lambda m: m["volume"], reverse=True)
    return found[:max_markets]


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover liquid Polymarket markets.")
    ap.add_argument("--min-volume", type=float, default=100_000.0)
    ap.add_argument("--min-liquidity", type=float, default=10_000.0)
    ap.add_argument("--max-markets", type=int, default=25)
    args = ap.parse_args()
    try:
        markets = discover_markets(args.min_volume, args.min_liquidity,
                                   args.max_markets)
    except Exception as e:
        print(f"discovery failed: {e}")
        return
    print(f"{'slug':58s} {'volume':>12s} {'liquidity':>12s}  question")
    for m in markets:
        print(f"{m['slug'][:58]:58s} {m['volume']:12,.0f} "
              f"{m['liquidity']:12,.0f}  {m['question'][:60]}")
    print(f"\n{len(markets)} markets; slugs ready for --symbols")


if __name__ == "__main__":
    main()

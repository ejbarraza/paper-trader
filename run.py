#!/usr/bin/env python3
"""Paper-trading engine CLI.

Examples:
    # backtest momentum + mean-reversion on stocks (Yahoo, free, no key)
    python run.py --feed yahoo --symbols AAPL,MSFT --strategies momentum,meanrev \\
        --start 2024-01-01 --end 2024-12-31 --capital 100000

    # paper-trade the options scanner's VRP put candidates (forward mode)
    python run.py --feed yahoo --symbols APP,PLTR --strategies vrp \\
        --scanner-dir ../options_scanner/outputs --capital 100000 --dashboard

    # deterministic backtest from CSVs in ./data
    python run.py --feed csv --csv-dir data --symbols AAA,BBB \\
        --strategies momentum --start 2024-01-01 --end 2024-06-30
"""

from __future__ import annotations

import argparse
from datetime import date

from dashboard import create_app
from engine import PaperEngine
from feeds import build_feed
from ledger import Ledger
from risk import RiskArbiter, RiskConfig
from strategies import build_strategy, describe_strategies


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Paper-trading engine (paper only).")
    ap.add_argument("--feed", default="yahoo", choices=["yahoo", "polymarket", "csv"])
    ap.add_argument("--symbols", default="AAPL,MSFT",
                    help="comma-separated symbols (slugs for polymarket)")
    ap.add_argument("--csv-dir", default="data")
    ap.add_argument("--csv-asset-class", default="stocks",
                    choices=["stocks", "options", "predictions"],
                    help="what the CSV files contain")
    ap.add_argument("--strategies", default="momentum,meanrev",
                    help="comma-separated: vrp,momentum,meanrev")
    ap.add_argument("--scanner-dir", default="../options_scanner/outputs",
                    help="options-scanner outputs dir (vrp strategy)")
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--db", default="paper.db")
    ap.add_argument("--slippage-bps", type=float, default=5.0)
    ap.add_argument("--max-drawdown", type=float, default=0.15)
    ap.add_argument("--daily-loss-limit", type=float, default=0.03)
    ap.add_argument("--dashboard", action="store_true",
                    help="serve the dashboard after the run")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--list-strategies", action="store_true",
                    help="show registered strategies and their asset classes")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_strategies:
        print(f"{'spec':10s} {'strategy':12s} {'asset classes':28s} blurb")
        for d in describe_strategies():
            print(f"{d['name']:10s} {d['strategy']:12s} "
                  f"{','.join(d['asset_classes']):28s} {d['blurb']}")
        return
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    feed_kwargs = {"directory": args.csv_dir,
                   "asset_class": args.csv_asset_class} if args.feed == "csv" else {}
    feed = build_feed(args.feed, **feed_kwargs)

    strats = []
    for spec in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        kw = {"scanner_dir": args.scanner_dir} if spec == "vrp" else {}
        strats.append(build_strategy(spec, **kw))
    print(f"feed={feed.name} symbols={symbols} "
          f"strategies={[s.name for s in strats]}")

    arbiter = RiskArbiter(RiskConfig(
        max_drawdown_frac=args.max_drawdown,
        daily_loss_limit_frac=args.daily_loss_limit))
    ledger = Ledger(args.db)
    engine = PaperEngine(feed, strats, arbiter, ledger,
                         capital=args.capital,
                         slippage_bps=args.slippage_bps)
    summary = engine.run(symbols, date.fromisoformat(args.start),
                         date.fromisoformat(args.end))
    print("run summary:", summary)

    for row in ledger.strategy_pnl():
        print(f"  {row['strategy']:12s} trades={row['n_trades']:4d} "
              f"net=${row['net']:,.2f}")

    if args.dashboard:
        print(f"dashboard: http://127.0.0.1:{args.port}")
        create_app(args.db).run(port=args.port)


if __name__ == "__main__":
    main()

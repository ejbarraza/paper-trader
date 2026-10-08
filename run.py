#!/usr/bin/env python3
"""Paper-trading engine CLI (paper only -- no live orders, ever).

Subcommands:
    run.py backtest      replay history through the engine
    run.py live          paper-forward: step the engine on live quotes
    run.py dashboard     serve the read-only dashboard
    run.py report        static HTML snapshot of a ledger (no server)
    run.py reconcile     audit a ledger's invariants (drift guard)
    run.py discover      scan Polymarket for liquid tradeable markets
    run.py resample      ticks.csv -> OHLC bars
    run.py collect       record live Polymarket websocket ticks
    run.py strategies    list available strategies
    run.py interactive   guided setup: pick strategies, answer prompts

Examples:
    # backtest momentum + mean-reversion on stocks (Yahoo, free, no key)
    run.py backtest --feed yahoo --symbols AAPL,MSFT \\
        --strategies momentum,meanrev --start 2024-01-01 --end 2024-12-31

    # buy-and-hold benchmark pie from a file (or --pie "AAPL:30,MSFT:30")
    run.py backtest --pie pies/qqq.json --symbols QQQ --db qqq.db \\
        --capital 10000 --max-exposure 1.0

    # config file instead of flags (CLI flags override the file)
    run.py backtest --book books/my-book.json

    # paper-trade the options scanner's VRP put candidates
    run.py backtest --feed yahoo --symbols APP,PLTR --strategies vrp \\
        --scanner-dir ../options_scanner/outputs

    # live prediction-market quotes over Polymarket's websocket (no key)
    run.py live --feed polymarket_ws --symbols <market-slug> \\
        --strategies endgame --interval 60

    # dashboard / static report from a ledger
    run.py dashboard --db paper.db --compare qqq.db
    run.py report --db paper.db --out report.html
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
from datetime import date
from types import SimpleNamespace

from dashboard import create_app
from engine import PaperEngine
from feeds import build_feed
from ledger import Ledger
from risk import RiskArbiter, RiskConfig
from strategies import PieStrategy, build_strategy, describe_strategies

STRATEGY_NAMES = ["endgame", "longvol", "meanrev", "momentum",
                  "pie", "tail", "vrp"]
FEEDS = ["yahoo", "polymarket", "polymarket_us", "polymarket_ws", "csv"]


def parse_pie(spec: str, capital: float) -> PieStrategy:
    """Build a PieStrategy from a JSON file or an inline 'SYM:pct,...' spec.
    File format: {"label": ..., "capital": ..., "allocations": {"AAPL": 30}}.
    Weights are normalized, so percentages or fractions both work."""
    if os.path.isfile(spec):
        with open(spec) as f:
            data = json.load(f)
        allocs = data["allocations"]
        label = data.get("label",
                         os.path.basename(spec).removesuffix(".json"))
        cap = float(data.get("capital", capital))
    else:
        allocs = {}
        for part in spec.split(","):
            sym, pct = part.split(":")
            allocs[sym.strip().upper()] = float(pct)
        label, cap = "pie", capital
    return PieStrategy(allocs, capital=cap, label=label)


def split_specs(raw: str) -> list[str]:
    """Split a --strategies string into specs. A chunk starts a new spec
    only if it names a known strategy, so config commas stay inside the
    spec they belong to: "pie:AAPL:30,MSFT:30,momentum" -> the pie spec
    keeps "AAPL:30,MSFT:30"."""
    specs = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        head = chunk.split(":", 1)[0].strip().lower()
        if head in STRATEGY_NAMES or not specs:
            specs.append(chunk)
        else:
            specs[-1] += "," + chunk
    return specs


def _unknown_strategy(name: str) -> SystemExit:
    sug = difflib.get_close_matches(name, STRATEGY_NAMES, n=1)
    hint = f" -- did you mean '{sug[0]}'?" if sug else ""
    return SystemExit(
        f"error: unknown strategy '{name}'{hint}\n"
        f"run `run.py strategies` for the list")


def resolve_strategies(args, symbols):
    """Build the strategy list. Nothing runs unless selected: --strategies
    must name every strategy, except --pie on its own also selects the pie
    (passing --pie IS selecting it). Inline config rides with the name:
    "pie:pies/qqq.json" or "pie:AAPL:30,MSFT:30"."""
    strats = []
    specs = split_specs(args.strategies or "")
    for spec in specs:
        name, _, cfg = spec.partition(":")
        name, cfg = name.strip().lower(), cfg.strip()
        if name == "pie":
            alloc = cfg or args.pie
            if not alloc:
                raise SystemExit(
                    "error: pie strategy needs an allocation: "
                    "--pie <file|\"AAPL:30,MSFT:30\"> or "
                    "--strategies \"pie:<file|...>\"")
            strats.append(parse_pie(alloc, args.capital))
            continue
        if name in ("vrp", "tail", "longvol"):
            kw: dict = {"scanner_dir": args.scanner_dir}
            for pair in cfg.split(",") if cfg else []:
                k, _, v = pair.partition("=")
                if not k.strip() or not v.strip():
                    raise SystemExit(
                        f"error: bad {name} config {pair!r}; "
                        f"use key=value pairs")
                kw[k.strip()] = v.strip()
        elif name == "endgame":
            if cfg:
                raise SystemExit(
                    f"error: strategy '{name}' takes no config, "
                    f"got {cfg!r}")
            kw = {"symbols": symbols}
        elif name in STRATEGY_NAMES:
            if cfg:
                raise SystemExit(
                    f"error: strategy '{name}' takes no config, "
                    f"got {cfg!r}")
            kw = {}
        else:
            raise _unknown_strategy(name)
        strats.append(build_strategy(name, **kw))
    spec_names = {s.partition(":")[0].strip().lower() for s in specs}
    if args.pie and "pie" not in spec_names:
        strats.append(parse_pie(args.pie, args.capital))
    for s in strats:
        if isinstance(s, PieStrategy):
            missing = [sym for sym in s.allocations if sym not in
                       {x.upper() for x in symbols}]
            if missing:
                print(f"[pie] warning: no bars for {missing} "
                      f"-- add them to --symbols")
    if not strats:
        if getattr(args, "default_spy", False):
            return [PieStrategy({"SPY": 100.0}, capital=args.capital,
                                label="spy-default")]
        raise SystemExit(
            "error: no strategies selected -- pass --strategies, "
            "--pie, or --book (see `run.py strategies`)")
    return strats


def ensure_default_position(args, symbols: list[str]) -> list[str]:
    """The account's resting state is long SPY: with nothing selected on a
    stock-capable feed, default to a buy-and-hold SPY pie (SPY is added to
    the symbols). On feeds where SPY is not tradeable there is no
    sensible default, so it stays an error. Returns the symbol list."""
    if (args.strategies or "").strip() or args.pie:
        return symbols
    if args.feed in ("yahoo", "csv"):
        args.default_spy = True
        if "SPY" not in {s.upper() for s in symbols}:
            symbols = symbols + ["SPY"]
        print("no strategies selected -- defaulting to buy-and-hold SPY "
              "(pass --strategies to choose)")
        return symbols
    raise SystemExit(
        "error: no strategies selected and no default position exists "
        f"for the {args.feed} feed -- pass --strategies, --pie, or --book "
        "(see `run.py strategies`)")


def normalize_symbols(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def apply_book(args, parser):
    """Overlay a --book JSON file: any CLI flag left at its default takes
    the book's value; explicitly passed flags win."""
    if not getattr(args, "book", None):
        return args
    with open(args.book) as f:
        book = json.load(f)
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    subparser = sub.choices[args.cmd]
    defaults = {a.dest: a.default for a in subparser._actions
                if a.dest != "help"}
    for key, val in book.items():
        if key not in defaults:
            raise SystemExit(
                f"error: book {args.book}: unknown key {key!r}")
        if getattr(args, key, None) == defaults[key]:
            setattr(args, key, val)
    return args


def _add_run_args(p):
    p.add_argument("--feed", default="yahoo", choices=FEEDS)
    p.add_argument("--symbols", default="AAPL,MSFT",
                   help="comma-separated symbols (slugs for polymarket)")
    p.add_argument("--strategies", default="",
                   help="comma-separated strategy names; inline config "
                        "allowed, e.g. \"pie:pies/qqq.json,momentum\" "
                        "(see `run.py strategies`)")
    p.add_argument("--pie", default=None,
                   help="buy-and-hold pie: JSON pie file or inline "
                        "'AAPL:30,MSFT:30'. Passing --pie selects the pie "
                        "strategy (no need to also name it).")
    p.add_argument("--book", default=None,
                   help="JSON run book (strategies, symbols, capital, "
                        "dates...); CLI flags override the file")
    p.add_argument("--scanner-dir", default="../options_scanner/outputs",
                   help="options-scanner outputs dir (vrp/tail/longvol)")
    p.add_argument("--csv-dir", default="data")
    p.add_argument("--csv-asset-class", default="stocks",
                   choices=["stocks", "options", "predictions"])
    p.add_argument("--capital", type=float, default=100_000.0)
    p.add_argument("--db", default="paper.db")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--max-drawdown", type=float, default=0.15)
    p.add_argument("--daily-loss-limit", type=float, default=0.03)
    p.add_argument("--max-exposure", type=float, default=0.80,
                   help="max portfolio exposure as a fraction of equity "
                        "(use ~1.0 for a fully-invested pie)")
    p.add_argument("--reconcile", action="store_true",
                   help="audit the ledger at end of run (cash walk, equity "
                        "decomposition, open-lot tie-out); fails the run "
                        "on drift. Also available as a --book key.")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="run.py", description="Paper-trading engine (paper only).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backtest", help="replay history through the engine")
    _add_run_args(p)
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2024-12-31")

    p = sub.add_parser("live", help="paper-forward on live quotes")
    _add_run_args(p)
    p.add_argument("--interval", type=float, default=60.0,
                   help="seconds between live steps")
    p.add_argument("--duration", type=float, default=0,
                   help="run length in seconds (0 = until Ctrl-C)")
    p.add_argument("--alert-url", default="",
                   help="webhook URL for session/kill-switch alerts "
                        "(e.g. Slack, Discord, ntfy)")
    p.add_argument("--dashboard", action="store_true",
                   help="serve the dashboard while the live run continues")
    p.add_argument("--port", type=int, default=5000)

    p = sub.add_parser("dashboard", help="serve the read-only dashboard")
    p.add_argument("--db", default="paper.db")
    p.add_argument("--compare", default=None,
                   help="second ledger DB to overlay (indexed to 100)")
    p.add_argument("--port", type=int, default=5000)

    p = sub.add_parser("report", help="static HTML snapshot of a ledger")
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--compare", default=None)

    p = sub.add_parser("iv-hv",
                       help="plot IV vs HV for a ticker (Yahoo, no creds)")
    p.add_argument("ticker")
    p.add_argument("--days", type=int, default=365,
                   help="lookback for the HV regime panel")
    p.add_argument("--risk-free", type=float, default=0.04)
    p.add_argument("--out", default=None,
                   help="PNG path (default outputs/ivhv_<ticker>_<date>.png)")

    sub.add_parser("reconcile",
                   help="audit a ledger's invariants").add_argument(
                       "--db", required=True)

    p = sub.add_parser("panoptic-lab",
                       help="simulate a Panoptic-style perpetual short put "
                            "(Binance, no creds)")
    p.add_argument("ticker", nargs="?", default="BTC")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--otm", type=float, default=0.05,
                   help="range top below spot, fraction")
    p.add_argument("--width", type=float, default=0.10,
                   help="range width as fraction of range top")
    p.add_argument("--iv", type=float, default=None,
                   help="implied vol (default: 30d HV of the path)")
    p.add_argument("--notional", type=float, default=10000)
    p.add_argument("--dte", type=int, default=30,
                   help="TradFi comparison put expiry, days")
    p.add_argument("--risk-free", type=float, default=0.04)
    p.add_argument("--out", default=None,
                   help="PNG path (default outputs/panoptic_<ticker>_<date>.png)")

    p = sub.add_parser("discover",
                       help="scan Polymarket for liquid tradeable markets")
    p.add_argument("--min-volume", type=float, default=100_000.0)
    p.add_argument("--min-liquidity", type=float, default=10_000.0)
    p.add_argument("--max-markets", type=int, default=25)

    p = sub.add_parser("resample", help="ticks.csv -> OHLC bars")
    p.add_argument("ticks", help="input ticks CSV (from `run.py collect`)")
    p.add_argument("--out", default="bars.csv")
    p.add_argument("--freq", type=int, default=60,
                   help="bar width in seconds")

    p = sub.add_parser("collect",
                       help="record live Polymarket websocket ticks to CSV")
    p.add_argument("--symbols", required=True,
                   help="comma-separated market slugs")
    p.add_argument("--seconds", type=float, required=True,
                   help="how long to record")
    p.add_argument("--out", default="ticks.csv")

    sub.add_parser("strategies", help="list available strategies")
    sub.add_parser("interactive", help="guided setup: answer prompts")
    return ap


def _build_feed(args, symbols):
    feed_kwargs = {"directory": args.csv_dir,
                   "asset_class": args.csv_asset_class} \
        if args.feed == "csv" else {}
    if args.feed == "polymarket_ws":
        feed_kwargs = {"symbols": symbols}
    return build_feed(args.feed, **feed_kwargs)


def _print_summary(ledger):
    for row in ledger.strategy_pnl():
        print(f"  {row['strategy']:12s} trades={row['n_trades']:4d} "
              f"net=${row['net']:,.2f}")


def run_backtest(args, symbols) -> None:
    feed = _build_feed(args, symbols)
    strats = resolve_strategies(args, symbols)
    print(f"feed={feed.name} symbols={symbols} "
          f"strategies={[s.name for s in strats]}")
    arbiter = RiskArbiter(RiskConfig(
        max_drawdown_frac=args.max_drawdown,
        daily_loss_limit_frac=args.daily_loss_limit,
        max_portfolio_exposure_frac=args.max_exposure))
    ledger = Ledger(args.db)
    engine = PaperEngine(feed, strats, arbiter, ledger,
                         capital=args.capital,
                         slippage_bps=args.slippage_bps)
    summary = engine.run(symbols, date.fromisoformat(args.start),
                         date.fromisoformat(args.end))
    print("run summary:", summary)
    _print_summary(ledger)


def run_live(args, symbols) -> None:
    feed = _build_feed(args, symbols)
    strats = resolve_strategies(args, symbols)
    print(f"feed={feed.name} symbols={symbols} "
          f"strategies={[s.name for s in strats]}")
    arbiter = RiskArbiter(RiskConfig(
        max_drawdown_frac=args.max_drawdown,
        daily_loss_limit_frac=args.daily_loss_limit,
        max_portfolio_exposure_frac=args.max_exposure))
    ledger = Ledger(args.db)
    engine = PaperEngine(feed, strats, arbiter, ledger,
                         capital=args.capital,
                         slippage_bps=args.slippage_bps)
    if args.dashboard:
        import threading
        threading.Thread(
            target=lambda: create_app(args.db).run(port=args.port),
            daemon=True).start()
        print(f"dashboard: http://127.0.0.1:{args.port} (live)")
    summary = engine.run_live(
        symbols, interval_s=args.interval,
        duration_s=args.duration or None,
        alert_url=args.alert_url or None)
    print("run summary:", summary)
    _print_summary(ledger)


def cmd_strategies() -> None:
    print(f"{'spec':10s} {'strategy':12s} {'asset classes':28s} blurb")
    for d in describe_strategies():
        print(f"{d['name']:10s} {d['strategy']:12s} "
              f"{','.join(d['asset_classes']):28s} {d['blurb']}")


def cmd_discover(args) -> None:
    from discover import discover_markets
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
    print(f"\n{len(markets)} markets; pass slugs via --symbols")


def cmd_resample(args) -> None:
    from resample import resample_ticks
    n = resample_ticks(args.ticks, args.out, args.freq)
    print(f"{n} bars -> {args.out} "
          f"(backtest with --feed csv --csv-dir "
          f"{args.out.rsplit('/', 1)[0] or '.'})")


def cmd_collect(args) -> None:
    symbols = normalize_symbols(args.symbols)
    feed = build_feed("polymarket_ws", symbols=symbols)
    path, n = feed.collect_ticks(symbols, args.seconds, args.out)
    feed.stop()
    print(f"recorded {n} ticks -> {path}")


def cmd_dashboard(args) -> None:
    print(f"dashboard: http://127.0.0.1:{args.port}")
    create_app(args.db, args.compare).run(port=args.port)


def cmd_report(args) -> None:
    from report import build
    html = build(args.db, args.compare)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"wrote {args.out} ({len(html) // 1024} KB)")


def cmd_iv_hv(args) -> None:
    from ivhv import main as ivhv_main
    ivhv_main([args.ticker, "--days", str(args.days),
               "--risk-free", str(args.risk_free)] +
              (["--out", args.out] if args.out else []))


def cmd_panoptic_lab(args) -> None:
    from panoptic_lab import main as panoptic_main
    panoptic_main([args.ticker, "--days", str(args.days),
                   "--otm", str(args.otm), "--width", str(args.width),
                   "--notional", str(args.notional), "--dte", str(args.dte),
                   "--risk-free", str(args.risk_free)] +
                  (["--iv", str(args.iv)] if args.iv is not None else []) +
                  (["--out", args.out] if args.out else []))


def cmd_reconcile(args) -> None:
    from reconcile import audit
    print(f"reconciling {args.db}")
    ok = audit(args.db)
    print("RECONCILE:", "PASS" if ok else "FAIL")
    if not ok:
        raise SystemExit(1)


def _pick(label: str, options: list[str], default: str) -> str:
    print(f"{label}:")
    for i, o in enumerate(options, 1):
        print(f"  [{i}] {o}" + (" (default)" if o == default else ""))
    while True:
        raw = input(f"Choose [default: {default}]: ").strip() or default
        if raw in options:
            return raw
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  invalid choice, try again")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw or default


def cmd_interactive() -> None:
    print("Paper trader setup -- Enter accepts the [default].\n")
    mode = _pick("Mode", ["backtest", "live"], "backtest")
    feed = _pick("Feed", FEEDS, "yahoo")
    symbols = _prompt("Symbols (comma-separated)", "AAPL,MSFT")
    print("Strategies:")
    infos = describe_strategies()
    for i, d in enumerate(infos, 1):
        print(f"  [{i}] {d['name']:10s} {d['blurb']}")
    specs = []
    for tok in _prompt("Pick strategies (numbers or names, "
                       "comma-separated)").split(","):
        tok = tok.strip()
        if not tok:
            continue
        name = infos[int(tok) - 1]["name"] \
            if tok.isdigit() and 1 <= int(tok) <= len(infos) else tok.lower()
        if name == "pie":
            alloc = _prompt("  pie allocation (JSON file or "
                            "'AAPL:30,MSFT:30')")
            specs.append(f"pie:{alloc}" if alloc else "pie")
        else:
            specs.append(name)
    capital = float(_prompt("Capital", "100000"))
    db = _prompt("Ledger DB", "paper.db")
    ns = SimpleNamespace(
        feed=feed, symbols=symbols, strategies=",".join(specs), pie=None,
        book=None, scanner_dir="../options_scanner/outputs", csv_dir="data",
        csv_asset_class="stocks", capital=capital, db=db, slippage_bps=5.0,
        max_drawdown=0.15, daily_loss_limit=0.03, max_exposure=0.80,
        start=_prompt("Start date", "2024-01-01") if mode == "backtest" else "",
        end=_prompt("End date", "2024-12-31") if mode == "backtest" else "",
        interval=60.0, duration=0.0, alert_url="", dashboard=False, port=5000)
    syms = normalize_symbols(symbols)
    syms = ensure_default_position(ns, syms)
    print(f"\nmode={mode} feed={feed} symbols={syms} "
          f"strategies={specs or 'SPY (default)'} capital={capital} db={db}")
    if _prompt("Run? [Y/n]", "Y").lower().startswith("n"):
        print("cancelled")
        return
    if mode == "backtest":
        run_backtest(ns, syms)
    else:
        run_live(ns, syms)


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd
    if cmd == "strategies":
        cmd_strategies()
    elif cmd == "discover":
        cmd_discover(args)
    elif cmd == "resample":
        cmd_resample(args)
    elif cmd == "collect":
        cmd_collect(args)
    elif cmd == "dashboard":
        cmd_dashboard(args)
    elif cmd == "report":
        cmd_report(args)
    elif cmd == "iv-hv":
        cmd_iv_hv(args)
    elif cmd == "panoptic-lab":
        cmd_panoptic_lab(args)
    elif cmd == "reconcile":
        cmd_reconcile(args)
    elif cmd == "interactive":
        cmd_interactive()
    elif cmd in ("backtest", "live"):
        args = apply_book(args, parser)
        symbols = normalize_symbols(args.symbols)
        symbols = ensure_default_position(args, symbols)
        if cmd == "backtest":
            run_backtest(args, symbols)
        else:
            run_live(args, symbols)
        if args.reconcile:
            from reconcile import audit
            print(f"reconciling {args.db}")
            ok = audit(args.db)
            print("RECONCILE:", "PASS" if ok else "FAIL")
            if not ok:
                raise SystemExit(1)


if __name__ == "__main__":
    main()

# Paper Trader

A **paper-trading engine** in Python: multiple strategy loops propose trades,
one shared risk arbiter disposes, fills are simulated, and everything lands in
a SQLite ledger behind a local dashboard. ~1,500 lines, stdlib + pandas/numpy,
yfinance, Flask.

Architecturally inspired by EVPoly's design (strategy loops, a shared
risk arbiter, a sqlite tracking DB, an operator dashboard) — **all code here
is original**. Paper only: there is no code path that can place a live order,
hold keys, or touch a wallet. That is deliberate, not a missing feature.

## Architecture

```
                ┌──────────────┐
   Yahoo ──►    │              │    ┌────────────┐
   Polymarket ─►│    FEEDS     │    │ STRATEGIES │── signals ──►┐
   CSV ──►      │ (pluggable)  │    │ vrp/momentum/meanrev      │
                └──────────────┘    └────────────┘              │
                                                              ▼
                                                     ┌────────────────┐
                                                     │  RISK ARBITER  │── approved?
                                                     │ (one choke     │── blocked + named reason
                                                     │  point)        │
                                                     └────────────────┘
                                                              │ fills
                                                              ▼
                                                     ┌────────────────┐
                                                     │ PAPER ENGINE   │──► SQLite ledger ──► dashboard
                                                     │ slippage+fees, │
                                                     │ expiry/assign  │
                                                     └────────────────┘
```

- **Feeds** (`feeds.py`) — one interface, five sources: Yahoo Finance
  (stocks, free), Polymarket's public Gamma API (prediction markets, free),
  **Polymarket US** via `gateway.polymarket.us` (the separate CFTC-regulated
  fiat venue — different hosts and market structure from .com; no public
  history endpoint, so it's live/paper-forward only), **Polymarket WebSocket**
  (`polymarket_ws` — the public CLOB market channel, tick-by-tick order
  books with no polling and no key; `slug` = YES token, `slug:NO` = NO
  token), CSV files (deterministic backtests). A feed that can't produce
  data returns empty; the engine skips the symbol loudly instead of trading
  on stale air.
- **Strategies** (`strategies.py`) — each sees one bar plus a read-only view
  (cash, equity, positions, recent closes) and emits signals. Strategies know
  nothing about risk; that separation is the point. Every strategy declares
  the asset classes it can trade (`vrp` → options; `momentum`/`meanrev` →
  stocks + predictions), and the engine refuses to run a strategy on a feed
  it doesn't understand — loudly, never silently.
  - `vrp` **[options]** — sells the top-VRP cash-secured puts from the
    [options-scanner](https://github.com/ejbarraza/Options-Scanner) output,
    takes profit at 50% of premium, holds the rest to expiry week. Marks come
    from Black-Scholes on entry IV (documented assumption).
  - `momentum` **[stocks, predictions]** — golden/death-cross trend following,
    long-only.
  - `meanrev` **[stocks, predictions]** — z-score fade, long or short, with a
    hard stop.
- **Feeds** declare what they provide (`yahoo` → stocks, `polymarket` →
  predictions, `csv` → your choice via `--csv-asset-class`). See them with
  `python run.py --list-strategies`.
- **Risk arbiter** (`risk.py`) — every signal passes through: per-strategy
  budgets, per-symbol and portfolio exposure caps, a price-band anti-chase
  rule, and two kill-switches (daily loss, max drawdown). Exits always pass
  the kill-switches — risk management must never trap you in a position.
  Rejections carry machine-readable reasons (`strategy_budget_exceeded`,
  `drawdown_killswitch`, …) straight into the ledger.
- **Engine** (`engine.py`) — bar-by-bar replay *or* paper-forward live mode:
  settles expiries (assignment or worthless), collects signals, arbitrates,
  simulates fills with slippage and fees (live fills cross the real
  bid/ask), marks to market, writes the ledger. Both modes share one
  `_step()` so backtest and live can't drift apart.
- **Dashboard** (`dashboard.py`) — read-only Flask app: equity curve,
  positions, per-strategy P&L, trade tape, and the risk-event log.

## Quickstart

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

# backtest two classic loops on stocks (Yahoo, no key needed)
./.venv/bin/python run.py --feed yahoo --symbols AAPL,MSFT \
  --strategies momentum,meanrev --start 2024-01-01 --end 2024-12-31 \
  --capital 100000

# paper-trade the scanner's VRP put candidates, then open the dashboard
./.venv/bin/python run.py --feed yahoo --symbols APP,PLTR --strategies vrp \
  --scanner-dir ../options_scanner/outputs --capital 100000 --dashboard
# → http://127.0.0.1:5000
```

Tune risk without touching code:

```bash
./.venv/bin/python run.py --feed csv --csv-dir data --symbols AAA \
  --strategies momentum --max-drawdown 0.10 --daily-loss-limit 0.02 \
  --slippage-bps 10
```

Record live prediction-market ticks, then backtest them:

```bash
# stream the real order book for 60s (no key; YES token by default)
./.venv/bin/python run.py --feed polymarket_ws \
  --symbols will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \
  --collect 60 --out ticks.csv
# ticks.csv holds top-of-book changes: ts,symbol,bid,ask,bid_size,ask_size,last
# resample to bars (e.g. with pandas) and backtest through --feed csv
```

Paper-forward mode — trade live quotes as they print (paper only, Ctrl-C to stop):

```bash
./.venv/bin/python run.py --feed polymarket_ws \
  --symbols will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \
  --strategies momentum,meanrev --live --interval 60 --duration 3600 \
  --dashboard
# → http://127.0.0.1:5000 shows the equity curve updating live
```

How it works: every `--interval` seconds each symbol is quoted; the mid
becomes the bar's close **with the real bid/ask attached**, so simulated
fills cross the actual spread (buys lift the ask, sells hit the bid) instead
of using a synthetic slippage around the close. Strategies warm up from feed
history when the feed has one (Yahoo does; the websocket warms up live).
Everything still passes through the shared risk arbiter and lands in the
same SQLite ledger the dashboard reads.

## Repo layout

```
feeds.py       # MarketDataFeed interface: yahoo / polymarket (.com REST) /
               #   polymarket_us / polymarket_ws (live order-book socket) / csv
strategies.py  # Strategy interface + vrp, momentum, meanrev loops
risk.py        # RiskArbiter: budgets, exposure caps, kill-switches
engine.py      # PaperEngine: event loop, fills, expiry, marks
pricing.py     # Black-Scholes put pricer (option marks)
ledger.py      # SQLite: trades, equity, risk_events, position snapshots
dashboard.py   # read-only Flask operator view
run.py         # CLI
```

## Adding your own

A strategy is ~30 lines:

```python
from strategies import Strategy, Signal

class MyStrategy(Strategy):
    name = "mine"
    def on_bar(self, bar, ctx):
        if some_condition(bar, ctx):
            return [Signal(self.name, bar.symbol, "buy", 100,
                           note="why")]
        return []
```

A feed is two methods: `history(symbol, start, end)` and `latest_quote(symbol)`.
Register both in the `build_*` tables and they work everywhere.

## Honest limits

- Option marks use Black-Scholes with **entry-time IV held constant**; there
  are no historical chains, so intraday option P&L is modeled, not observed.
- Fills assume infinite liquidity at the quoted price plus slippage — real
  markets are worse. This is a strategy laboratory, not a promise.
- Backtests are in-sample by construction when you tune on them. Don't.

## Disclaimer

Paper trading only. Nothing here can place a live order. Not financial advice.

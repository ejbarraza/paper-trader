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
  the asset classes it can trade (`vrp`/`tail`/`longvol` → options;
  `momentum`/`meanrev` → stocks + predictions; `pie` → stocks), and the
  engine refuses to
  run a strategy on a feed it doesn't understand — loudly, never silently.
  - `vrp` **[options]** — sells the top-VRP cash-secured puts from the
    [options-scanner](https://github.com/ejbarraza/Options-Scanner) output,
    takes profit at 50% of premium, holds the rest to expiry week. Marks come
    from Black-Scholes on entry IV (documented assumption).
  - `tail` **[options]** — put ratio backspread (short 1 higher-strike put /
    long 2 lower-strike puts, same expiry) from the same scanner puts: the
    long-convexity leg of the barbell. Long gamma, long vega — small bleed
    if the market sits still, large payoff on a crash. The VRP leg's premium
    is meant to fund it. Legs are emitted long-first so a partial arbiter
    rejection can't strand a naked short, and the spread is managed atomically
    (a repair check closes any short whose wing failed to fill).
  - `longvol` **[options]** — buys long-dated (LEAPS) calls and puts from
    the scanner's `_leaps.csv`, most at-the-money first (max gamma/vega per
    contract). Pure long-vol convexity: max loss is the premium, no margin,
    no assignment. Takes profit on doubles, exits inside one year to expiry.
  - `momentum` **[stocks, predictions]** — golden/death-cross trend following,
    long-only.
  - `meanrev` **[stocks, predictions]** — z-score fade, long or short, with a
    hard stop.
  - `endgame` **[predictions]** — the mechanical leg of the classic
    endgame trade: in a market's final window, buys the Yes (or No) ask at
    ≤5¢ and holds to resolution; the engine settles via the feed's
    `settlements()`. No outcome oracle is modeled — the backtest hit-rate
    is the honest metric.
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
  - `pie` **[stocks]** — buy-and-hold benchmark pie (M1-style): on the first
    bar for each symbol in the allocation, buys `floor(capital × weight /
    price)` shares and holds. No rebalancing, no exits. Give it an
    allocation with `--pie "AAPL:30,MSFT:30,VTI:40"` or `--pie pies/tech-leaders.json`
    (see `pies/` for samples); it is sized to `--capital`. Run it as the whole
    account and the equity curve is the pie's balance over time.
- **Dashboard** (`dashboard.py`) — read-only Flask app: equity curve,
  positions, per-strategy P&L, trade tape, and the risk-event log. The
  **Edge** panel charts each strategy's cumulative *realized* P&L (closed
  trades only) with expectancy per trade and profit factor — the honest
  answer to "do I have edge". `dashboard.py --compare other.db` overlays a
  second account's equity curve (indexed to 100), e.g. a buy-and-hold pie
  benchmark against your active strategies.

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
same SQLite ledger the dashboard reads. For unattended runs, pass
`--alert-url <webhook>` — the engine POSTs JSON on session start/stop and
on the first kill-switch trip of each reason, and writes `heartbeat.json`
next to the DB every step so the dashboard's banner reads LIVE/STALE.

Find tradeable markets instead of hand-feeding slugs:

```bash
./.venv/bin/python run.py --discover --min-volume 100000 \
  --min-liquidity 10000 --max-markets 25
```

## Testing

```bash
./.venv/bin/python -m pytest tests/ -q   # 60+ tests, no network needed
```

Covers the feed registry and WS book parser (synthetic messages), strategy
signal logic, every arbiter rule, spread-aware fills, backtest regression,
the live loop (fake feed), FIFO attribution, settlement parsing, the tick
resampler, and discovery filters.

## Repo layout

```
feeds.py       # MarketDataFeed interface: yahoo / polymarket (.com REST) /
               #   polymarket_us / polymarket_ws (live order-book socket) / csv
strategies.py  # Strategy interface + vrp, momentum, meanrev, endgame loops
risk.py        # RiskArbiter: budgets, exposure caps, kill-switches
engine.py      # PaperEngine: backtest replay + paper-forward live loop,
               #   spread-aware fills, option expiry, prediction settlement
pricing.py     # Black-Scholes put pricer (option marks)
ledger.py      # SQLite: trades, equity, risk_events, position snapshots,
               #   FIFO attribution, portfolio summary
dashboard.py   # read-only Flask operator view (health, attribution)
resample.py    # tick CSV -> OHLC bars for backtesting recordings
discover.py    # Gamma scan for liquid, order-book-enabled markets
tests/         # pytest suite (no network)
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
- Prediction-market settlement only fires on **decisive** Gamma resolutions;
  ambiguous markets stay marked at last price. The endgame loop has no
  outcome oracle — its paper P&L includes markets that resolve against the
  position, which is the point: the hit-rate is the metric.
- Backtests are in-sample by construction when you tune on them. Don't.

## Disclaimer

Paper trading only. Nothing here can place a live order. Not financial advice.

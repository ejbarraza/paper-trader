# A/E Tracker — actual vs expected premium

## What it is

For every opening fill, the engine records the **decision-time expected
premium** — the quote the strategy's signal acted on — into the ledger's
`ae_expected` table. `Ledger.ae_summary()` then compares that against the
**FIFO realized P&L** per strategy leg, aggregated monthly:

```
A/E % = realized / expected × 100
```

On a premium-collecting leg (short puts), A/E near 100% means the premium
the signal saw survived fills, spread crossing, slippage and fees. Sustained
drift below 100% is the earliest signal that edge is decaying or costs are
eating it — it shows up here before it shows up in cumulative P&L, because
cumulative P&L mixes edge with position sizing and luck.

## How expected is measured

`signal.limit_price` is the quote the strategy acted on:

| leg | signal | expected (signed dollars) |
|---|---|---|
| `short_put` | `sell_put` | + bid × contracts × 100 (scanner bid) |
| `long_put` | `buy_put` | − ask × contracts × 100 (scanner ask) |
| `long_call` | `buy_call` | − ask × contracts × 100 (scanner ask) |
| `spot` | `buy` / `sell` | ∓/± limit × qty (signed decision-time notional) |

The fill then crosses the spread (`_est_fill`: lift ask / hit bid, ±
slippage) and pays fees. The gap between this mark and realized is exactly
what A/E measures. Marks are skipped when a signal carries no limit price
(expected premium unknown — e.g. spot loops that don't set one).

Note the asymmetry with the TradFi comparison in `panoptic_lab.py`: there,
"expected" is a modeled theta stream. Here it is the actual quoted price
the strategy decided on. No model in between.

## Reading the numbers

`ae_summary()` returns one row per (strategy, leg, month):

- `expected` — summed decision-time premium for opens that month
- `realized` — FIFO-matched closed P&L for that leg that month
- `ae_pct` — realized/expected×100, or `None` when there is no positive
  expected base or no closed round trips that month
- `n_opens`, `n_closes`

Interpretation rules:

1. **Only closed round trips count.** An open position contributes nothing
   until it closes — a month with opens but no closes shows `ae_pct: None`,
   not 0%. Don't read drift into it.
2. **The ratio is designed for premium-collecting legs** (`short_put`,
   expected > 0). For premium-paying legs (`long_put`/`long_call`,
   expected < 0) no ratio is computed; compare realized against the premium
   paid directly.
3. **Aggregate before judging.** A single month on a slow leg is noisy
   (opens in January, closes in March). Sum expected and realized over a
   quarter or the leg's lifetime, then take the ratio. The report shows
   monthly rows; the judgment lives in the trailing aggregate.
4. **A/E < 100% sustained is the signal.** One bad month is variance.
   Three months sliding from 98% → 91% → 84% on the vrp leg means the
   scanner's mid-price edge is not surviving contact with the market —
   widen the width gate, raise `min_vrp`, or accept the edge is gone.

## In the report

`run.py report` renders an "Actual vs expected premium" section: one row
per (strategy, leg, month) with the A/E % color-coded (green ≥ 100%,
red < 90%, grey when undefined). On old DBs (no `ae_expected` rows) the
section shows a placeholder instead of failing.

## Schema notes

- `ae_expected(trade_id PK, ts, strategy, symbol, leg, expected)` is created
  with `IF NOT EXISTS` — old DBs gain the table on first open with the new
  code; old rows are untouched.
- `record_trade()` now returns the inserted row id (previously returned
  `None`); all existing callers ignore it, so this is backward compatible.
- The per-leg realized series comes from `_fifo_walk(collect_leg_series=True)`,
  which returns a 4-tuple `(stats, series, open_lots, leg_series)`; the
  three existing call sites were updated. FIFO attribution itself is
  unchanged.

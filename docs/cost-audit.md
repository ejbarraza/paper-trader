# Execution-Cost Audit — paper-trader engine + options_scanner

**Scope:** Phase 1 audit only. Read-only investigation; no source changes.
**Date:** 2026-10-08. Engine at `engine.py::_est_fill`; scanner at
`~/workspace/options_scanner/options_scanner.py` (CSP leg).

---

## 1. Fill-basis table (engine.py `_est_fill`)

| # | Action | Side | Instrument | Price basis | Spread-aware? |
|---|--------|------|-----------|-------------|---------------|
| 1 | `buy` | buy | spot | `bar.ask` if present else `bar.close`; ×(1+slippage) | **Only when the bar carries top-of-book** (live bars). Historical Yahoo bars have no bid/ask → fills at close. |
| 2 | `sell` | sell | spot | `bar.bid` if present else `bar.close`; ×(1−slippage) | Same conditional as #1. |
| 3 | `sell_put` | sell (open short) | option | `signal.limit_price` — strategies pass the scanner's **bid** (`strategies.py:176`, `:545`) — or fallback `bar.close × 0.05`; ×(1−slippage) | **Yes when `limit_price` present.** The fallback (`5% of spot` as premium) is arbitrary and undocumented. |
| 4 | `buy_put` / `buy_call` | buy (open long) | option | `signal.limit_price` — strategies pass the scanner's **ask** (`strategies.py:539`, `:682`) — or a BS-mark fallback on the signal's IV; ×(1+slippage) | **Yes when `limit_price` present.** |
| 5 | `buy_put_close` | buy (close short) | option | BS **mid** mark at entry-held IV ×(1+slippage). The strategy passes `limit_price=mark` (`strategies.py:158`) but `_est_fill` **ignores it**. | **No — always model mid.** |
| 6 | `sell_put_close` / `sell_call_close` | sell (close long) | option | BS **mid** mark at entry-held IV ×(1−slippage). `signal.limit_price` **ignored**. | **No — always model mid.** |

Defaults: slippage 5 bps (flat, no size/width dependence), `fee_per_contract`
$1.30, `fee_per_share` $0.00.

### Ambiguities / inconsistencies found

1. **Opens pay the touch; closes get mid.** A short-put round trip opens at the
   bid and closes at the BS mid — the close is systematically favorable vs
   reality (a real close lifts the ask). The engine's P&L on early-closed
   winners is overstated by roughly half the width per close. This asymmetry
   is *tested-in* (tests assert mid-based closes), not accidental.
2. **Marks use entry-time IV held constant** (`pricing.py` documents this).
   Closes are therefore doubly mid-based: model mid *and* stale IV. In a
   selloff the ask to close a short put explodes while the mid mark lags.
3. **`sell_put` fallback `bar.close × 0.05`** fires whenever a strategy omits
   `limit_price` — a magic number with no basis in the quote.
4. **Spot fills differ by mode:** live bars (bid/ask present) fill at the
   touch; historical bars fill at close. Backtests and live runs are not
   cost-comparable for spot legs.
5. **No `sell_call` (open short call) action exists** — the engine cannot open
   short calls, only short puts.

---

## 2. Quantification: how cost-aware are fills today?

**Recorded history (empirical):** 50 fills across all 8 `backtests/*.db`
($162,071 notional) — **100% are spot `buy`s on Yahoo daily bars with no
bid/ask → 0% spread-aware by count and by notional.** No persisted DB
contains option fills (the APP scanner smoke tests did not persist ledgers).

**By code path (what a scanner-driven VRP run would do):**

- Option **opens** (`sell_put`, `buy_put`, `buy_call`): spread-aware — they
  fill at the scanner's bid/ask via `limit_price`.
- Option **closes** (`buy_put_close`, `sell_put_close`, `sell_call_close`):
  mid-based — always BS mid ± slippage.
- Spot: spread-aware only with top-of-book bars, else close.

**Bottom line:** the engine is *half* cost-aware. The open leg of the actual
put-selling loop pays the touch; the close leg receives mid. The
"already largely cost-aware, modeling collapses to reporting" hypothesis is
**false for closes** — that is where the mispricing lives.

---

## 3. Scanner side: does the VRP ranking account for width?

**Partially — the economics do, the default ranking does not.**

- CSP candidate economics are already spread-aware:
  `net_premium = mid×100 − (2×fee_per_contract + half_width×100)`
  = `bid×100 − 2×fees`. The `annualized_roi_net` ranking therefore prices the
  open at the bid. ✅
- **But the default ranking is `--csp-rank vrp`**, and
  `vrp = provider_IV − RV` where the provider IV is mid-based. The
  `width_cap` gate (default 15%) excludes the worst offenders, but *within*
  the cap a 3%-wide and a 14%-wide candidate rank purely on mid-IV VRP —
  width is invisible to the default sort. A bid-IV-based VRP (or an explicit
  width penalty) **would** re-rank candidates: wide-but-high-IV names would
  fall relative to tight names.
- Debit-spread legs already carry width-aware gates
  (`min_debit_width_mult` on combined half-widths). ✅
- Cross-check engine↔scanner: engine open fills at the scanner's bid, and the
  scanner's net premium is bid − fees — **these two agree**. The engine's
  close (BS mid) agrees with neither.

---

## 4. Calibration notes (what a spread-only model still gets wrong)

- A spread-only cost model (half-width as slippage) assumes you trade **at
  the touch**. The scanner's own width observations (BRUN ~48%, FIGR 24–28%,
  RDDT 8–111%) imply single-contract quoted size — at any real size you walk
  the book, and the touch is fiction. The scanner records `volume` and
  `openInterest` per leg, but no cost model consumes them.
- Engine slippage is a flat 5 bps regardless of width or size; there is no
  size-dependent slippage anywhere.
- **Recommendation:** document the single-contract assumption explicitly
  rather than model book-walking without data. There is no evidence in-repo
  to calibrate a depth model — building one would be invention, not
  calibration.

---

## Verdict: is the modeling phase needed, and what should it cover?

**Yes, but scoped — not a general cost-model build:**

1. **Close-leg fill basis (needed).** Model option closes at the touch: ask
   to close shorts (`buy_put_close`), bid to close longs
   (`sell_put_close`/`sell_call_close`) — e.g., BS mid ± half-width using the
   entry width or a width model. This is the single biggest measured gap:
   every early-closed winner is currently overstated by ~half the width.
2. **Scanner default ranking (needed, small).** Make the default VRP ranking
   width-aware — bid-based IV or a width penalty — so the rank order reflects
   harvestable edge, not mid-IV. The `roi` ranking path already does this;
   the default `vrp` path does not.
3. **Size/depth (not needed now).** No data to calibrate; document the
   single-contract assumption. Revisit only with real fill-size evidence.
4. **Not needed:** the open leg (already bid/ask via `limit_price`), the
   scanner's net-ROI math (already half-width aware), debit-spread width
   gates (already exist).

**Explicitly out of scope for modeling:** changing the open-leg behavior,
   re-blessing any golden dataset (that belongs to the ledger-oracle work),
   and any change to `photocraft` (excluded per standing constraint —
   unrelated here, noted for completeness).

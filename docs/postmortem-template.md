# Post-mortem template

Use this after any bad period for a strategy leg, any arbiter kill-switch
fire, or any run whose numbers surprise you. Fill it from the flight
recorder bundle (`<db>.flight.json`), the ledger (`run.py report`,
`ae_summary()`), and the reconciled cash walk — **facts before narrative**.
Copy this file, don't edit it in place.

---

## 1. Summary

- Date of incident / period under review:
- Strategy leg(s) affected:
- Ledger DB + flight bundle:
- One-line verdict (what happened, in numbers):

## 2. Factual timeline

Timestamped, no interpretation. Every entry should be checkable in the
ledger or the flight bundle.

- <ts> — <what the tape shows: fills, rejects, settlements, equity marks>
- …

Prompts: when did the leg last print a green month? What did A/E do in the
months before the bad period? Did the flight bundle's config/manifest/feed
change between the last good run and this one? (`config_hash`,
`manifest_hash`, `git_sha`.)

## 3. Findings

Numbered, each tied to timeline entries. Distinguish:

- **Mechanical** (booking bug, stale scanner input, wrong feed params —
  check `reconcile` first),
- **Cost** (A/E drift: expected premium not surviving spread/slippage/fees —
  quote the A/E % series),
- **Regime** (the edge itself decayed: VRP compressed, realized vol regime
  shifted, event risk repriced).

For each finding: evidence, and what would have detected it earlier.

## 4. Recommendations

Concrete, owned, and sequenced. Each one states the change, the file(s),
and how you'll know it worked (which metric moves, over what window).

- [ ] …
- [ ] …

Explicitly list what you are **not** changing and why (avoids thrash).

## 5. Follow-up

- Re-run date / evaluation window:
- Metric that closes this post-mortem:
- Link to the re-run's flight bundle:

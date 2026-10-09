# Flight Recorder

## What it is

Every `run.py backtest` / `run.py live` invocation writes a JSON bundle to
`<db>.flight.json` **before the engine steps** — even a crashed run leaves
its reproduction recipe behind. The bundle captures everything needed to
bit-reproduce the run:

```json
{
  "db": "/abs/path/to/run.db",
  "command": "backtest",
  "started_at": "2026-10-08T18:00:00+00:00",
  "engine": {"git_sha": "0bb6197...", "git_dirty": false},
  "strategies": {
    "specs": ["tail:wing_frac=0.9", "vrp"],
    "resolved_names": ["tail", "vrp"],
    "config_hash": "sha256…"
  },
  "scanner": {
    "dir": "/abs/path/to/scanner/outputs",
    "manifest_hash": "sha256…"  // or "none"
  },
  "feed": {"name": "csv", "params": {"directory": "…", "asset_class": "…"}},
  "run": {"symbols": ["APP"], "start": "…", "end": "…", "capital": 100000.0,
          "slippage_bps": 5.0, "max_drawdown": 0.2, …}
}
```

## Field notes

- **config_hash**: sha256 over the sorted strategy spec strings
  (`--strategies`, `--pie`, `--book` as given). Two runs with the same hash
  ran the same strategy configuration.
- **manifest_hash**: sha256 over sorted `(relpath, size, mtime)` of the
  scanner CSVs. `"none"` when no scanner dir was given, missing, or holds
  no CSVs. Any regenerated scanner output changes the hash — which is the
  point: stale scanner inputs are a silent run-to-run difference.
- **feed**: name + constructor params. Same feed + params + same date range
  replays the same bars for deterministic feeds (csv, yahoo history).
- **engine.git_sha / git_dirty**: ties the run to an exact checkout. A dirty
  tree is flagged, not blocked — uncommitted experiments are the most common
  source of "but it worked yesterday."
- **run**: symbols, date range (backtest) or interval/duration (live),
  capital, slippage, and arbiter limits.

## What it does NOT guarantee

Bit-reproduction additionally requires the same code (check out `git_sha`),
the same scanner CSVs (verify `manifest_hash`), and — for `live` runs —
the same market. Live quotes cannot be replayed; a live bundle is an audit
trail, not a replay recipe. Treat it as the input to the post-mortem, not
as a time machine.

## API

`flight.py` exposes the pieces independently so tests and tooling can use
them without running anything:

- `config_hash(specs) -> str`
- `scanner_manifest_hash(scanner_dir) -> str` (`"none"` when empty/missing)
- `engine_version(repo_dir=None) -> {"git_sha", "git_dirty"}` (falls back to
  `"unknown"` when git is unavailable — the bundle is still written)
- `write_flight_bundle(db_path, *, command, strategy_specs, strategy_names,
  scanner_dir, feed_name, feed_params, run_params) -> str` (path written)

## Reproducing a run

1. `git checkout <git_sha>` (and confirm `git_dirty` was false, or
   reconstruct the dirty diff).
2. Verify `scanner_manifest_hash(<dir>)` matches `manifest_hash`.
3. Re-run the recorded command with the recorded `strategies.specs`,
   `feed`, and `run` params against the same DB path.
4. Diff the new ledger against the original (`run.py reconcile --db …`
   on both, plus `ae_summary()` comparison).

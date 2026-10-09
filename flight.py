#!/usr/bin/env python3
"""Flight recorder: a JSON bundle written next to the ledger DB at run start,
capturing everything needed to bit-reproduce a backtest/live run.

Written before the engine steps, so even a crashed run leaves its
reproduction recipe behind. Contents:

  - strategy config hash (sha256 over the resolved spec strings)
  - scanner input manifest hash (sorted relpath/size/mtime of the scanner
    CSVs), or "none" when no scanner input was used
  - feed name + params (feed snapshot identity: same feed + params +
    same date range replays the same bars)
  - engine version (git SHA + dirty flag, "unknown" when git is unavailable)
  - start timestamp and the run's key parameters

Bit-reproduction also requires the same code (git SHA), the same scanner
CSVs (manifest hash) and, for live runs, the same market -- live quotes
cannot be replayed, so live bundles are an audit trail, not a replay recipe.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def config_hash(strategy_specs: list[str]) -> str:
    """Deterministic hash of the resolved strategy spec strings."""
    return _sha256_str(json.dumps(sorted(strategy_specs), sort_keys=True))


def scanner_manifest_hash(scanner_dir: str | None) -> str:
    """Hash of sorted (relpath, size, mtime) for CSVs under scanner_dir.

    Returns "none" when the directory is missing, empty, or holds no CSVs.
    """
    if not scanner_dir or not os.path.isdir(scanner_dir):
        return "none"
    entries = []
    for root, _dirs, files in os.walk(scanner_dir):
        for f in sorted(files):
            if not f.endswith(".csv"):
                continue
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((os.path.relpath(p, scanner_dir),
                            st.st_size, int(st.st_mtime)))
    if not entries:
        return "none"
    return _sha256_str(json.dumps(entries, sort_keys=True))


def engine_version(repo_dir: str | None = None) -> dict:
    """git SHA + dirty flag for the paper-trader checkout.

    Falls back to {"git_sha": "unknown", ...} when git is unavailable --
    the bundle is still written, since a partial recipe beats none.
    """
    try:
        cwd = repo_dir or os.path.dirname(os.path.abspath(__file__))
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
            text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            text=True, timeout=10).stdout.strip())
        if not sha:
            raise RuntimeError("empty sha")
        return {"git_sha": sha, "git_dirty": dirty}
    except Exception:
        return {"git_sha": "unknown", "git_dirty": None}


def write_flight_bundle(db_path: str, *, command: str,
                        strategy_specs: list[str],
                        strategy_names: list[str],
                        scanner_dir: str | None,
                        feed_name: str, feed_params: dict,
                        run_params: dict) -> str:
    """Write <db_path>.flight.json; return its path."""
    bundle = {
        "db": os.path.abspath(db_path),
        "command": command,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine_version(),
        "strategies": {
            "specs": sorted(strategy_specs),
            "resolved_names": sorted(strategy_names),
            "config_hash": config_hash(strategy_specs),
        },
        "scanner": {
            "dir": os.path.abspath(scanner_dir) if scanner_dir else None,
            "manifest_hash": scanner_manifest_hash(scanner_dir),
        },
        "feed": {"name": feed_name, "params": feed_params},
        "run": run_params,
    }
    out = db_path + ".flight.json"
    with open(out, "w") as f:
        json.dump(bundle, f, indent=2, sort_keys=True)
    return out

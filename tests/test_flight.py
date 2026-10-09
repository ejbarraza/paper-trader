"""Flight recorder: JSON bundle written next to the ledger DB at run start."""

import json
import os

from flight import (config_hash, engine_version, scanner_manifest_hash,
                    write_flight_bundle)


def test_config_hash_deterministic_and_order_insensitive():
    a = config_hash(["vrp", "tail:wing_frac=0.9"])
    b = config_hash(["tail:wing_frac=0.9", "vrp"])
    assert a == b and len(a) == 64
    assert config_hash(["vrp"]) != a


def test_manifest_none_for_missing_or_empty_dir(tmp_path):
    assert scanner_manifest_hash(None) == "none"
    assert scanner_manifest_hash(str(tmp_path / "nope")) == "none"
    assert scanner_manifest_hash(str(tmp_path)) == "none"  # empty
    (tmp_path / "notes.txt").write_text("not a csv")
    assert scanner_manifest_hash(str(tmp_path)) == "none"


def test_manifest_hash_stable_and_content_sensitive(tmp_path):
    (tmp_path / "APP_csp.csv").write_text("a,b\n1,2\n")
    h1 = scanner_manifest_hash(str(tmp_path))
    h2 = scanner_manifest_hash(str(tmp_path))
    assert h1 == h2 and len(h1) == 64
    (tmp_path / "PLTR_csp.csv").write_text("x\n")
    assert scanner_manifest_hash(str(tmp_path)) != h1


def test_engine_version_has_sha_key():
    v = engine_version()
    assert "git_sha" in v and "git_dirty" in v


def test_write_flight_bundle(tmp_path):
    db = str(tmp_path / "run.db")
    (tmp_path / "scan").mkdir()
    (tmp_path / "scan" / "APP_csp.csv").write_text("strike\n90\n")
    out = write_flight_bundle(
        db, command="backtest",
        strategy_specs=["vrp", "tail"],
        strategy_names=["vrp", "tail"],
        scanner_dir=str(tmp_path / "scan"),
        feed_name="csv", feed_params={"directory": "data"},
        run_params={"symbols": ["APP"], "capital": 100000.0})
    assert out == db + ".flight.json"
    assert os.path.exists(out)
    with open(out) as f:
        bundle = json.load(f)
    assert bundle["command"] == "backtest"
    assert bundle["started_at"]
    assert bundle["strategies"]["config_hash"] == config_hash(["vrp", "tail"])
    assert bundle["scanner"]["manifest_hash"] != "none"
    assert bundle["feed"] == {"name": "csv", "params": {"directory": "data"}}
    assert bundle["run"]["symbols"] == ["APP"]
    assert bundle["engine"]["git_sha"]


def test_write_flight_bundle_no_scanner(tmp_path):
    db = str(tmp_path / "run.db")
    out = write_flight_bundle(
        db, command="live", strategy_specs=[], strategy_names=[],
        scanner_dir=None, feed_name="yahoo", feed_params={},
        run_params={})
    with open(out) as f:
        bundle = json.load(f)
    assert bundle["scanner"]["manifest_hash"] == "none"
    assert bundle["scanner"]["dir"] is None

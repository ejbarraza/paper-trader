"""reconcile.py: ledger audit harness + rejection telemetry."""

from datetime import datetime

from ledger import Ledger
from reconcile import (audit, check_cash_walk, check_equity_decomposition,
                       check_open_lots)

T0 = datetime(2024, 1, 1)
T1 = datetime(2024, 1, 2)
T2 = datetime(2024, 1, 3)


def _clean_ledger(path):
    led = Ledger(str(path))
    led.record_equity(T0, 10000.0, 10000.0)
    led.record_trade(T1, "momentum", "AAA", "buy", 10, 50.0, -500.0)
    led.record_equity(T1, 10000.0, 9500.0)
    led.snapshot_positions(T1, {"spot:AAA": (10, 50.0)})
    led.record_trade(T2, "momentum", "AAA", "sell", 10, 60.0, 600.0)
    led.record_equity(T2, 10100.0, 10100.0)
    led.snapshot_positions(T2, {})
    return led


def test_cash_walk_pass(tmp_path):
    _clean_ledger(tmp_path / "a.db")
    ok, detail = check_cash_walk(str(tmp_path / "a.db"))
    assert ok, detail


def test_cash_walk_fails_on_phantom_cash(tmp_path):
    led = _clean_ledger(tmp_path / "b.db")
    led.record_trade(T2, "x", "AAA", "buy", 1, 200.0, -999.99)
    ok, detail = check_cash_walk(str(tmp_path / "b.db"))
    assert not ok and "999.99" in detail


def test_equity_decomposition_pass(tmp_path):
    _clean_ledger(tmp_path / "a.db")
    ok, detail = check_equity_decomposition(str(tmp_path / "a.db"))
    assert ok, detail


def test_equity_decomposition_fails_on_bad_mark(tmp_path):
    led = _clean_ledger(tmp_path / "c.db")
    led.snapshot_positions(T1, {"spot:AAA": (10, 5000.0)})  # absurd mark
    ok, _ = check_equity_decomposition(str(tmp_path / "c.db"))
    assert not ok


def test_open_lots_pass(tmp_path):
    _clean_ledger(tmp_path / "a.db")
    ok, detail = check_open_lots(str(tmp_path / "a.db"))
    assert ok, detail


def test_open_lots_fails_on_qty_mismatch(tmp_path):
    led = _clean_ledger(tmp_path / "d.db")
    # tape says flat after the T2 sell; snapshot claims 10 shares left
    led.snapshot_positions(T2, {"spot:AAA": (10, 60.0)})
    ok, detail = check_open_lots(str(tmp_path / "d.db"))
    assert not ok and "spot:AAA" in detail


def test_open_lots_options_sign(tmp_path):
    led = Ledger(str(tmp_path / "e.db"))
    led.record_equity(T0, 10000.0, 10000.0)
    led.record_trade(T1, "vrp", "AAPL", "sell_put", 2, 3.0, 590.0)
    led.record_equity(T1, 10000.0, 10590.0)
    # short 2 contracts -> engine stores negative qty
    led.snapshot_positions(T1, {"put:AAPL:150:2024-02-16": (-2, 290.0)})
    ok, detail = check_open_lots(str(tmp_path / "e.db"))
    assert ok, detail


def test_audit_all_pass(tmp_path):
    _clean_ledger(tmp_path / "a.db")
    assert audit(str(tmp_path / "a.db"), verbose=False) is True


def test_audit_reports_failure(tmp_path, capsys):
    led = _clean_ledger(tmp_path / "f.db")
    led.record_trade(T2, "x", "AAA", "buy", 1, 200.0, -999.99)
    assert audit(str(tmp_path / "f.db"), verbose=False) is False


def test_rejection_summary(tmp_path):
    led = Ledger(str(tmp_path / "g.db"))
    led.record_risk_event(T0, "momentum", "AAA", "buy",
                          "strategy_budget_exceeded")
    led.record_risk_event(T0, "momentum", "AAA", "buy",
                          "strategy_budget_exceeded")
    led.record_risk_event(T0, "meanrev", "BBB", "sell", "price_band_violated")
    s = led.rejection_summary()
    assert s["total"] == 3
    assert s["by_reason"] == {"strategy_budget_exceeded": 2,
                             "price_band_violated": 1}
    top = [r for r in s["by_strategy_reason"]
           if r["reason"] == "strategy_budget_exceeded"][0]
    assert top["strategy"] == "momentum" and top["n"] == 2


def test_rejection_summary_empty(tmp_path):
    led = Ledger(str(tmp_path / "h.db"))
    s = led.rejection_summary()
    assert s["total"] == 0 and s["by_reason"] == {}

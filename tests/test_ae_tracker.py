"""A/E tracker: decision-time expected premium vs FIFO realized P&L."""

from datetime import date, datetime

import pytest

from engine import PaperEngine
from ledger import Ledger
from risk import RiskArbiter, RiskConfig
from strategies import Signal


def _ledger():
    return Ledger(":memory:")


def test_record_trade_returns_id():
    led = _ledger()
    tid = led.record_trade(datetime(2024, 5, 1), "vrp", "APP", "sell_put",
                           1, 2.0, 198.7, "")
    assert isinstance(tid, int) and tid > 0


def test_ae_empty_db():
    assert _ledger().ae_summary() == []


def test_ae_short_put_expired_worthless():
    led = _ledger()
    t1 = datetime(2024, 5, 1, 12)
    tid = led.record_trade(t1, "vrp", "APP", "sell_put", 1, 2.00, 198.70, "")
    # signal saw the 2.05 scanner bid; fill crossed slippage to 2.00
    led.record_ae_mark(t1, "vrp", "APP", "short_put", 205.0, tid)
    led.record_trade(datetime(2024, 5, 20, 12), "vrp", "APP",
                     "expiry_worthless", 1, 0.0, 0.0, "")
    rows = led.ae_summary()
    assert len(rows) == 1
    r = rows[0]
    assert (r["strategy"], r["leg"], r["month"]) == ("vrp", "short_put",
                                                    "2024-05")
    assert r["expected"] == pytest.approx(205.0)
    assert r["realized"] == pytest.approx(200.0)
    assert r["ae_pct"] == pytest.approx(200.0 / 205.0 * 100, rel=1e-3)
    assert r["n_opens"] == 1 and r["n_closes"] == 1


def test_ae_short_put_closed_early():
    led = _ledger()
    t1 = datetime(2024, 5, 1, 12)
    tid = led.record_trade(t1, "vrp", "APP", "sell_put", 1, 2.00, 198.70, "")
    led.record_ae_mark(t1, "vrp", "APP", "short_put", 205.0, tid)
    # bought back at 1.00 -> kept half the premium
    led.record_trade(datetime(2024, 5, 10, 12), "vrp", "APP",
                     "buy_put_close", 1, 1.00, -101.30, "")
    (r,) = led.ae_summary()
    assert r["realized"] == pytest.approx(100.0)
    assert r["ae_pct"] == pytest.approx(100.0 / 205.0 * 100, rel=1e-3)


def test_ae_no_closes_no_ratio():
    # Open position: expected is banked, but nothing closed -> no ratio.
    led = _ledger()
    t1 = datetime(2024, 5, 1, 12)
    tid = led.record_trade(t1, "vrp", "APP", "sell_put", 1, 2.00, 198.70, "")
    led.record_ae_mark(t1, "vrp", "APP", "short_put", 205.0, tid)
    (r,) = led.ae_summary()
    assert r["expected"] == pytest.approx(205.0)
    assert r["realized"] == pytest.approx(0.0)
    assert r["ae_pct"] is None
    assert r["n_closes"] == 0


def test_ae_monthly_grouping():
    led = _ledger()
    for month, day in (("2024-05", 1), ("2024-06", 2)):
        t1 = datetime(2024, int(month[-2:]), day, 12)
        tid = led.record_trade(t1, "vrp", "APP", "sell_put", 1, 2.00,
                               198.70, "")
        led.record_ae_mark(t1, "vrp", "APP", "short_put", 205.0, tid)
        led.record_trade(datetime(2024, int(month[-2:]), 20, 12), "vrp",
                         "APP", "expiry_worthless", 1, 0.0, 0.0, "")
    rows = led.ae_summary()
    assert [r["month"] for r in rows] == ["2024-05", "2024-06"]
    assert all(r["ae_pct"] == pytest.approx(200 / 205 * 100, rel=1e-3)
               for r in rows)


def test_ae_long_call_leg_recorded():
    led = _ledger()
    t1 = datetime(2024, 5, 1, 12)
    tid = led.record_trade(t1, "longvol", "PLTR", "buy_call", 1, 3.00,
                           -301.30, "")
    # premium paid: negative expectation
    led.record_ae_mark(t1, "longvol", "PLTR", "long_call", -305.0, tid)
    (r,) = led.ae_summary()
    assert r["leg"] == "long_call"
    assert r["expected"] == pytest.approx(-305.0)
    # negative expected base -> no ratio; raw numbers still reported
    assert r["ae_pct"] is None


def _engine():
    return PaperEngine(feed=None, strategies=[],
                       arbiter=RiskArbiter(RiskConfig()),
                       ledger=_ledger())


def test_engine_records_ae_mark_on_sell_put():
    e = _engine()
    ts = datetime(2024, 5, 1, 12)
    sig = Signal("vrp", "APP", "sell_put", 1, strike=90.0,
                 expiry=date(2024, 6, 21), limit_price=2.05)
    e._apply_fill(sig, 2.00, ts)  # fill net of slippage
    marks = e.ledger._all("SELECT * FROM ae_expected")
    assert len(marks) == 1
    assert marks[0]["leg"] == "short_put"
    assert marks[0]["expected"] == pytest.approx(205.0)
    assert marks[0]["strategy"] == "vrp"


def test_engine_records_ae_mark_on_long_call():
    e = _engine()
    ts = datetime(2024, 5, 1, 12)
    sig = Signal("longvol", "PLTR", "buy_call", 2, strike=200.0,
                 expiry=date(2025, 1, 17), limit_price=3.10,
                 meta={"iv": 0.6})
    e._apply_fill(sig, 3.12, ts)
    marks = e.ledger._all("SELECT * FROM ae_expected")
    assert len(marks) == 1
    assert marks[0]["leg"] == "long_call"
    assert marks[0]["expected"] == pytest.approx(-620.0)


def test_engine_skips_ae_mark_without_limit_price():
    e = _engine()
    ts = datetime(2024, 5, 1, 12)
    sig = Signal("momentum", "SPY", "buy", 10)  # no limit_price
    e._apply_fill(sig, 500.0, ts)
    assert e.ledger._all("SELECT * FROM ae_expected") == []

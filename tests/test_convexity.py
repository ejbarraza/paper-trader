"""Convexity legs: long-option engine support, tail hedge, long vol."""

import csv
import math
from datetime import date, datetime

import pytest

from engine import PaperEngine
from feeds import Bar
from ledger import Ledger
from pricing import bs_call_price, bs_put_price
from risk import RiskArbiter, RiskConfig
from strategies import (Ctx, LongVolStrategy, Signal, TailHedgeStrategy,
                        build_strategy)


def _bar(symbol, close, ts=None):
    return Bar(ts=ts or datetime(2024, 6, 3), symbol=symbol, open=close,
               high=close, low=close, close=close)


def _ctx(closes, positions=None, put_details=None, call_details=None):
    return Ctx(cash=100_000.0, equity=100_000.0, today=date(2024, 6, 3),
               positions=positions or {}, closes=closes,
               put_details=put_details or {},
               call_details=call_details or {})


def _engine(**kw):
    return PaperEngine(feed=None, strategies=[],
                       arbiter=RiskArbiter(RiskConfig()),
                       ledger=Ledger(":memory:"), **kw)


# -- pricing ------------------------------------------------------------

def test_put_call_parity():
    S, K, T, r, sigma = 100.0, 95.0, 0.5, 0.04, 0.30
    c = bs_call_price(S, K, T, r, sigma)
    p = bs_put_price(S, K, T, r, sigma)
    assert c - p == pytest.approx(S - K * math.exp(-r * T))
    assert c > 0 and p > 0


def test_call_intrinsic_at_expiry():
    assert bs_call_price(100, 90, 0.0, 0.04, 0.3) == pytest.approx(10.0)
    assert bs_call_price(80, 90, 0.0, 0.04, 0.3) == pytest.approx(0.0)


# -- engine: long options -----------------------------------------------

def test_buy_call_fill_pays_ask_plus_slippage():
    e = _engine()
    sig = Signal("long_vol", "APP", "buy_call", 2, strike=340.0,
                 expiry=date(2025, 6, 3), limit_price=38.5,
                 meta={"iv": 0.5})
    assert e._est_fill(sig, _bar("APP", 320.0)) == pytest.approx(38.5 * 1.0005)


def test_buy_put_fill_falls_back_to_bs_mark():
    # No limit price: the fallback must be an *option* mark, never the
    # underlying's price.
    e = _engine()
    sig = Signal("tail_hedge", "APP", "buy_put", 2, strike=300.0,
                 expiry=date(2024, 7, 15), meta={"iv": 0.5})
    fill = e._est_fill(sig, _bar("APP", 320.0))
    expect = bs_put_price(320.0, 300.0, 42 / 365.0, e.risk_free, 0.5) * 1.0005
    assert fill == pytest.approx(expect)
    assert fill < 320.0  # an option quote, not the stock price


def test_long_call_marks_and_expires_itm():
    e = _engine()
    exp = date(2024, 6, 10)
    sig = Signal("long_vol", "APP", "buy_call", 2, strike=340.0, expiry=exp,
                 limit_price=38.5, meta={"iv": 0.5})
    bar = _bar("APP", 320.0)
    fill = e._est_fill(sig, bar)
    e.latest["APP"] = bar
    e._apply_fill(sig, fill, datetime(2024, 6, 3, 12))
    key = ("call", "APP", 340.0, exp.isoformat())
    assert e.positions[key] == 2
    premium = 2 * fill * 100.0
    assert e.cash == pytest.approx(100_000.0 - premium - 2 * e.fee_per_contract)
    assert e.strategy_exposure["long_vol"] == pytest.approx(premium)
    marks = e._marks(date(2024, 6, 3))
    assert marks[key] == pytest.approx(
        2 * bs_call_price(320.0, 340.0, 7 / 365.0, e.risk_free, 0.5) * 100.0)
    # expiry deep ITM: collect intrinsic
    e.latest["APP"] = _bar("APP", 360.0)
    e._settle_expiries(exp, datetime(2024, 6, 10, 12))
    assert key not in e.positions
    trades = e.ledger.trades()
    assert any(t["action"] == "expiry_call_long" and
               t["strategy"] == "long_vol" for t in trades)
    assert e.cash == pytest.approx(
        100_000.0 - premium - 2 * e.fee_per_contract + 2 * 20.0 * 100.0)
    assert e.strategy_exposure["long_vol"] == pytest.approx(0.0)


def test_long_put_expires_worthless():
    e = _engine()
    exp = date(2024, 6, 10)
    sig = Signal("tail_hedge", "APP", "buy_put", 2, strike=300.0, expiry=exp,
                 limit_price=0.50, meta={"iv": 0.5})
    bar = _bar("APP", 320.0)
    fill = e._est_fill(sig, bar)
    e.latest["APP"] = bar
    e._apply_fill(sig, fill, datetime(2024, 6, 3, 12))
    key = ("put", "APP", 300.0, exp.isoformat())
    cash_after_buy = e.cash
    e._settle_expiries(exp, datetime(2024, 6, 10, 12))  # S=320 > K: worthless
    assert key not in e.positions
    assert e.cash == pytest.approx(cash_after_buy)  # nothing collected
    assert any(t["action"] == "expiry_put_long" for t in e.ledger.trades())


def test_sell_call_close_releases_premium_exposure():
    e = _engine()
    exp = date(2025, 6, 3)
    sig = Signal("long_vol", "APP", "buy_call", 2, strike=340.0, expiry=exp,
                 limit_price=38.5, meta={"iv": 0.5})
    bar = _bar("APP", 320.0)
    e.latest["APP"] = bar
    e._apply_fill(sig, e._est_fill(sig, bar), datetime(2024, 6, 3, 12))
    key = ("call", "APP", 340.0, exp.isoformat())
    assert e.strategy_exposure["long_vol"] > 0
    close = Signal("long_vol", "APP", "sell_call_close", 2, strike=340.0,
                   expiry=exp, reduce_only=True)
    e._apply_fill(close, e._est_fill(close, bar), datetime(2024, 6, 4, 12))
    assert key not in e.positions
    assert e.strategy_exposure["long_vol"] == pytest.approx(0.0)


def test_long_option_notional_is_premium():
    n = RiskArbiter.signal_notional(
        Signal("long_vol", "APP", "buy_call", 2, strike=340.0,
               expiry=date(2025, 1, 1)), 38.5)
    assert n == pytest.approx(2 * 38.5 * 100.0)  # max loss = premium
    n2 = RiskArbiter.signal_notional(
        Signal("long_vol", "APP", "sell_call_close", 2, strike=340.0,
               expiry=date(2025, 1, 1)), 40.0)
    assert n2 == 0.0  # exits free exposure


# -- tail hedge strategy -------------------------------------------------

def _write_tail_csv(path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strike", "expiration", "bid", "ask", "impliedVolatility",
                    "vrp", "DTE"])
        w.writerow([230, "2024-07-15", 1.60, 1.70, 0.63, 0.21, 42])  # short
        w.writerow([200, "2024-07-15", 0.40, 0.50, 0.60, 0.10, 42])  # wing
        w.writerow([190, "2024-07-15", 0.20, 0.30, 0.58, 0.05, 42])  # cheaper


def test_tail_emits_ratio_spread_long_first(tmp_path):
    _write_tail_csv(str(tmp_path / "APP_csp.csv"))
    strat = TailHedgeStrategy(scanner_dir=str(tmp_path))
    sigs = strat.on_bar(_bar("APP", 322.0), _ctx({"APP": [322.0] * 30}))
    assert len(sigs) == 2
    assert sigs[0].action == "buy_put" and sigs[0].quantity == 2  # wing first
    assert sigs[0].strike == 190.0  # cheapest wing ask
    assert sigs[1].action == "sell_put" and sigs[1].quantity == 1
    assert sigs[1].strike == 230.0
    # net debit 2*0.30 - 1.60 < 0: within the default budget
    assert not sigs[0].reduce_only and not sigs[1].reduce_only


def test_tail_repairs_orphaned_short(tmp_path):
    _write_tail_csv(str(tmp_path / "APP_csp.csv"))
    strat = TailHedgeStrategy(scanner_dir=str(tmp_path))
    bar = _bar("APP", 322.0)
    strat.on_bar(bar, _ctx({"APP": [322.0] * 30}))  # entry bar: legs recorded
    # next bar: short filled, long wing rejected -> must not stay naked short
    skey = ("put", "APP", 230.0, "2024-07-15")
    sigs = strat.on_bar(bar, _ctx({"APP": [322.0] * 30},
                                  positions={skey: -1.0}))
    assert len(sigs) == 1
    assert sigs[0].action == "buy_put_close" and sigs[0].strike == 230.0
    assert sigs[0].reduce_only


def test_tail_skips_when_no_wing(tmp_path):
    with open(tmp_path / "APP_csp.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strike", "expiration", "bid", "ask", "impliedVolatility",
                    "vrp", "DTE"])
        w.writerow([230, "2024-07-15", 1.60, 1.70, 0.63, 0.21, 42])
        w.writerow([225, "2024-07-15", 1.20, 1.30, 0.60, 0.10, 42])  # too close
    strat = TailHedgeStrategy(scanner_dir=str(tmp_path))
    assert strat.on_bar(_bar("APP", 322.0),
                        _ctx({"APP": [322.0] * 30})) == []


def test_tail_registry():
    assert build_strategy("tail", scanner_dir="/tmp").name == "tail_hedge"


# -- long vol strategy ----------------------------------------------------

def _write_leaps_csv(path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "option_type", "expiration", "DTE",
                    "trading_DTE", "strike", "moneyness", "bid", "ask", "mid",
                    "width_frac", "volume", "openInterest",
                    "impliedVolatility"])
        w.writerow(["c1", "call", "2026-01-16", 500, 350, 340, 1.05,
                    30.0, 32.0, 31.0, 0.06, 10, 100, 0.5])
        w.writerow(["c2", "call", "2026-01-16", 500, 350, 400, 1.24,
                    10.0, 12.0, 11.0, 0.18, 5, 50, 0.5])
        w.writerow(["p1", "put", "2026-01-16", 500, 350, 300, 0.93,
                    25.0, 27.0, 26.0, 0.07, 8, 80, 0.5])


def test_longvol_buys_most_atm_first(tmp_path):
    _write_leaps_csv(str(tmp_path / "APP_leaps.csv"))
    strat = LongVolStrategy(scanner_dir=str(tmp_path), top_n=1,
                            size_usd=10_000.0)
    sigs = strat.on_bar(_bar("APP", 322.0), _ctx({"APP": [322.0] * 30}))
    calls = [s for s in sigs if s.action == "buy_call"]
    puts = [s for s in sigs if s.action == "buy_put"]
    assert len(calls) == 1 and calls[0].strike == 340.0  # moneyness 1.05
    assert len(puts) == 1 and puts[0].strike == 300.0
    assert calls[0].limit_price == 32.0  # pays the ask
    assert calls[0].quantity == 3  # floor(10000 / (32*100))
    assert puts[0].quantity == 3  # floor(10000 / (27*100))


def test_longvol_takes_profit_on_double(tmp_path):
    _write_leaps_csv(str(tmp_path / "APP_leaps.csv"))
    strat = LongVolStrategy(scanner_dir=str(tmp_path), top_n=1,
                            size_usd=10_000.0)
    key = ("call", "APP", 340.0, "2026-01-16")
    strat._legs[key] = {"contracts": 3, "iv": 0.5, "entry_ask": 32.0}
    ctx = _ctx({"APP": [420.0] * 30}, positions={key: 3},
               call_details={key: {"iv": 0.5}})
    sigs = strat.on_bar(_bar("APP", 420.0), ctx)
    closes = [s for s in sigs if s.action == "sell_call_close"]
    assert len(closes) == 1 and closes[0].reduce_only


def test_longvol_registry():
    assert build_strategy("longvol", scanner_dir="/tmp").name == "long_vol"


# -- attribution ----------------------------------------------------------

def test_attribution_long_call_round_trip_in_dollars():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    led.record_trade(ts, "long_vol", "APP", "buy_call", 2, 32.0, -6401.30)
    led.record_trade(ts, "long_vol", "APP", "sell_call_close", 2, 64.0,
                     12798.70)
    stats = {r["strategy"]: r for r in led.strategy_stats()}
    assert stats["long_vol"]["realized"] == pytest.approx((64.0 - 32.0) * 2 * 100)
    assert stats["long_vol"]["wins"] == 1


def test_attribution_long_put_expiry():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    led.record_trade(ts, "tail_hedge", "APP", "buy_put", 2, 0.30, -61.30)
    led.record_trade(ts, "tail_hedge", "APP", "expiry_put_long", 2, 5.0,
                     1000.0)  # intrinsic 5.00
    stats = {r["strategy"]: r for r in led.strategy_stats()}
    assert stats["tail_hedge"]["realized"] == pytest.approx(2 * (5.0 - 0.3) * 100)
    assert stats["tail_hedge"]["wins"] == 1

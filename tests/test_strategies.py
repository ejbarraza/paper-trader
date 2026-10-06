"""Strategies: signal logic on synthetic bars (no network)."""

import csv
from datetime import date, datetime

import pytest

from feeds import Bar
from strategies import (Ctx, MeanReversionStrategy, MomentumStrategy,
                        VrpPutSellingStrategy, build_strategy)


def _bar(symbol, close, ts=None, bid=None, ask=None):
    return Bar(ts=ts or datetime(2024, 6, 3), symbol=symbol, open=close,
               high=close, low=close, close=close, bid=bid, ask=ask)


def _ctx(closes, positions=None, cash=100_000.0, equity=100_000.0):
    return Ctx(cash=cash, equity=equity, today=date(2024, 6, 3),
               positions=positions or {}, closes=closes, put_details={})


def test_momentum_golden_cross_buys():
    strat = MomentumStrategy(fast=3, slow=5)
    closes = [10.0] * 5 + [20.0]
    sigs = strat.on_bar(_bar("S", 20.0), _ctx({"S": closes}))
    assert len(sigs) == 1
    assert sigs[0].action == "buy" and sigs[0].quantity == 500  # 10% of 100k / 20


def test_momentum_death_cross_sells():
    strat = MomentumStrategy(fast=3, slow=5)
    closes = [20.0] * 5 + [10.0]
    sigs = strat.on_bar(_bar("S", 10.0),
                        _ctx({"S": closes}, positions={("spot", "S"): 100}))
    assert len(sigs) == 1
    assert sigs[0].action == "sell" and sigs[0].reduce_only


def test_momentum_needs_warmup():
    strat = MomentumStrategy(fast=3, slow=5)
    assert strat.on_bar(_bar("S", 10.0), _ctx({"S": [10.0, 10.0]})) == []


def test_meanrev_short_on_spike():
    strat = MeanReversionStrategy(lookback=5, z_entry=2.0)
    closes = [10.0, 10.0, 10.0, 10.0, 11.0]
    sigs = strat.on_bar(_bar("S", 20.0), _ctx({"S": closes}))
    assert len(sigs) == 1 and sigs[0].action == "sell"  # short the spike


def test_meanrev_long_on_dip():
    strat = MeanReversionStrategy(lookback=5, z_entry=2.0)
    closes = [10.0, 10.0, 10.0, 10.0, 9.0]
    sigs = strat.on_bar(_bar("S", 1.0), _ctx({"S": closes}))
    assert len(sigs) == 1 and sigs[0].action == "buy"


def test_meanrev_exits_on_reversion():
    strat = MeanReversionStrategy(lookback=5, z_entry=2.0, z_exit=0.5)
    closes = [10.0] * 4 + [10.1]
    sigs = strat.on_bar(_bar("S", 10.05),
                        _ctx({"S": closes}, positions={("spot", "S"): 50}))
    assert len(sigs) == 1 and sigs[0].reduce_only


def _write_scanner_csv(path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strike", "expiration", "bid", "impliedVolatility",
                    "vrp", "DTE"])
        w.writerow([90, "2024-07-15", 1.20, 0.45, 0.12, 40])
        w.writerow([85, "2024-07-15", 0.80, 0.40, 0.05, 40])


def test_vrp_enters_from_scanner(tmp_path):
    _write_scanner_csv(str(tmp_path / "APP_csp.csv"))
    strat = VrpPutSellingStrategy(scanner_dir=str(tmp_path))
    sigs = strat.on_bar(_bar("APP", 100.0), _ctx({"APP": [100.0] * 30}))
    assert sigs and all(s.action == "sell_put" for s in sigs)
    assert sigs[0].strike == 90.0  # top VRP first


def test_vrp_ignores_unknown_symbol(tmp_path):
    _write_scanner_csv(str(tmp_path / "APP_csp.csv"))
    strat = VrpPutSellingStrategy(scanner_dir=str(tmp_path))
    assert strat.on_bar(_bar("ZZZ", 100.0), _ctx({"ZZZ": [100.0] * 30})) == []


def test_build_strategy_registry():
    assert build_strategy("momentum").name == "momentum"
    assert build_strategy("meanrev").name == "meanrev"
    with pytest.raises(ValueError):
        build_strategy("nope")

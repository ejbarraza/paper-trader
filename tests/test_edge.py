"""Edge panel (expectancy, profit factor, realized series) and pie portfolios."""

import json
from datetime import date, datetime

import pytest

from feeds import Bar
from ledger import Ledger
from run import parse_pie
from strategies import Ctx, PieStrategy


def _bar(symbol, close):
    return Bar(ts=datetime(2024, 6, 3), symbol=symbol, open=close,
               high=close, low=close, close=close)


def _ctx():
    return Ctx(cash=100_000.0, equity=100_000.0, today=date(2024, 6, 3),
               positions={}, closes={}, put_details={})


def _ledger_two_strategies():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    # momentum: +100 then -100 -> expectancy 0, profit factor 1
    led.record_trade(ts, "momentum", "AAA", "buy", 10, 50.0, -500.0)
    led.record_trade(ts, "momentum", "AAA", "sell", 10, 60.0, 600.0)
    led.record_trade(ts, "momentum", "BBB", "buy", 10, 30.0, -300.0)
    led.record_trade(ts, "momentum", "BBB", "sell", 10, 20.0, 200.0)
    # meanrev: two wins, no losses -> profit factor None
    led.record_trade(ts, "meanrev", "CCC", "buy", 10, 10.0, -100.0)
    led.record_trade(ts, "meanrev", "CCC", "sell", 10, 15.0, 150.0)
    return led


def test_expectancy_and_profit_factor():
    led = _ledger_two_strategies()
    stats = {r["strategy"]: r for r in led.strategy_stats()}
    m = stats["momentum"]
    assert m["expectancy"] == pytest.approx(0.0)
    assert m["profit_factor"] == pytest.approx(1.0)
    assert m["gross_win"] == pytest.approx(100.0)
    assert m["gross_loss"] == pytest.approx(-100.0)
    mr = stats["meanrev"]
    assert mr["expectancy"] == pytest.approx(50.0)
    assert mr["profit_factor"] is None  # no losses: undefined, not infinity


def test_realized_series_is_a_step_function():
    led = _ledger_two_strategies()
    series = led.realized_series()
    mom = [p["realized"] for p in series if p["strategy"] == "momentum"]
    # buy (flat), sell +100, buy (flat), sell back to 0
    assert mom == [0.0, 100.0, 100.0, 0.0]
    mr = [p["realized"] for p in series if p["strategy"] == "meanrev"]
    assert mr == [0.0, 50.0]
    assert all("ts" in p and "strategy" in p for p in series)


def test_pie_buys_allocation_once_and_holds():
    strat = PieStrategy({"AAPL": 0.5, "MSFT": 0.5}, capital=10_000.0)
    sigs = strat.on_bar(_bar("AAPL", 200.0), _ctx())
    assert len(sigs) == 1 and sigs[0].action == "buy"
    assert sigs[0].quantity == 25  # floor(10000*0.5/200)
    # second bar: no rebuy, no rebalance
    assert strat.on_bar(_bar("AAPL", 210.0), _ctx()) == []
    sigs2 = strat.on_bar(_bar("MSFT", 400.0), _ctx())
    assert len(sigs2) == 1 and sigs2[0].quantity == 12  # floor(5000/400)


def test_pie_ignores_unknown_symbols_and_dust():
    strat = PieStrategy({"AAPL": 1.0}, capital=10_000.0)
    assert strat.on_bar(_bar("ZZZ", 10.0), _ctx()) == []
    tiny = PieStrategy({"BRK.A": 1.0}, capital=10_000.0)
    assert tiny.on_bar(_bar("BRK.A", 500_000.0), _ctx()) == []  # can't afford 1


def test_pie_label_is_instance_scoped():
    a = PieStrategy({"AAPL": 1.0}, label="pie-a")
    b = PieStrategy({"AAPL": 1.0}, label="pie-b")
    assert a.name == "pie-a" and b.name == "pie-b"
    assert PieStrategy.name == "pie"  # registry name untouched


def test_parse_pie_inline_and_file(tmp_path):
    p = parse_pie("AAPL:30,MSFT:30,VTI:40", 10_000.0)
    assert p.allocations == {"AAPL": 0.3, "MSFT": 0.3, "VTI": 0.4}
    assert p.capital == 10_000.0
    fp = tmp_path / "mypie.json"
    fp.write_text(json.dumps({"label": "mine", "capital": 5000,
                              "allocations": {"AAPL": 1, "MSFT": 3}}))
    p2 = parse_pie(str(fp), 10_000.0)
    assert p2.name == "mine" and p2.capital == 5000
    assert p2.allocations == {"AAPL": 0.25, "MSFT": 0.75}  # normalized

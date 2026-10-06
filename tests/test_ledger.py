"""Ledger: write/read round-trips."""

import pytest
from datetime import datetime

from ledger import Ledger


def test_trade_roundtrip():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1, 12, 0, 0)
    led.record_trade(ts, "momentum", "AAA", "buy", 10, 50.0, -500.0, "t")
    trades = led.trades()
    assert len(trades) == 1
    assert trades[0]["strategy"] == "momentum"
    assert trades[0]["cash_delta"] == -500.0


def test_equity_curve():
    led = Ledger(":memory:")
    led.record_equity(datetime(2024, 5, 1), 100_000.0, 100_000.0)
    led.record_equity(datetime(2024, 5, 2), 101_000.0, 99_000.0)
    curve = led.equity_curve()
    assert [c["equity"] for c in curve] == [100_000.0, 101_000.0]


def test_risk_events():
    led = Ledger(":memory:")
    led.record_risk_event(datetime(2024, 5, 1), "meanrev", "AAA", "buy",
                          "strategy_budget_exceeded")
    evts = led.risk_events()
    assert evts[0]["reason"] == "strategy_budget_exceeded"


def test_position_snapshot():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    led.snapshot_positions(ts, {"spot:AAA": (10, 50.0)})
    pos = led.latest_positions()
    assert pos[0]["pkey"] == "spot:AAA" and pos[0]["qty"] == 10


def test_strategy_pnl():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    led.record_trade(ts, "momentum", "AAA", "buy", 10, 50.0, -500.0)
    led.record_trade(ts, "momentum", "AAA", "sell", 10, 60.0, 600.0)
    led.record_trade(ts, "meanrev", "BBB", "buy", 5, 20.0, -100.0)
    pnl = {r["strategy"]: r for r in led.strategy_pnl()}
    assert pnl["momentum"]["net"] == 100.0
    assert pnl["momentum"]["n_trades"] == 2
    assert pnl["meanrev"]["net"] == -100.0


def test_strategy_stats_fifo_win_rate():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    led.record_trade(ts, "momentum", "AAA", "buy", 10, 50.0, -500.0)
    led.record_trade(ts, "momentum", "AAA", "sell", 10, 60.0, 600.0)   # +100 win
    led.record_trade(ts, "momentum", "BBB", "buy", 10, 30.0, -300.0)
    led.record_trade(ts, "momentum", "BBB", "sell", 10, 20.0, 200.0)   # -100 loss
    stats = {r["strategy"]: r for r in led.strategy_stats()}
    assert stats["momentum"]["wins"] == 1
    assert stats["momentum"]["losses"] == 1
    assert stats["momentum"]["n_trades"] == 4
    assert stats["momentum"]["realized"] == pytest.approx(0.0)
    assert stats["momentum"]["win_rate"] == pytest.approx(0.5)


def test_strategy_stats_put_round_trip():
    led = Ledger(":memory:")
    ts = datetime(2024, 5, 1)
    led.record_trade(ts, "vrp_puts", "APP", "sell_put", 2, 1.20, 238.70)
    led.record_trade(ts, "vrp_puts", "APP", "buy_put_close", 2, 0.60, -121.30)
    stats = {r["strategy"]: r for r in led.strategy_stats()}
    # kept 0.60/contract x 2
    assert stats["vrp_puts"]["realized"] == pytest.approx(1.20)
    assert stats["vrp_puts"]["wins"] == 1


def test_portfolio_summary_drawdown():
    led = Ledger(":memory:")
    led.record_equity(datetime(2024, 5, 1), 100_000.0, 100_000.0)
    led.record_equity(datetime(2024, 5, 2), 110_000.0, 110_000.0)
    led.record_equity(datetime(2024, 5, 3), 99_000.0, 99_000.0)
    s = led.portfolio_summary()
    assert s["return_pct"] == pytest.approx(-1.0)
    assert s["max_drawdown_pct"] == pytest.approx(10.0)  # 110k -> 99k
    assert s["n_points"] == 3


def test_portfolio_summary_empty():
    s = Ledger(":memory:").portfolio_summary()
    assert s["n_points"] == 0 and s["max_drawdown_pct"] == 0.0

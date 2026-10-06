"""Risk arbiter: budgets, kill-switches, and exit exemptions."""

import pytest

from risk import PortfolioState, RiskArbiter, RiskConfig
from strategies import Signal


def _state(**kw):
    base = dict(cash=100_000.0, equity=100_000.0, peak_equity=100_000.0,
                day_start_equity=100_000.0, strategy_exposure={},
                symbol_exposure={})
    base.update(kw)
    return PortfolioState(**base)


def _buy(qty=10, price_note=""):
    return Signal("momentum", "AAA", "buy", qty, note=price_note)


def test_approves_small_trade():
    arb = RiskArbiter()
    ok, reason = arb.check(_buy(10), 50.0, _state())
    assert ok and reason == ""


def test_strategy_budget_exceeded():
    arb = RiskArbiter(RiskConfig(default_strategy_capital=1_000.0))
    ok, reason = arb.check(_buy(100), 50.0, _state())  # $5k notional
    assert not ok and reason == "strategy_budget_exceeded"


def test_drawdown_killswitch():
    arb = RiskArbiter()
    ok, reason = arb.check(_buy(1), 50.0, _state(equity=80_000.0))
    assert not ok and reason == "drawdown_killswitch"


def test_daily_loss_killswitch():
    arb = RiskArbiter()
    ok, reason = arb.check(_buy(1), 50.0, _state(equity=96_000.0))
    assert not ok and reason == "daily_loss_killswitch"


def test_reduce_only_bypasses_killswitches():
    arb = RiskArbiter()
    sig = Signal("momentum", "AAA", "sell", 10, reduce_only=True)
    ok, reason = arb.check(sig, 50.0, _state(equity=80_000.0))
    assert ok and reason == ""


def test_no_quote_rejected():
    arb = RiskArbiter()
    ok, reason = arb.check(_buy(1), 0.0, _state())
    assert not ok and reason == "no_quote"


def test_price_band_violated():
    arb = RiskArbiter()
    sig = Signal("momentum", "AAA", "buy", 10, limit_price=100.0)
    ok, reason = arb.check(sig, 110.0, _state())  # 10% drift > 2% band
    assert not ok and reason == "price_band_violated"


def test_position_limit_exceeded():
    arb = RiskArbiter(RiskConfig(max_position_notional=1_000.0))
    ok, reason = arb.check(_buy(100), 50.0, _state())
    assert not ok and reason == "position_limit_exceeded"

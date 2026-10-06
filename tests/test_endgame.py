"""Prediction settlement: payout parsing (pure) and engine cash-out."""

from datetime import date, datetime

import pytest

from engine import PaperEngine
from feeds import MarketDataFeed, Quote, _parse_payout, gamma_yes_payout
from ledger import Ledger
from risk import RiskArbiter, RiskConfig
from strategies import Ctx, EndgameSweepStrategy, Signal, Strategy
from feeds import Bar


def test_parse_payout_yes_win():
    m = {"endDate": "2024-01-02T00:00:00Z", "outcomePrices": '["1", "0"]'}
    assert _parse_payout(m, date(2024, 6, 1)) == 1.0


def test_parse_payout_no_win():
    m = {"endDate": "2024-01-02T00:00:00Z", "outcomePrices": '["0", "1"]'}
    assert _parse_payout(m, date(2024, 6, 1)) == 0.0


def test_parse_payout_not_expired():
    m = {"endDate": "2025-01-02T00:00:00Z", "outcomePrices": '["1", "0"]'}
    assert _parse_payout(m, date(2024, 6, 1)) is None


def test_parse_payout_ambiguous():
    m = {"endDate": "2024-01-02T00:00:00Z", "outcomePrices": '["0.6", "0.4"]'}
    assert _parse_payout(m, date(2024, 6, 1)) is None


def test_parse_payout_bad_data():
    assert _parse_payout({}, date(2024, 6, 1)) is None
    assert _parse_payout({"endDate": "junk", "outcomePrices": "junk"},
                         date(2024, 6, 1)) is None


class SettleFeed(MarketDataFeed):
    name = "settle_fake"
    asset_class = "predictions"

    def __init__(self, payouts):
        self.payouts = payouts  # symbol -> payout

    def history(self, symbol, start, end):
        return []

    def latest_quote(self, symbol):
        return Quote(ts=datetime.now(), symbol=symbol, bid=0.5, ask=0.52)

    def settlements(self, symbols, today):
        return [(s, self.payouts[s]) for s in symbols if s in self.payouts]


class StubStrat(Strategy):
    name = "stub"
    asset_classes = frozenset({"predictions"})

    def on_bar(self, bar: Bar, ctx: Ctx):
        return []


def _settled_engine(payouts, qty=100.0):
    eng = PaperEngine(SettleFeed(payouts), [StubStrat()],
                      RiskArbiter(RiskConfig()), Ledger(":memory:"),
                      capital=100_000.0)
    key = ("spot", "MKT")
    eng.positions[key] = qty
    eng._key_strategy[key] = "stub"
    eng._key_exposure[key] = abs(qty) * 0.5
    eng.strategy_exposure["stub"] = abs(qty) * 0.5
    return eng


def test_settle_long_win():
    eng = _settled_engine({"MKT": 1.0}, qty=100.0)
    eng._settle_predictions(date(2024, 6, 1), datetime(2024, 6, 1))
    assert ("spot", "MKT") not in eng.positions
    assert eng.cash == pytest.approx(100_000.0 + 100.0)  # 100 shares @ $1
    assert eng.strategy_exposure["stub"] == 0.0
    trades = eng.ledger.trades()
    assert trades[0]["action"] == "resolve" and "win" in trades[0]["note"]


def test_settle_long_loss():
    eng = _settled_engine({"MKT": 0.0}, qty=100.0)
    eng._settle_predictions(date(2024, 6, 1), datetime(2024, 6, 1))
    assert eng.cash == pytest.approx(100_000.0)  # paid nothing, shares gone
    assert "loss" in eng.ledger.trades()[0]["note"]


def test_settle_short_win():
    eng = _settled_engine({"MKT": 0.0}, qty=-100.0)
    eng._settle_predictions(date(2024, 6, 1), datetime(2024, 6, 1))
    assert eng.cash == pytest.approx(100_000.0)
    assert "win" in eng.ledger.trades()[0]["note"]


def test_settle_skips_undecided():
    eng = _settled_engine({}, qty=100.0)  # feed reports nothing
    eng._settle_predictions(date(2024, 6, 1), datetime(2024, 6, 1))
    assert ("spot", "MKT") in eng.positions  # still held, marked at last


def test_settle_ignores_non_prediction_feed():
    from feeds import CsvFeed
    eng = PaperEngine(CsvFeed("/tmp"), [StubStrat()],
                      RiskArbiter(RiskConfig()), Ledger(":memory:"))
    eng.positions[("spot", "MKT")] = 100.0
    eng._settle_predictions(date(2024, 6, 1), datetime(2024, 6, 1))
    assert ("spot", "MKT") in eng.positions


def _endgame(end_in_minutes, ask):
    ends = {"MKT": datetime(2024, 6, 1, 12, 0, 0) +
            __import__("datetime").timedelta(minutes=end_in_minutes)}
    return EndgameSweepStrategy(symbols=["MKT"], end_dates=ends,
                                endgame_minutes=120, max_price=0.05,
                                size_usd=100.0)


def _ctx():
    return Ctx(cash=100_000.0, equity=100_000.0, today=date(2024, 6, 1),
               positions={}, closes={}, put_details={})


def test_endgame_buys_in_window():
    strat = _endgame(end_in_minutes=60, ask=0.03)
    bar = Bar(ts=datetime(2024, 6, 1, 11, 0, 0), symbol="MKT", open=.03,
              high=.03, low=.03, close=.03, bid=0.02, ask=0.03)
    sigs = strat.on_bar(bar, _ctx())
    assert len(sigs) == 1 and sigs[0].action == "buy"
    assert sigs[0].quantity == 3333  # floor(100 / 0.03)


def test_endgame_skips_outside_window():
    strat = _endgame(end_in_minutes=180, ask=0.03)  # 3h > 120m window
    bar = Bar(ts=datetime(2024, 6, 1, 9, 0, 0), symbol="MKT", open=.03,
              high=.03, low=.03, close=.03, bid=0.02, ask=0.03)
    assert strat.on_bar(bar, _ctx()) == []


def test_endgame_skips_expensive():
    strat = _endgame(end_in_minutes=60, ask=0.20)
    bar = Bar(ts=datetime(2024, 6, 1, 11, 0, 0), symbol="MKT", open=.2,
              high=.2, low=.2, close=.2, bid=0.19, ask=0.20)
    assert strat.on_bar(bar, _ctx()) == []


def test_endgame_skips_when_positioned():
    strat = _endgame(end_in_minutes=60, ask=0.03)
    bar = Bar(ts=datetime(2024, 6, 1, 11, 0, 0), symbol="MKT", open=.03,
              high=.03, low=.03, close=.03, bid=0.02, ask=0.03)
    ctx = _ctx()
    ctx.positions[("spot", "MKT")] = 100.0
    assert strat.on_bar(bar, ctx) == []


def test_endgame_unknown_symbol():
    strat = EndgameSweepStrategy(symbols=[], end_dates={})
    bar = Bar(ts=datetime(2024, 6, 1, 11, 0, 0), symbol="MKT", open=.03,
              high=.03, low=.03, close=.03, bid=0.02, ask=0.03)
    assert strat.on_bar(bar, _ctx()) == []

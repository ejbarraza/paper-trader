"""Engine: spread-aware fills, backtest regression, and the live loop."""

import csv
from datetime import date, datetime

import pytest

from engine import PaperEngine
from feeds import Bar, CsvFeed, MarketDataFeed, Quote
from ledger import Ledger
from risk import RiskArbiter, RiskConfig
from strategies import Ctx, MeanReversionStrategy, Signal, Strategy


def _write_csv(path, symbol, closes):
    from datetime import timedelta
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "symbol", "open", "high", "low", "close", "volume"])
        for i, c in enumerate(closes):
            ts = (datetime(2024, 1, 1) + timedelta(days=i)).isoformat()
            w.writerow([ts, symbol, c, c, c, c, 1000])


def _engine(strategies, **kw):
    return PaperEngine(feed=None, strategies=strategies,
                       arbiter=RiskArbiter(RiskConfig()), ledger=Ledger(":memory:"),
                       **kw)


def test_fill_uses_live_spread():
    e = _engine([])
    live = Bar(ts=datetime.now(), symbol="X", open=.5, high=.5, low=.5,
               close=.5, bid=0.49, ask=0.51)
    assert e._est_fill(Signal("s", "X", "buy", 10), live) == pytest.approx(0.51 * 1.0005)
    assert e._est_fill(Signal("s", "X", "sell", 10), live) == pytest.approx(0.49 * 0.9995)


def test_fill_falls_back_to_close():
    e = _engine([])
    hist = Bar(ts=datetime.now(), symbol="X", open=.5, high=.5, low=.5, close=.5)
    assert e._est_fill(Signal("s", "X", "buy", 10), hist) == pytest.approx(0.5 * 1.0005)


def test_backtest_regression(tmp_path):
    # Flat with two shocks: short the spike, buy the dip, revert both.
    closes = [100.0] * 25 + [110.0] + [100.0] * 25 + [90.0] + [100.0] * 9
    _write_csv(str(tmp_path / "SYN.csv"), "SYN", closes)
    feed = CsvFeed(str(tmp_path))
    eng = PaperEngine(feed, [MeanReversionStrategy()], RiskArbiter(RiskConfig()),
                      Ledger(":memory:"), capital=100_000.0)
    summary = eng.run(["SYN"], date(2024, 1, 1), date(2024, 12, 31))
    assert summary["fills"] > 0
    assert summary["signals"] == summary["fills"] + summary["rejected"]
    assert summary["final_equity"] > 0
    assert len(eng.ledger.trades()) == summary["fills"]
    assert len(eng.ledger.equity_curve()) > 0


def test_backtest_skips_incompatible_strategy(tmp_path, capsys):
    _write_csv(str(tmp_path / "SYN.csv"), "SYN", [100.0] * 10)
    from strategies import VrpPutSellingStrategy
    eng = PaperEngine(CsvFeed(str(tmp_path)),
                      [VrpPutSellingStrategy(scanner_dir=str(tmp_path))],
                      RiskArbiter(RiskConfig()), Ledger(":memory:"))
    out = eng.run(["SYN"], date(2024, 1, 1), date(2024, 12, 31))
    assert "error" in out
    assert "skipping" in capsys.readouterr().out


class FakeLiveFeed(MarketDataFeed):
    """Canned live quotes; history() is empty like the WS feed."""
    name = "fake_live"
    asset_class = "predictions"

    def __init__(self, mids):
        self.mids = mids

    def history(self, symbol, start, end):
        return []

    def latest_quote(self, symbol):
        mid = self.mids[0]
        return Quote(ts=datetime.now(), symbol=symbol,
                     bid=mid - 0.005, ask=mid + 0.005)


class AlwaysBuy(Strategy):
    name = "alwaysbuy"
    asset_classes = frozenset({"predictions"})

    def on_bar(self, bar: Bar, ctx: Ctx):
        if ctx.positions.get(("spot", bar.symbol), 0) == 0:
            return [Signal("alwaysbuy", bar.symbol, "buy", 10, note="t")]
        return []


def test_run_live_steps_and_fills():
    feed = FakeLiveFeed([0.50, 0.51, 0.52])
    eng = PaperEngine(feed, [AlwaysBuy()], RiskArbiter(RiskConfig()),
                      Ledger(":memory:"), capital=100_000.0)
    summary = eng.run_live(["MKT"], interval_s=0.05, duration_s=0.22)
    assert summary["fills"] == 1  # buys once, then flat
    assert summary["signals"] >= 1
    assert len(eng.ledger.equity_curve()) >= 2
    trades = eng.ledger.trades()
    assert trades[0]["action"] == "buy" and trades[0]["price"] == pytest.approx(0.505 * 1.0005)


def test_run_live_rejects_incompatible():
    feed = FakeLiveFeed([0.50])
    from strategies import VrpPutSellingStrategy
    eng = PaperEngine(feed, [VrpPutSellingStrategy(scanner_dir="/tmp")],
                      RiskArbiter(RiskConfig()), Ledger(":memory:"))
    assert "error" in eng.run_live(["MKT"], interval_s=0.01, duration_s=0.05)

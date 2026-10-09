"""Ledger accounting oracles: independent verification of the money math.

The ledger is the system of record, so these tests never derive expectations
from ledger.py / engine.py / pricing.py.  Instead:

  * Hand-computed fixtures: every dollar figure below is literal arithmetic
    written out in comments (e.g. 2 contracts x $1.20/share x 100 = $240.00).
    If the engine or ledger disagrees with hand arithmetic, the engine or
    ledger is wrong.
  * Independent FIFO oracle: ``OracleFIFO`` re-implements the documented
    action semantics from scratch.  It is validated against the hand
    fixtures, then cross-checked against ``Ledger.strategy_stats()`` on
    randomized tapes -- two implementations must agree.
  * Engine property tests: random signals through ``PaperEngine._apply_fill``
    plus expiries; after every step, engine cash must equal capital plus the
    trade-tape cash deltas, and engine positions must match an independently
    maintained book.
  * Mutation-style tests: absolute-dollar assertions that go red under
    symmetric mis-booking (halve premium paid AND received) and under the
    historical 100x points-vs-dollars bug.
  * Real-data replay: the reconcile audit harness re-run on shipped
    backtest DBs.

Known modeling choice (not a bug): ``strategy_stats()["realized"]`` is
gross of contract fees -- fees move cash (and ``net``) but the FIFO
realized P&L is computed from per-share prices only.  The fixtures below
assert both, so the distinction is pinned.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from engine import PaperEngine
from feeds import Bar
from ledger import Ledger
from risk import RiskArbiter, RiskConfig
from strategies import Signal

REPO = Path(__file__).resolve().parent.parent
FEE = 1.30          # engine fee_per_contract used throughout
CAPITAL = 100_000.0


def _engine(**kw):
    args = dict(feed=None, strategies=[],
                arbiter=RiskArbiter(RiskConfig()),
                ledger=Ledger(":memory:"), capital=CAPITAL,
                slippage_bps=0.0, fee_per_contract=FEE, fee_per_share=0.0)
    args.update(kw)
    return PaperEngine(**args)


def _ts(day=1):
    return datetime(2024, 1, day, 12, 0, 0)


def _stats(ledger):
    return {r["strategy"]: r for r in ledger.strategy_stats()}


# ---------------------------------------------------------------------------
# Part 1 -- hand-computed engine fixtures (absolute dollars, by hand)
# ---------------------------------------------------------------------------

def test_spot_round_trip_absolute_cash():
    e = _engine()
    # buy 10 AAA @ $50.00: cost = 10*50.00 + 0 fees = $500.00
    e._apply_fill(Signal("mom", "AAA", "buy", 10), 50.00, _ts(1))
    assert e.cash == pytest.approx(CAPITAL - 500.00)          # 99,500.00
    # sell 10 AAA @ $60.00: proceeds = 10*60.00 = $600.00
    e._apply_fill(Signal("mom", "AAA", "sell", 10), 60.00, _ts(2))
    assert e.cash == pytest.approx(CAPITAL + 100.00)          # 100,100.00
    assert e.positions.get(("spot", "AAA"), 0.0) == pytest.approx(0.0)
    s = _stats(e.ledger)["mom"]
    # realized = 10 * (60 - 50) = +$100.00 ; net cash flow = +$100.00
    assert s["realized"] == pytest.approx(100.00)
    assert s["net"] == pytest.approx(100.00)
    assert s["wins"] == 1 and s["losses"] == 0


def test_short_put_round_trip_absolute_dollars():
    # THE 100x bug class: premium is quoted per-share in points; dollars
    # need contracts x points x 100.  Every figure below is hand arithmetic.
    e = _engine()
    # sell_put 2 contracts @ 1.20: premium = 2*1.20*100 = $240.00
    # fees = 2*1.30 = $2.60  -> cash_delta = +$237.40
    e._apply_fill(Signal("vrp", "AAA", "sell_put", 2, strike=100,
                         expiry=date(2024, 2, 16), limit_price=1.20,
                         meta={"iv": 0.5}), 1.20, _ts(1))
    assert e.cash == pytest.approx(CAPITAL + 237.40)          # 100,237.40
    assert e.positions[("put", "AAA", 100, "2024-02-16")] == -2
    # buy_put_close 2 @ 0.60: cost = 2*0.60*100 + 2.60 = $122.60
    e._apply_fill(Signal("vrp", "AAA", "buy_put_close", 2, strike=100,
                         expiry=date(2024, 2, 16)), 0.60, _ts(2))
    assert e.cash == pytest.approx(CAPITAL + 114.80)          # 100,114.80
    assert ("put", "AAA", 100, "2024-02-16") not in e.positions
    s = _stats(e.ledger)["vrp"]
    # realized (gross of fees, by design) = 2*(1.20-0.60)*100 = +$120.00
    assert s["realized"] == pytest.approx(120.00)
    # net cash flow keeps the fees: 237.40 - 122.60 = +$114.80
    assert s["net"] == pytest.approx(114.80)
    assert s["wins"] == 1


def test_csp_assignment_absolute_cash_and_attribution():
    # THE attribution bug class: expiry trades must book under the strategy
    # that opened the position, never under "engine".
    e = _engine()
    # sell_put 1 @ 2.50, K=100: premium = 1*2.50*100 = $250.00
    # fees $1.30 -> cash_delta = +$248.70
    e._apply_fill(Signal("vrp", "AAA", "sell_put", 1, strike=100,
                         expiry=date(2024, 2, 16), limit_price=2.50,
                         meta={"iv": 0.5}), 2.50, _ts(1))
    assert e.cash == pytest.approx(CAPITAL + 248.70)
    # expiry with S=90 < K=100 -> assigned 100 shares @ $100:
    # cost = 100*100 = $10,000.00
    e.latest["AAA"] = Bar(ts=_ts(10), symbol="AAA", open=90, high=90,
                          low=90, close=90, volume=0)
    e._settle_expiries(date(2024, 2, 16), _ts(10))
    assert e.cash == pytest.approx(CAPITAL + 248.70 - 10_000.00)  # 90,248.70
    assert e.positions[("spot", "AAA")] == pytest.approx(100.0)
    trades = e.ledger.trades(limit=10)
    assign = [t for t in trades if t["action"] == "expiry_assign"]
    assert len(assign) == 1
    assert assign[0]["strategy"] == "vrp"          # NOT "engine"
    assert assign[0]["qty"] == pytest.approx(100.0)
    assert assign[0]["price"] == pytest.approx(100.0)
    assert assign[0]["cash_delta"] == pytest.approx(-10_000.00)
    s = _stats(e.ledger)["vrp"]
    # premium kept: 1*2.50*100 = +$250.00 ; stock lot now open, unrealized
    assert s["realized"] == pytest.approx(250.00)
    lots = e.ledger.implied_open_qty()
    assert lots[("spot", "vrp", "AAA")] == pytest.approx(100.0)


def test_put_expires_worthless_absolute():
    e = _engine()
    # sell_put 1 @ 3.00: premium $300.00, fees $1.30 -> +$298.70
    e._apply_fill(Signal("vrp", "AAA", "sell_put", 1, strike=100,
                         expiry=date(2024, 2, 16), limit_price=3.00,
                         meta={"iv": 0.5}), 3.00, _ts(1))
    assert e.cash == pytest.approx(CAPITAL + 298.70)
    e.latest["AAA"] = Bar(ts=_ts(10), symbol="AAA", open=110, high=110,
                          low=110, close=110, volume=0)
    e._settle_expiries(date(2024, 2, 16), _ts(10))
    # worthless: no cash moves at expiry
    assert e.cash == pytest.approx(CAPITAL + 298.70)
    t = [t for t in e.ledger.trades(limit=10)
         if t["action"] == "expiry_worthless"][0]
    assert t["strategy"] == "vrp" and t["cash_delta"] == pytest.approx(0.0)
    s = _stats(e.ledger)["vrp"]
    # premium kept whole: 1*3.00*100 = +$300.00
    assert s["realized"] == pytest.approx(300.00)
    assert s["wins"] == 1


def test_long_call_expiry_via_intrinsic_absolute():
    e = _engine()
    # buy_call 2 @ 1.50: cost = 2*1.50*100 + 2*1.30 = $302.60
    e._apply_fill(Signal("lv", "AAA", "buy_call", 2, strike=100,
                         expiry=date(2024, 2, 16), limit_price=1.50,
                         meta={"iv": 0.5}), 1.50, _ts(1))
    assert e.cash == pytest.approx(CAPITAL - 302.60)          # 99,697.40
    # expiry S=110, K=100 -> intrinsic $10.00/share:
    # value = 2*10.00*100 = +$2,000.00
    e.latest["AAA"] = Bar(ts=_ts(10), symbol="AAA", open=110, high=110,
                          low=110, close=110, volume=0)
    e._settle_expiries(date(2024, 2, 16), _ts(10))
    assert e.cash == pytest.approx(CAPITAL - 302.60 + 2_000.00)  # 101,697.40
    t = [t for t in e.ledger.trades(limit=10)
         if t["action"] == "expiry_call_long"][0]
    assert t["strategy"] == "lv"
    assert t["qty"] == pytest.approx(2.0)
    assert t["price"] == pytest.approx(10.00)       # intrinsic per share
    assert t["cash_delta"] == pytest.approx(2_000.00)
    s = _stats(e.ledger)["lv"]
    # realized = 2*(10.00-1.50)*100 = +$1,700.00
    assert s["realized"] == pytest.approx(1_700.00)


def test_long_put_closed_at_loss_absolute():
    e = _engine()
    # buy_put 1 @ 2.00: cost = 200.00 + 1.30 = $201.30
    e._apply_fill(Signal("lv", "AAA", "buy_put", 1, strike=100,
                         expiry=date(2024, 2, 16), limit_price=2.00,
                         meta={"iv": 0.5}), 2.00, _ts(1))
    assert e.cash == pytest.approx(CAPITAL - 201.30)
    # sell_put_close 1 @ 0.50: proceeds = 50.00 - 1.30 = $48.70
    e._apply_fill(Signal("lv", "AAA", "sell_put_close", 1, strike=100,
                         expiry=date(2024, 2, 16)), 0.50, _ts(2))
    assert e.cash == pytest.approx(CAPITAL - 152.60)          # 99,847.40
    s = _stats(e.ledger)["lv"]
    # realized = 1*(0.50-2.00)*100 = -$150.00
    assert s["realized"] == pytest.approx(-150.00)
    assert s["losses"] == 1 and s["wins"] == 0


def test_partial_close_fifo_absolute():
    e = _engine()
    # sell_put 4 @ 2.00: premium 800.00, fees 5.20 -> +$794.80
    e._apply_fill(Signal("vrp", "AAA", "sell_put", 4, strike=100,
                         expiry=date(2024, 2, 16), limit_price=2.00,
                         meta={"iv": 0.5}), 2.00, _ts(1))
    # buy_put_close 1 @ 1.00: cost 100.00 + 1.30 = $101.30
    e._apply_fill(Signal("vrp", "AAA", "buy_put_close", 1, strike=100,
                         expiry=date(2024, 2, 16)), 1.00, _ts(2))
    # buy_put_close 3 @ 2.50: cost 750.00 + 3.90 = $753.90
    e._apply_fill(Signal("vrp", "AAA", "buy_put_close", 3, strike=100,
                         expiry=date(2024, 2, 16)), 2.50, _ts(3))
    # cash = 100000 + 794.80 - 101.30 - 753.90 = 99,939.60
    assert e.cash == pytest.approx(99_939.60)
    s = _stats(e.ledger)["vrp"]
    # FIFO: 1*(2.00-1.00)*100 + 3*(2.00-2.50)*100 = 100 - 150 = -$50.00
    assert s["realized"] == pytest.approx(-50.00)
    assert s["losses"] == 1
    assert e.ledger.implied_open_qty() == {}


# ---------------------------------------------------------------------------
# Part 2 -- independent FIFO oracle, validated on the hand fixtures,
# then cross-checked against the ledger on randomized tapes.
# ---------------------------------------------------------------------------

class OracleFIFO:
    """From-scratch re-implementation of the documented ledger action
    semantics.  Option P&L is contracts x per-share points x 100, in
    dollars -- the independent check on the historical 100x bug class."""

    def __init__(self):
        self.lots = {}       # (strategy, symbol, book) -> [[signed_qty, price]]
        self.realized = {}   # strategy -> dollars
        self.wins = {}
        self.losses = {}

    # -- internals -----------------------------------------------------
    def _book(self, s, sym, b):
        return self.lots.setdefault((s, sym, b), [])

    def _add(self, s, pnl):
        self.realized[s] = self.realized.get(s, 0.0) + pnl
        if pnl > 1e-9:
            self.wins[s] = self.wins.get(s, 0) + 1
        elif pnl < -1e-9:
            self.losses[s] = self.losses.get(s, 0) + 1

    def _match_spot(self, lots, qty, px, pnl_sign):
        """Mirrors the ledger's FIFO match formula for spot lots."""
        pnl = 0.0
        i = 0
        while qty > 1e-9 and i < len(lots):
            lq, lp = lots[i]
            m = min(qty, abs(lq))
            pnl += pnl_sign * m * (px - lp) * (1 if lq > 0 else -1)
            qty -= m
            if abs(lq) - m < 1e-9:
                lots.pop(i)
            else:
                lots[i][0] = lq - m * (1 if lq > 0 else -1)
                i += 1
        return pnl, qty

    def _close_opt(self, lots, qty, px, per_share):
        """FIFO close of option lots; per_share(lot_price) -> $/share."""
        pnl = 0.0
        while qty > 1e-9 and lots:
            lq, lp = lots[0]
            m = min(qty, abs(lq))
            pnl += m * per_share(lp) * 100.0
            qty -= m
            if abs(lq) - m < 1e-9:
                lots.pop(0)
            else:
                lots[0][0] = lq - m * (1 if lq > 0 else -1)
        return pnl, qty

    # -- public: one trade ------------------------------------------------
    def apply(self, strategy, symbol, action, qty, price):
        lots_spot = self._book(strategy, symbol, "spot")
        if action == "buy":
            # cover shorts first, then open long
            pnl, rem = self._match_spot(lots_spot, qty, price, +1)
            self._add(strategy, pnl)
            if rem > 1e-9:
                lots_spot.append([rem, price])
        elif action == "sell":
            pnl, rem = self._match_spot(lots_spot, qty, price, +1)
            self._add(strategy, pnl)
            if rem > 1e-9:
                lots_spot.append([-rem, price])
        elif action == "resolve":
            pnl, _ = self._match_spot(lots_spot, abs(qty), price, +1)
            self._add(strategy, pnl)
        elif action == "sell_put":
            self._book(strategy, symbol, "put").append([qty, price])
        elif action == "buy_put_close":
            pnl, _ = self._close_opt(self._book(strategy, symbol, "put"),
                                     qty, price, lambda lp: lp - price)
            self._add(strategy, pnl)
        elif action == "buy_put":
            self._book(strategy, symbol, "long_put").append([qty, price])
        elif action == "sell_put_close":
            pnl, _ = self._close_opt(self._book(strategy, symbol, "long_put"),
                                     qty, price, lambda lp: price - lp)
            self._add(strategy, pnl)
        elif action == "buy_call":
            self._book(strategy, symbol, "long_call").append([qty, price])
        elif action == "sell_call_close":
            pnl, _ = self._close_opt(self._book(strategy, symbol, "long_call"),
                                     qty, price, lambda lp: price - lp)
            self._add(strategy, pnl)
        elif action == "expiry_worthless":
            for lq, lp in self._book(strategy, symbol, "put"):
                self._add(strategy, lq * lp * 100.0)
            self.lots[(strategy, symbol, "put")] = []
        elif action == "expiry_assign":
            for lq, lp in self._book(strategy, symbol, "put"):
                self._add(strategy, lq * lp * 100.0)
            self.lots[(strategy, symbol, "put")] = []
            lots_spot.append([qty, price])  # shares @ strike
        elif action in ("expiry_put_long", "expiry_call_long"):
            book = "long_put" if action == "expiry_put_long" else "long_call"
            for lq, lp in self._book(strategy, symbol, book):
                self._add(strategy, lq * (price - lp) * 100.0)
            self.lots[(strategy, symbol, book)] = []

    def open_lots(self):
        out = {}
        for (s, sym, book), lots in self.lots.items():
            net = sum(lq for lq, _ in lots)
            if abs(net) > 1e-9:
                out[(book, s, sym)] = net
        return out


def _feed_tape(ledger, oracle, tape):
    """Apply one tape to both the ledger (cash_delta=0: FIFO realized does
    not depend on it) and the oracle."""
    for i, (s, sym, a, q, px) in enumerate(tape):
        ts = datetime(2024, 3, 1) + timedelta(minutes=i)
        ledger.record_trade(ts, s, sym, a, q, px, 0.0, "")
        oracle.apply(s, sym, a, q, px)


def test_oracle_matches_hand_fixtures():
    # Validates the oracle itself against hand arithmetic before it is
    # used as the cross-check in the property tests.
    o = OracleFIFO()
    o.apply("vrp", "AAA", "sell_put", 2, 1.20)
    o.apply("vrp", "AAA", "buy_put_close", 2, 0.60)
    assert o.realized["vrp"] == pytest.approx(120.00)   # 2*(1.20-.60)*100
    assert o.wins["vrp"] == 1

    o = OracleFIFO()
    o.apply("vrp", "AAA", "sell_put", 1, 2.50)
    o.apply("vrp", "AAA", "expiry_assign", 100, 100.0)  # 100 sh @ K=100
    assert o.realized["vrp"] == pytest.approx(250.00)   # premium kept
    assert o.open_lots()[("spot", "vrp", "AAA")] == pytest.approx(100.0)

    o = OracleFIFO()
    o.apply("lv", "AAA", "buy_call", 2, 1.50)
    o.apply("lv", "AAA", "expiry_call_long", 2, 10.00)  # intrinsic 10/share
    assert o.realized["lv"] == pytest.approx(1_700.00)  # 2*(10-1.50)*100

    o = OracleFIFO()
    o.apply("mom", "AAA", "buy", 10, 50.0)
    o.apply("mom", "AAA", "sell", 10, 60.0)
    assert o.realized["mom"] == pytest.approx(100.00)


def _random_tape(rng, n_trades):
    """Random but always-valid tape across 2 strategies x 2 symbols."""
    tape = []
    # availability model: open contracts per (strategy, symbol, book)
    open_short_put = {}
    open_long_put = {}
    open_long_call = {}
    strats = ["alpha", "beta"]
    syms = ["AAA", "BBB"]
    for _ in range(n_trades):
        s = rng.choice(strats)
        sym = rng.choice(syms)
        r = rng.random()
        if r < 0.30:
            a = rng.choice(["buy", "sell"])
            tape.append((s, sym, a, rng.randint(1, 20),
                         round(rng.uniform(10, 200), 2)))
        elif r < 0.45:
            tape.append((s, sym, "sell_put", rng.randint(1, 3),
                         round(rng.uniform(0.5, 4.0), 2)))
            k = (s, sym)
            open_short_put[k] = open_short_put.get(k, 0) + tape[-1][3]
        elif r < 0.55 and open_short_put.get((s, sym), 0) > 0:
            k = (s, sym)
            q = rng.randint(1, open_short_put[k])
            tape.append((s, sym, "buy_put_close", q,
                         round(rng.uniform(0.1, 5.0), 2)))
            open_short_put[k] -= q
        elif r < 0.65:
            tape.append((s, sym, "buy_put", rng.randint(1, 3),
                         round(rng.uniform(0.5, 4.0), 2)))
            k = (s, sym)
            open_long_put[k] = open_long_put.get(k, 0) + tape[-1][3]
        elif r < 0.72 and open_long_put.get((s, sym), 0) > 0:
            k = (s, sym)
            q = rng.randint(1, open_long_put[k])
            tape.append((s, sym, "sell_put_close", q,
                         round(rng.uniform(0.1, 5.0), 2)))
            open_long_put[k] -= q
        elif r < 0.80:
            tape.append((s, sym, "buy_call", rng.randint(1, 3),
                         round(rng.uniform(0.5, 4.0), 2)))
            k = (s, sym)
            open_long_call[k] = open_long_call.get(k, 0) + tape[-1][3]
        elif r < 0.86 and open_long_call.get((s, sym), 0) > 0:
            k = (s, sym)
            q = rng.randint(1, open_long_call[k])
            tape.append((s, sym, "sell_call_close", q,
                         round(rng.uniform(0.1, 5.0), 2)))
            open_long_call[k] -= q
        elif r < 0.90 and open_short_put.get((s, sym), 0) > 0:
            k = (s, sym)
            c = open_short_put.pop(k)
            if rng.random() < 0.5:
                tape.append((s, sym, "expiry_worthless", c, 0.0))
            else:
                K = round(rng.uniform(80, 120), 2)
                tape.append((s, sym, "expiry_assign", 100 * c, K))
        elif r < 0.95 and open_long_put.get((s, sym), 0) > 0:
            k = (s, sym)
            c = open_long_put.pop(k)
            tape.append((s, sym, "expiry_put_long", c,
                         round(rng.uniform(0, 15), 2)))
        elif open_long_call.get((s, sym), 0) > 0:
            k = (s, sym)
            c = open_long_call.pop(k)
            tape.append((s, sym, "expiry_call_long", c,
                         round(rng.uniform(0, 15), 2)))
        else:
            tape.append((s, sym, rng.choice(["buy", "sell"]),
                         rng.randint(1, 20), round(rng.uniform(10, 200), 2)))
    return tape


@pytest.mark.parametrize("seed", range(30))
def test_oracle_vs_ledger_random_tapes(seed):
    """Two independent FIFO implementations must agree on realized P&L,
    win/loss counts, and open lots across randomized tapes."""
    rng = random.Random(1000 + seed)
    tape = _random_tape(rng, rng.randint(40, 80))
    led = Ledger(":memory:")
    oracle = OracleFIFO()
    _feed_tape(led, oracle, tape)

    got = {r["strategy"]: r for r in led.strategy_stats()}
    for s in ("alpha", "beta"):
        exp_real = oracle.realized.get(s, 0.0)
        exp_w = oracle.wins.get(s, 0)
        exp_l = oracle.losses.get(s, 0)
        row = got.get(s)
        if row is None:
            assert exp_real == pytest.approx(0.0)
            continue
        assert row["realized"] == pytest.approx(exp_real), \
            f"seed {seed} strategy {s}: ledger {row['realized']} vs oracle {exp_real}"
        assert row["wins"] == exp_w, f"seed {seed} strategy {s} wins"
        assert row["losses"] == exp_l, f"seed {seed} strategy {s} losses"

    exp_lots = oracle.open_lots()
    got_lots = led.implied_open_qty()
    assert set(exp_lots) == set(got_lots), f"seed {seed} lot keys"
    for k in exp_lots:
        assert got_lots[k] == pytest.approx(exp_lots[k]), f"seed {seed} {k}"

# ---------------------------------------------------------------------------
# Part 3 -- engine property tests: random signals through _apply_fill plus
# expiries.  After EVERY step: cash == capital + tape cash deltas, and engine
# positions == an independently maintained book.
# ---------------------------------------------------------------------------

def _prop_engine():
    return PaperEngine(feed=None, strategies=[],
                       arbiter=RiskArbiter(RiskConfig()),
                       ledger=Ledger(":memory:"), capital=CAPITAL,
                       slippage_bps=0.0, fee_per_contract=FEE,
                       fee_per_share=0.0)


def _tape_cash(ledger):
    return sum(t["cash_delta"] for t in ledger.trades(limit=100_000))


def _assert_books(e, book):
    got = {k: v for k, v in e.positions.items() if abs(v) > 1e-9}
    exp = {k: v for k, v in book.items() if abs(v) > 1e-9}
    assert set(got) == set(exp), f"keys: engine {sorted(got)} vs book {sorted(exp)}"
    for k in exp:
        assert got[k] == pytest.approx(exp[k]), f"position {k}"


@pytest.mark.parametrize("seed", range(20))
def test_engine_cash_and_positions_random_flows(seed):
    rng = random.Random(5000 + seed)
    e = _prop_engine()
    book = {}                       # engine position key -> qty (my model)
    price = {"AAA": 100.0, "BBB": 50.0}
    base = date(2024, 1, 1)
    day = 0

    def bar(sym):
        p = price[sym]
        return Bar(ts=datetime(2024, 1, 1) + timedelta(days=day),
                   symbol=sym, open=p, high=p, low=p, close=p, volume=1000)

    def put_key(sym, K, exp):
        return ("put", sym, K, exp.isoformat())

    def call_key(sym, K, exp):
        return ("call", sym, K, exp.isoformat())

    for step in range(120):
        day += rng.randint(0, 2)
        ts = datetime(2024, 1, 1) + timedelta(days=day)
        sym = rng.choice(["AAA", "BBB"])
        price[sym] = round(price[sym] * (1 + rng.uniform(-0.04, 0.04)), 2)
        r = rng.random()
        strat = rng.choice(["s1", "s2"])
        K = round(price[sym] * rng.uniform(0.85, 1.05), 2)
        exp = base + timedelta(days=rng.randint(7, 45))

        if r < 0.25:
            a = rng.choice(["buy", "sell"])
            q = rng.randint(1, 20)
            e._apply_fill(Signal(strat, sym, a, q), price[sym], ts)
            book[("spot", sym)] = book.get(("spot", sym), 0.0) \
                + (q if a == "buy" else -q)
        elif r < 0.40:
            c, prem = rng.randint(1, 3), round(rng.uniform(0.5, 4.0), 2)
            e._apply_fill(Signal(strat, sym, "sell_put", c, strike=K,
                                 expiry=exp, limit_price=prem,
                                 meta={"iv": 0.5}), prem, ts)
            k = put_key(sym, K, exp)
            book[k] = book.get(k, 0.0) - c
        elif r < 0.50:
            shorts = [k for k in book if k[0] == "put" and k[1] == sym
                      and book[k] < -1e-9]
            if not shorts:
                continue
            k = rng.choice(shorts)
            c = rng.randint(1, int(-book[k]))
            px = round(rng.uniform(0.1, 5.0), 2)
            e._apply_fill(Signal(strat, sym, "buy_put_close", c,
                                 strike=k[2],
                                 expiry=date.fromisoformat(k[3])), px, ts)
            book[k] += c
        elif r < 0.62:
            kind = rng.choice(["buy_put", "buy_call"])
            c, prem = rng.randint(1, 3), round(rng.uniform(0.5, 4.0), 2)
            e._apply_fill(Signal(strat, sym, kind, c, strike=K, expiry=exp,
                                 limit_price=prem, meta={"iv": 0.5}), prem, ts)
            k = (kind.split("_")[1], sym, K, exp.isoformat())
            book[k] = book.get(k, 0.0) + c
        elif r < 0.70:
            longs = [k for k in book if k[0] in ("put", "call")
                     and k[1] == sym and book[k] > 1e-9]
            if not longs:
                continue
            k = rng.choice(longs)
            kind = "sell_put_close" if k[0] == "put" else "sell_call_close"
            c = rng.randint(1, int(book[k]))
            px = round(rng.uniform(0.1, 5.0), 2)
            e._apply_fill(Signal(strat, sym, kind, c, strike=k[2],
                                 expiry=date.fromisoformat(k[3])), px, ts)
            book[k] -= c
        else:
            # expire everything at or past the furthest expiry among opens
            opt_keys = [k for k in book if len(k) == 4 and abs(book[k]) > 1e-9]
            if not opt_keys:
                continue
            today = max(date.fromisoformat(k[3]) for k in opt_keys)
            ts = datetime.combine(today, datetime.min.time())
            for s2 in ("AAA", "BBB"):
                e.latest[s2] = bar(s2)
            e._settle_expiries(today, ts)
            for k in opt_keys:
                if date.fromisoformat(k[3]) > today:
                    continue
                qty = book.pop(k)
                if qty < 0 and k[0] == "put":
                    # short put: assignment below strike, worthless above
                    S = price[k[1]]
                    if S < k[2]:
                        sk = ("spot", k[1])
                        book[sk] = book.get(sk, 0.0) + 100 * abs(qty)
                # long options and worthless shorts just vanish

        # invariants after EVERY step
        assert e.cash == pytest.approx(CAPITAL + _tape_cash(e.ledger)), \
            f"seed {seed} step {step}: cash walk broke"
        _assert_books(e, book)

    # final: ledger FIFO books tie to engine positions per (kind, symbol)
    assert e.cash == pytest.approx(CAPITAL + _tape_cash(e.ledger))


# ---------------------------------------------------------------------------
# Part 4 -- mutation-style tests: symmetric mis-booking must go red
# ---------------------------------------------------------------------------

def test_mutation_symmetric_premium_halving_goes_red():
    """If both premium legs were halved (a symmetric mis-booking that
    self-consistency checks cannot see), realized must read $60, not $120."""
    led = Ledger(":memory:")
    ts = _ts(1)
    # halved tape: 2 contracts @ 0.60, closed @ 0.30
    led.record_trade(ts, "vrp", "AAA", "sell_put", 2, 0.60, 118.70, "")
    led.record_trade(ts, "vrp", "AAA", "buy_put_close", 2, 0.30, -61.30, "")
    s = _stats(led)["vrp"]
    assert s["realized"] == pytest.approx(60.00)   # 2*(0.60-0.30)*100
    assert s["realized"] != pytest.approx(120.00)  # the correct tape's value


def test_mutation_points_vs_dollars_caught():
    """The historical 100x bug: premium in points treated as dollars would
    report $1.20 instead of $120.00.  The absolute assertion pins dollars."""
    led = Ledger(":memory:")
    ts = _ts(1)
    led.record_trade(ts, "vrp", "AAA", "sell_put", 2, 1.20, 238.70, "")
    led.record_trade(ts, "vrp", "AAA", "buy_put_close", 2, 0.60, -121.30, "")
    s = _stats(led)["vrp"]
    assert s["realized"] == pytest.approx(120.00)
    # guard against the points-scale reading (2 * 0.60 = 1.20)
    assert abs(s["realized"]) > 10.0


def test_mutation_expiry_attribution_strategy_not_engine():
    """The historical attribution bug: expiry booked under 'engine' left
    FIFO lots orphaned.  Expiry must carry the opener's strategy."""
    e = _engine()
    e._apply_fill(Signal("tail", "AAA", "sell_put", 1, strike=100,
                         expiry=date(2024, 2, 16), limit_price=2.00,
                         meta={"iv": 0.5}), 2.00, _ts(1))
    e.latest["AAA"] = Bar(ts=_ts(10), symbol="AAA", open=120, high=120,
                          low=120, close=120, volume=0)
    e._settle_expiries(date(2024, 2, 16), _ts(10))
    strats = {t["strategy"] for t in e.ledger.trades(limit=10)}
    assert "engine" not in strats, f"expiry leaked to 'engine': {strats}"
    assert _stats(e.ledger)["tail"]["realized"] == pytest.approx(200.00)


# ---------------------------------------------------------------------------
# Part 5 -- real-data replay: reconcile harness on shipped backtest DBs
# ---------------------------------------------------------------------------

def test_replay_csi_100k_reconcile_invariants():
    from reconcile import (check_cash_walk, check_equity_decomposition,
                           check_open_lots)
    db = str(REPO / "backtests" / "csi_100k.db")
    for name, fn in [("cash_walk", check_cash_walk),
                     ("equity_decomposition", check_equity_decomposition),
                     ("open_lots", check_open_lots)]:
        ok, msg = fn(db)
        assert ok, f"{name}: {msg}"


def test_replay_ai_memory_reconcile_invariants():
    from reconcile import (check_cash_walk, check_equity_decomposition,
                           check_open_lots)
    db = str(REPO / "backtests" / "ai_memory.db")
    for name, fn in [("cash_walk", check_cash_walk),
                     ("equity_decomposition", check_equity_decomposition),
                     ("open_lots", check_open_lots)]:
        ok, msg = fn(db)
        assert ok, f"{name}: {msg}"


# ---------------------------------------------------------------------------
# Part 6 -- golden provisional fixture
# ---------------------------------------------------------------------------
# PROVISIONAL -- per the project plan this dataset is formally blessed only
# AFTER the execution-cost model lands (separate workstream: it changes trade
# attribution).  Until then it pins today's hand-computed expectations and
# must be re-blessed, not trusted, after any ledger/engine change.

def _golden_db() -> str:
    db = str(REPO / "tests" / "fixtures" /
             "golden_reconcile_v0_provisional.db")
    if not Path(db).exists():
        from tests.make_golden_fixture import main as _make
        _make()
    return db


def test_golden_provisional_fixture_reconciles():
    from reconcile import (check_cash_walk, check_equity_decomposition,
                           check_open_lots)
    db = _golden_db()
    for name, fn in [("cash_walk", check_cash_walk),
                     ("equity_decomposition", check_equity_decomposition),
                     ("open_lots", check_open_lots)]:
        ok, msg = fn(db)
        assert ok, f"golden {name}: {msg}"


def test_golden_provisional_absolute_numbers():
    """Spot-checks the hand-computed figures baked into the fixture."""
    led = Ledger(_golden_db())
    # hand walk: -500.00 + 198.70 - 101.30 + 530.00 = +127.40 net
    s = _stats(led)["vrp"]
    assert s["net"] == pytest.approx(127.40)
    # realized: spot 10*(53-50)=30 ; put 1*(2.00-1.00)*100=100 ; total 130
    assert s["realized"] == pytest.approx(130.00)
    curve = led.equity_curve()
    assert curve[-1]["equity"] == pytest.approx(100_127.40)
    assert curve[-1]["cash"] == pytest.approx(100_127.40)

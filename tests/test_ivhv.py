"""IV solver + HV calculator: round-trips, fail-closed gates, interpolation."""

import math

import pandas as pd
import pytest

from ivhv import atm_iv_for_expiry, implied_vol, iv_at_dte, realized_vol
from pricing import bs_call_price, bs_put_price


def test_iv_roundtrip_call():
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.04, 0.30
    price = bs_call_price(S, K, T, r, sigma)
    assert implied_vol(price, S, K, T, r, True) == pytest.approx(sigma, abs=1e-4)


def test_iv_roundtrip_put_otm():
    S, K, T, r, sigma = 150.0, 140.0, 0.5, 0.04, 0.45
    price = bs_put_price(S, K, T, r, sigma)
    assert implied_vol(price, S, K, T, r, False) == pytest.approx(sigma, abs=1e-4)


def test_iv_rejects_sub_intrinsic_price():
    # call worth less than intrinsic: arbitrage-violating quote -> None
    assert implied_vol(1.0, 100.0, 90.0, 0.25, 0.04, True) is None


def test_iv_rejects_degenerate():
    assert implied_vol(1.0, 100.0, 100.0, 0.0, 0.04, True) is None
    assert implied_vol(-1.0, 100.0, 100.0, 0.25, 0.04, True) is None


def test_realized_vol_flat_series_is_zero():
    s = pd.Series([100.0] * 60)
    hv = realized_vol(s, 30)
    assert hv.iloc[-1] == pytest.approx(0.0, abs=1e-12)


def test_realized_vol_known_magnitude():
    # alternating +-1% log returns: std ~= 0.01 -> annualized ~= 0.01*sqrt(252)
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
    hv = realized_vol(pd.Series(closes), 30)
    assert hv.iloc[-1] == pytest.approx(0.01 * math.sqrt(252), rel=0.05)


def test_iv_at_dte_interpolates_and_clamps():
    rows = [{"dte": 10, "avg": 0.20}, {"dte": 50, "avg": 0.30}]
    assert iv_at_dte(rows, 30.0) == pytest.approx(0.25)
    assert iv_at_dte(rows, 5.0) == pytest.approx(0.20)   # clamps low
    assert iv_at_dte(rows, 99.0) == pytest.approx(0.30)  # clamps high
    assert iv_at_dte([], 30.0) is None


def _chain(strikes, iv, S=100.0, T=0.25, r=0.04, bad=()):
    rows = []
    for i, K in enumerate(strikes):
        c = bs_call_price(S, K, T, r, iv)
        p = bs_put_price(S, K, T, r, iv)
        bid_c, ask_c = (0.0, 0.0) if i in bad else (c * 0.98, c * 1.02)
        rows.append({"strike": K, "bid": bid_c, "ask": ask_c,
                     "_put_bid": p * 0.98, "_put_ask": p * 1.02})
    calls = pd.DataFrame([{"strike": r["strike"], "bid": r["bid"],
                           "ask": r["ask"]} for r in rows])
    puts = pd.DataFrame([{"strike": r["strike"], "bid": r["_put_bid"],
                          "ask": r["_put_ask"]} for r in rows])
    return calls, puts


def test_atm_iv_picks_nearest_strike_and_averages_legs():
    calls, puts = _chain([90.0, 100.0, 110.0], iv=0.35)
    out = atm_iv_for_expiry(calls, puts, S=100.0, T_years=0.25, r=0.04)
    assert out is not None
    assert out["call"] == pytest.approx(0.35, abs=1e-3)
    assert out["put"] == pytest.approx(0.35, abs=1e-3)
    assert out["avg"] == pytest.approx(0.35, abs=1e-3)


def test_atm_iv_skips_one_sided_quotes():
    # calls have no two-sided quotes -> only the put leg survives
    calls, puts = _chain([100.0], iv=0.35, bad=(0,))
    out = atm_iv_for_expiry(calls, puts, S=100.0, T_years=0.25, r=0.04)
    assert out is not None and "call" not in out
    assert out["put"] == pytest.approx(0.35, abs=1e-3)


def test_atm_iv_none_when_no_quotes():
    calls = pd.DataFrame([{"strike": 100.0, "bid": 0.0, "ask": 0.0}])
    puts = pd.DataFrame([{"strike": 100.0, "bid": 0.0, "ask": 1.5}])
    assert atm_iv_for_expiry(calls, puts, 100.0, 0.25, 0.04) is None

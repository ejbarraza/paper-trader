"""Tests for panoptic_lab.py -- all synthetic paths, no network."""

import math

import pytest

from panoptic_lab import bs_gamma, hv_annualized, simulate, v3_amounts

D = 10_000.0
S0, PA, PB = 100_000.0, 85_500.0, 95_000.0
L = D / (math.sqrt(PB) - math.sqrt(PA))


def _prices(start, moves):
    """Build (day, close) series from daily gross moves."""
    out, p = [], start
    for i, m in enumerate(moves):
        p *= m
        out.append((f"2026-01-{i + 1:02d}", p))
    return out


def test_v3_above_range_all_usdc():
    x, y = v3_amounts(120_000.0, PA, PB, L)
    assert x == 0.0
    assert y == pytest.approx(D)
    assert x * 120_000.0 + y == pytest.approx(D)


def test_v3_below_range_all_token():
    x, y = v3_amounts(50_000.0, PA, PB, L)
    assert y == 0.0
    assert x > 0.0
    # fully assigned: worth less than the USDC deposit
    assert x * 50_000.0 < D


def test_v3_value_monotonic_falling():
    vals = [v3_amounts(p, PA, PB, L)[0] * p + v3_amounts(p, PA, PB, L)[1]
            for p in (120_000, 100_000, 94_000, 90_000, 85_500, 70_000)]
    # flat while above the range, strictly falling once price enters it
    assert vals[0] == pytest.approx(vals[1]) == pytest.approx(D)
    assert all(b < a for a, b in zip(vals[1:], vals[2:]))


def test_v3_continuous_at_edges():
    eps = 1e-6
    for edge in (PA, PB):
        lo = v3_amounts(edge * (1 - eps), PA, PB, L)
        hi = v3_amounts(edge * (1 + eps), PA, PB, L)
        vlo = lo[0] * edge + lo[1]
        vhi = hi[0] * edge + hi[1]
        assert vlo == pytest.approx(vhi, rel=1e-6)


def test_gamma_positive_and_peaks_near_strike():
    g_atm = bs_gamma(95_000, PB, 30 / 365, 0.04, 0.6)
    g_far = bs_gamma(150_000, PB, 30 / 365, 0.04, 0.6)
    assert g_atm > 0
    assert g_atm > g_far


def test_flat_path_panoptic_earns_streamia_only():
    prices = _prices(S0, [1.0] * 30)
    sim = simulate(prices, otm=0.05, width=0.10, iv=0.6,
                   notional=D, dte=30, r=0.04)
    assert sim["streamia_total"] > 0
    # no price move -> position stays all-USDC, P&L == streamia
    assert sim["pan_pnl"] == pytest.approx(sim["streamia_total"], rel=1e-9)
    assert not sim["assigned"]


def test_crash_path_assignment_loses():
    prices = _prices(S0, [0.97] * 30)  # ~ -60% over 30d
    sim = simulate(prices, otm=0.05, width=0.10, iv=0.6,
                   notional=D, dte=30, r=0.04)
    assert sim["assigned"]
    assert sim["pan_pnl"] < 0
    assert sim["pan_pnl"] < sim["streamia_total"]  # streamia can't cover it


def test_streamia_apr_scales_with_iv():
    prices = _prices(S0, [1.0] * 30)
    lo = simulate(prices, otm=0.05, width=0.10, iv=0.3,
                  notional=D, dte=30, r=0.04)
    hi = simulate(prices, otm=0.05, width=0.10, iv=0.9,
                  notional=D, dte=30, r=0.04)
    assert hi["streamia_apr"] > lo["streamia_apr"] > 0


def test_range_below_spot_required():
    prices = _prices(S0, [1.0] * 10)
    with pytest.raises(ValueError):
        simulate(prices, otm=-0.05, width=0.10, iv=0.6,
                 notional=D, dte=30, r=0.04)


def test_hv_flat_series_is_zero():
    assert hv_annualized([100.0] * 40) == pytest.approx(0.0)


def test_hv_known_vol():
    # alternating +-1% daily moves: daily sd ~1%, annualized ~19%
    moves = [1.01 if i % 2 else 0.99 for i in range(60)]
    prices, p = [], 100.0
    for m in moves:
        p *= m
        prices.append(p)
    hv = hv_annualized(prices)
    assert 0.15 < hv < 0.25


def test_tradfi_put_rolls_collect_multiple_premiums():
    prices = _prices(S0, [1.0] * 30)  # flat, dte=10 -> 3 rolls
    sim = simulate(prices, otm=0.05, width=0.10, iv=0.6,
                   notional=D, dte=10, r=0.04)
    # three premiums collected; the last put still has 1 day of time value
    # left at the end, so P&L sits just under 3x the first premium
    assert 2.9 * sim["first_premium"] < sim["trad_pnl"] \
        < 3 * sim["first_premium"]


def test_tradfi_roll_realizes_assignment_loss():
    prices = _prices(S0, [0.985] * 25)  # steady bleed through the strike
    sim = simulate(prices, otm=0.05, width=0.10, iv=0.6,
                   notional=D, dte=10, r=0.04)
    assert sim["trad_pnl"] < sim["pan_pnl"] or sim["trad_pnl"] < 0

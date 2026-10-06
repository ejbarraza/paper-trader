#!/usr/bin/env python3
"""Option pricing helpers for the paper engine.

The engine marks options between entry and expiry with Black-Scholes
(needed for close decisions and daily equity marks). IV is the entry-time
implied vol from the scanner -- held constant and documented as an
assumption, since historical chains are not available.
"""

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T_years: float, r: float, sigma: float):
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T_years) / (sigma * math.sqrt(T_years))
    return d1, d1 - sigma * math.sqrt(T_years)


def bs_put_price(S: float, K: float, T_years: float, r: float,
                 sigma: float) -> float:
    """European put value. Returns intrinsic when expired or degenerate."""
    if T_years <= 0:
        return max(K - S, 0.0)
    if sigma <= 0 or S <= 0:
        return max(K - S, 0.0)
    d1, d2 = _d1_d2(S, K, T_years, r, sigma)
    return K * math.exp(-r * T_years) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_call_price(S: float, K: float, T_years: float, r: float,
                  sigma: float) -> float:
    """European call value. Returns intrinsic when expired or degenerate."""
    if T_years <= 0:
        return max(S - K, 0.0)
    if sigma <= 0 or S <= 0:
        return max(S - K, 0.0)
    d1, d2 = _d1_d2(S, K, T_years, r, sigma)
    return S * _norm_cdf(d1) - K * math.exp(-r * T_years) * _norm_cdf(d2)

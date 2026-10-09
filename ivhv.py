#!/usr/bin/env python3
"""IV vs HV calculator and plots (paper-trader analysis tool).

Fetches an option chain plus price history from Yahoo Finance (no
credentials), backs out implied volatility by inverting the Black-Scholes
pricer in pricing.py (bisection, fail-closed on missing two-sided quotes),
computes realized (historical) volatility from log returns, and plots:

  1. IV term structure: ATM implied vol vs days-to-expiry, with the current
     30-day realized vol as a reference line -- the VRP picture the
     put-selling leg trades on.
  2. HV regime: 10-day and 30-day realized vol over the past year, with the
     latest ~30-day ATM IV as a reference line.

Typical use:
    python -m ivhv SPY
    python run.py iv-hv RDDT --days 365 --out outputs/ivhv_rddt.png

Notes / assumptions (same conventions as the engine):
  - European Black-Scholes, no dividends (matches the engine's modeling).
  - Mid-price of two-sided quotes only; options with bid<=0 or ask<=0 are
    skipped (fail-closed quote gates).
  - Annualization uses 252 trading days; DTE uses 365 calendar days.
"""

from __future__ import annotations

import argparse
import math
from datetime import date

import matplotlib

matplotlib.use("Agg")  # headless: never try to open a window
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from pricing import bs_call_price, bs_put_price

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# implied volatility (bisection on the BS pricer)
# --------------------------------------------------------------------------
def implied_vol(price: float, S: float, K: float, T_years: float, r: float,
                is_call: bool, tol: float = 1e-6) -> float | None:
    """Back out sigma from a market price. None if uninvertible.

    Fail-closed: returns None for arbitrage-violating prices (below
    intrinsic) or prices outside the BS attainable range, and for any
    non-finite input (a NaN spot/price previously inverted to the 500%
    bisection cap instead of refusing).
    """
    if not (math.isfinite(price) and math.isfinite(S) and math.isfinite(K)
            and math.isfinite(T_years) and math.isfinite(r)):
        return None
    if T_years <= 0 or S <= 0 or price < 0:
        return None
    pricer = bs_call_price if is_call else bs_put_price
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if price < intrinsic - 1e-9:
        return None
    lo, hi = 1e-4, 5.0
    f_lo = pricer(S, K, T_years, r, lo) - price
    f_hi = pricer(S, K, T_years, r, hi) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = pricer(S, K, T_years, r, mid) - price
        if abs(f_mid) < tol:
            return mid
        if f_mid * f_lo < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------
# realized (historical) volatility
# --------------------------------------------------------------------------
def realized_vol(closes: pd.Series, window: int) -> pd.Series:
    """Annualized rolling realized vol of log returns."""
    closes = pd.Series(closes).dropna()
    logret = np.log(closes / closes.shift(1)).dropna()
    return logret.rolling(window).std(ddof=1) * math.sqrt(TRADING_DAYS)


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------
def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    # yfinance >= 1.x returns (field, ticker) MultiIndex columns even for a
    # single ticker; flatten so df["Close"] resolves (same as feeds.py).
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_history(ticker: str, days: int = 365) -> pd.DataFrame:
    import yfinance as yf

    hist = _flatten(yf.Ticker(ticker).history(period="1y", auto_adjust=False))
    hist = hist.dropna(subset=["Close"])
    return hist.tail(days + 40)  # pad so rolling windows are defined


def atm_iv_for_expiry(chain_calls: pd.DataFrame, chain_puts: pd.DataFrame,
                      S: float, T_years: float, r: float) -> dict | None:
    """Average call/put IV at the strike nearest the underlying.

    Skips legs without two-sided quotes or failed inversions; returns None
    when neither leg yields an IV.
    """
    out = {}
    for name, frame, is_call in (("call", chain_calls, True),
                                 ("put", chain_puts, False)):
        if frame is None or frame.empty:
            continue
        q = frame[(frame["bid"] > 0) & (frame["ask"] > 0)].copy()
        if q.empty:
            continue
        q["mid"] = (q["bid"] + q["ask"]) / 2.0
        row = q.iloc[(q["strike"] - S).abs().argsort()[:1]].iloc[0]
        iv = implied_vol(float(row["mid"]), S, float(row["strike"]),
                         T_years, r, is_call)
        if iv is not None:
            out[name] = iv
    if not out:
        return None
    out["avg"] = sum(out.values()) / len(out)
    return out


def build_term_structure(ticker: str, r: float, today: date | None = None):
    """ATM IV per expiry. Returns (rows, S)."""
    import yfinance as yf

    today = today or date.today()
    t = yf.Ticker(ticker)
    expiries = t.options or []
    if not expiries:
        raise SystemExit(f"no option expiries found for {ticker}")
    closes = _flatten(t.history(period="5d", auto_adjust=False))["Close"].dropna()
    if closes.empty:
        raise SystemExit(f"no usable close for {ticker} (yfinance returned no data)")
    S = float(closes.iloc[-1])
    if not math.isfinite(S):
        raise SystemExit(f"no usable close for {ticker} (yfinance returned NaN)")
    rows = []
    for exp in expiries:
        dte = (date.fromisoformat(exp) - today).days
        if dte < 1:
            continue
        T = dte / 365.0
        try:
            chain = t.option_chain(exp)
        except Exception:
            continue  # fail-closed on bad expiry payloads
        ivs = atm_iv_for_expiry(_flatten(chain.calls), _flatten(chain.puts),
                                S, T, r)
        if ivs is None:
            continue
        rows.append({"expiry": exp, "dte": dte, **ivs})
    return rows, S


def iv_at_dte(rows: list[dict], target_dte: float = 30.0) -> float | None:
    """Linear-interpolate ATM IV at a target DTE (nearest if outside range)."""
    if not rows:
        return None
    pts = sorted(((r["dte"], r["avg"]) for r in rows), key=lambda p: p[0])
    if target_dte <= pts[0][0]:
        return pts[0][1]
    if target_dte >= pts[-1][0]:
        return pts[-1][1]
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target_dte <= d1:
            w = (target_dte - d0) / (d1 - d0)
            return v0 + w * (v1 - v0)
    return None


# --------------------------------------------------------------------------
# plot
# --------------------------------------------------------------------------
def plot_iv_hv(ticker: str, hist: pd.DataFrame, rows: list[dict],
               S: float, r: float, out: str) -> dict:
    hv10 = realized_vol(hist["Close"], 10)
    hv30 = realized_vol(hist["Close"], 30)
    hv30_now = float(hv30.iloc[-1])
    iv30 = iv_at_dte(rows, 30.0)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8), sharex=False)
    fig.suptitle(f"{ticker} — IV vs HV  (S=${S:,.2f}, r={r:.1%})", fontsize=13)

    # panel 1: HV regime over time
    ax1.plot(hv10.index, hv10 * 100, label="HV 10d", linewidth=1.2)
    ax1.plot(hv30.index, hv30 * 100, label="HV 30d", linewidth=1.6)
    if iv30 is not None:
        ax1.axhline(iv30 * 100, color="tab:orange", linestyle="--",
                    label=f"ATM IV ~30d ({iv30:.1%})")
    ax1.set_ylabel("vol (annualized %)")
    ax1.set_title("Realized-vol regime (past year)")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)

    # panel 2: IV term structure today
    dtes = [r["dte"] for r in rows]
    ivs = [r["avg"] * 100 for r in rows]
    ax2.plot(dtes, ivs, marker="o", linewidth=1.6, label="ATM IV")
    ax2.axhline(hv30_now * 100, color="tab:green", linestyle="--",
                label=f"HV 30d now ({hv30_now:.1%})")
    ax2.set_xlabel("days to expiry")
    ax2.set_ylabel("IV (annualized %)")
    ax2.set_title("IV term structure (today)")
    ax2.legend(loc="best")
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return {"hv30": hv30_now, "iv30": iv30,
            "vrp": (iv30 - hv30_now) if iv30 is not None else None}


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description="plot IV vs HV for a ticker")
    ap.add_argument("ticker")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--risk-free", type=float, default=0.04)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    ticker = args.ticker.upper()
    out = args.out or f"outputs/ivhv_{ticker.lower()}_{date.today():%Y%m%d}.png"

    hist = fetch_history(ticker, args.days)
    rows, S = build_term_structure(ticker, args.risk_free)
    if not rows:
        raise SystemExit(f"no usable option quotes for {ticker}")
    summary = plot_iv_hv(ticker, hist, rows, S, args.risk_free, out)

    print(f"{ticker}: S=${S:,.2f}")
    print(f"{'expiry':12s} {'dte':>4s} {'IVcall':>7s} {'IVput':>7s} "
          f"{'IVavg':>7s} {'VRP':>7s}")
    for r in rows:
        vrp = r["avg"] - summary["hv30"]
        print(f"{r['expiry']:12s} {r['dte']:4d} "
              f"{r.get('call', float('nan')):7.1%} "
              f"{r.get('put', float('nan')):7.1%} "
              f"{r['avg']:7.1%} {vrp:+7.1%}")
    iv30 = summary["iv30"]
    if iv30 is not None:
        print(f"IV~30d={iv30:.1%}  HV30d={summary['hv30']:.1%}  "
              f"VRP={summary['vrp']:+.1%}")
    print(f"wrote {out}")
    return summary


if __name__ == "__main__":
    main()

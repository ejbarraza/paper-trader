#!/usr/bin/env python3
"""Panoptic lab: simulate a perpetual short-put, Panoptic-style, on CEX data.

What this models (documented assumptions -- not the protocol's exact accounting):
  * Selling a put  = single-sided USDC liquidity in a Uniswap v3 range [Pa, Pb]
    below spot.  Position value follows EXACT Uniswap v3 math (no magic there):
    price above the range -> all USDC; price inside -> progressively converted
    to the token (like gradual assignment); price below -> fully assigned.
  * Streamia (streaming premium) accrues at the fair rate, which is the
    gamma-theta identity: a short-gamma position fairly earns
        streamia dt = 1/2 * Gamma * S^2 * sigma_iv^2 * dt
    i.e. you are paid implied variance on your gamma exposure.  This is the
    VRP-harvest identity in streaming form: if realized vol < IV you profit,
    same economics as a cash-secured put, different wrapper.
  * Comparison leg: TradFi DTE-day puts struck at the range top, rolled at
    expiry and re-struck at the same OTM (mirrors real put-selling), marked
    daily with the project's Black-Scholes pricer, sized cash-secured
    against the same collateral D.

Data: Binance klines (no key) with a Yahoo fallback.  No chain reads -- the
whole lab runs on CEX prices, which is the point: pool-state RPC only becomes
necessary if this ever graduates to live integration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import urllib.request

from pricing import bs_put_price

BINANCE_MAP = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT",
    "HYPE": "HYPEUSDT",
}

REF_TENOR_YEARS = 30 / 365  # reference expiry for the streamia gamma


# ---------------------------------------------------------------- data

def fetch_prices(ticker: str, days: int) -> list[tuple[str, float]]:
    """Daily closes, oldest first.  Binance first, Yahoo fallback."""
    symbol = BINANCE_MAP.get(ticker.upper(), ticker.upper() + "USDT")
    url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
           f"&interval=1d&limit={days}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "paper-trader-lab"})
        with urllib.request.urlopen(req, timeout=20) as r:
            klines = json.loads(r.read().decode())
        out = []
        for k in klines:
            day = dt.datetime.fromtimestamp(k[0] / 1000,
                                            tz=dt.timezone.utc).strftime("%Y-%m-%d")
            out.append((day, float(k[4])))
        if len(out) >= 10:
            return out
    except Exception:
        pass
    # fallback: Yahoo
    import yfinance as yf
    df = yf.download(f"{ticker.upper()}-USD", period=f"{days}d",
                     interval="1d", progress=False, auto_adjust=True)
    closes = df["Close"].dropna()
    col = closes.columns[0] if hasattr(closes, "columns") else None
    series = closes[col] if col is not None else closes
    return [(d.strftime("%Y-%m-%d"), float(v)) for d, v in series.items()]


def hv_annualized(closes: list[float], window: int = 30) -> float:
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-window:]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * 365)


# ---------------------------------------------------------------- math

def bs_gamma(S: float, K: float, T_years: float, r: float, sigma: float) -> float:
    if T_years <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T_years) \
        / (sigma * math.sqrt(T_years))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) \
        / (S * sigma * math.sqrt(T_years))


def v3_amounts(P: float, Pa: float, Pb: float, L: float) -> tuple[float, float]:
    """(token0, token1) for liquidity L over [Pa, Pb] at price P (t0/t1)."""
    sa, sb, sp = math.sqrt(Pa), math.sqrt(Pb), math.sqrt(max(P, 1e-12))
    if sp >= sb:
        return 0.0, L * (sb - sa)
    if sp <= sa:
        return L * (1 / sa - 1 / sb), 0.0
    return L * (1 / sp - 1 / sb), L * (sp - sa)


# ---------------------------------------------------------------- simulation

def simulate(prices: list[tuple[str, float]], *, otm: float, width: float,
             iv: float, notional: float, dte: int, r: float) -> dict:
    closes = [p[1] for p in prices]
    S0 = closes[0]
    Pb = S0 * (1 - otm)          # range top  = TradFi strike
    Pa = Pb * (1 - width)        # range bottom
    if S0 <= Pb:
        raise ValueError("range top must be below spot")
    D = notional
    L = D / (math.sqrt(Pb) - math.sqrt(Pa))

    T0 = dte / 365

    # TradFi leg: repeated DTE-day cash-secured puts, rolled at expiry.
    # (Mirrors real put-selling: each roll re-strikes at the same OTM.)
    trad_cum_premium = 0.0
    trad_assign_loss = 0.0
    K = Pb
    n_puts = D / Pb
    t_sold = 0
    first_premium = None

    def _sell_put(S_now: float, K_now: float):
        nonlocal trad_cum_premium, first_premium
        n = D / K_now
        prem = n * bs_put_price(S_now, K_now, T0, r, iv)
        trad_cum_premium += prem
        if first_premium is None:
            first_premium = prem
        return n

    n_puts = _sell_put(S0, Pb)

    rows, cum_streamia = [], 0.0
    peak = -1e18
    max_dd = 0.0
    for i, (day, S) in enumerate(prices):
        if i - t_sold >= dte and i < len(prices) - 1:
            # old put expires: realize intrinsic assignment loss, sell new
            trad_assign_loss += n_puts * max(K - S, 0.0)
            K = S * (1 - otm)
            n_puts = _sell_put(S, K)
            t_sold = i
        x, y = v3_amounts(S, Pa, Pb, L)
        pos_value = x * S + y
        g = bs_gamma(S, Pb, REF_TENOR_YEARS, r, iv)
        streamia_day = (D / Pb) * 0.5 * g * S * S * iv * iv / 365
        cum_streamia += streamia_day
        pan_equity = pos_value + cum_streamia
        T_rem = max(T0 - (i - t_sold) / 365, 0.0)
        trad_equity = trad_cum_premium - trad_assign_loss \
            - n_puts * bs_put_price(S, K, T_rem, r, iv)
        peak = max(peak, pan_equity)
        max_dd = min(max_dd, (pan_equity - peak) / peak if peak > 0 else 0.0)
        rows.append({"day": day, "S": S, "pan_equity": pan_equity,
                     "pan_pnl": pan_equity - D, "trad_pnl": trad_equity,
                     "streamia_day": streamia_day,
                     "cum_streamia": cum_streamia,
                     "in_range": Pa < S < Pb})

    n_days = len(rows)
    return {
        "S0": S0, "Pa": Pa, "Pb": Pb, "D": D, "L": L,
        "iv": iv, "first_premium": first_premium, "rows": rows,
        "streamia_total": cum_streamia,
        "streamia_apr": cum_streamia / D * 365 / n_days,
        "pan_pnl": rows[-1]["pan_pnl"],
        "pan_ret": rows[-1]["pan_pnl"] / D,
        "trad_pnl": rows[-1]["trad_pnl"],
        "trad_ret": rows[-1]["trad_pnl"] / D,
        "max_dd": max_dd,
        "assigned": min(closes) <= Pa,
        "days_in_range": sum(1 for row in rows if row["in_range"]),
        "min_price": min(closes),
    }


def plot(sim: dict, ticker: str, out: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = sim["rows"]
    days = [r["day"] for r in rows]
    S = [r["S"] for r in rows]
    pan = [r["pan_pnl"] for r in rows]
    trad = [r["trad_pnl"] for r in rows]
    xs = range(len(rows))

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    fig.suptitle(f"{ticker} — Panoptic-style perpetual short put "
                 f"(range ${sim['Pa']:,.0f}–${sim['Pb']:,.0f}, "
                 f"IV {sim['iv']:.0%}, D=${sim['D']:,.0f})")
    a1.plot(xs, S, label="price", color="black", lw=1.2)
    a1.axhline(sim["Pb"], color="tab:blue", ls="--", label="range top / strike")
    a1.axhline(sim["Pa"], color="tab:red", ls="--", label="range bottom")
    a1.fill_between(xs, sim["Pa"], sim["Pb"], color="tab:blue", alpha=0.08)
    a1.set_ylabel("price (USDC)")
    a1.legend(fontsize=8)
    a1.grid(alpha=0.3)

    a2.plot(xs, pan, label="Panoptic leg (v3 position + streamia)")
    a2.plot(xs, trad, label="TradFi 30d short-put rolls", ls="--")
    a2.axhline(0, color="black", lw=0.8)
    a2.set_ylabel("cumulative P&L (USDC)")
    a2.set_xlabel("day")
    step = max(1, len(xs) // 8)
    a2.set_xticks(list(xs)[::step], [d[5:] for d in days[::step]],
                  rotation=30, fontsize=8)
    a2.legend(fontsize=8)
    a2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ticker", nargs="?", default="BTC")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--otm", type=float, default=0.05,
                    help="range top below spot, fraction (default 0.05)")
    ap.add_argument("--width", type=float, default=0.10,
                    help="range width as fraction of range top (default 0.10)")
    ap.add_argument("--iv", type=float, default=None,
                    help="implied vol (default: 30d HV of the path)")
    ap.add_argument("--notional", type=float, default=10000)
    ap.add_argument("--dte", type=int, default=30,
                    help="TradFi comparison put expiry, days")
    ap.add_argument("--risk-free", type=float, default=0.04)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    prices = fetch_prices(a.ticker, a.days)
    closes = [p[1] for p in prices]
    iv = a.iv if a.iv is not None else hv_annualized(closes)
    iv_source = "given" if a.iv is not None else "30d HV"

    sim = simulate(prices, otm=a.otm, width=a.width, iv=iv,
                   notional=a.notional, dte=a.dte, r=a.risk_free)
    out = a.out or (f"outputs/panoptic_{a.ticker.lower()}_"
                    f"{dt.date.today():%Y%m%d}.png")
    plot(sim, a.ticker.upper(), out)

    print(f"{a.ticker.upper()}: S0=${sim['S0']:,.2f}  "
          f"range=[${sim['Pa']:,.0f}, ${sim['Pb']:,.0f}]  "
          f"IV={iv:.1%} ({iv_source})  D=${sim['D']:,.0f}")
    print(f"streamia collected: ${sim['streamia_total']:,.2f}  "
          f"(effective APR {sim['streamia_apr']:.1%})")
    print(f"Panoptic leg P&L: ${sim['pan_pnl']:,.2f} "
          f"({sim['pan_ret']:+.1%})  maxDD {sim['max_dd']:.1%}  "
          f"assigned={'yes' if sim['assigned'] else 'no'}  "
          f"days in range: {sim['days_in_range']}")
    print(f"TradFi {a.dte}d short-put rolls P&L: ${sim['trad_pnl']:,.2f} "
          f"({sim['trad_ret']:+.1%})  first premium=${sim['first_premium']:,.2f}")
    print(f"wrote {out}")
    return sim


if __name__ == "__main__":
    main()

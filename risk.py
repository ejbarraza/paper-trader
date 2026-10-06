#!/usr/bin/env python3
"""The shared risk arbiter.

One choke point, every strategy, every order. Strategies propose; the arbiter
disposes. Rejections carry machine-readable reasons (in the same spirit as the
options scanner's named exclusions) so the dashboard can show exactly which
rule saved you from yourself.

Design notes:
- Exits (``reduce_only`` signals) always pass the kill-switches and exposure
  checks: risk management must never trap you in a position. They still need
  a valid quote.
- Notional for a short put is strike x 100 x contracts (the assignment
  obligation), not the premium -- the arbiter measures what can hurt you.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strategies import Signal


@dataclass
class RiskConfig:
    per_strategy_capital: dict[str, float] = field(default_factory=dict)
    default_strategy_capital: float = 30_000.0
    max_position_notional: float = 50_000.0
    max_portfolio_exposure_frac: float = 0.80
    price_band_frac: float = 0.02
    daily_loss_limit_frac: float = 0.03
    max_drawdown_frac: float = 0.15


@dataclass
class PortfolioState:
    cash: float
    equity: float
    peak_equity: float
    day_start_equity: float
    strategy_exposure: dict[str, float]  # strategy -> deployed notional
    symbol_exposure: dict[str, float]  # symbol -> deployed notional


class RiskArbiter:
    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()

    @staticmethod
    def signal_notional(signal: Signal, fill_price: float) -> float:
        if signal.action == "sell_put":
            return signal.quantity * (signal.strike or 0.0) * 100.0
        if signal.action == "buy_put_close":
            return 0.0  # exits free exposure
        return abs(signal.quantity) * fill_price  # spot

    def check(self, signal: Signal, est_fill: float,
              state: PortfolioState) -> tuple[bool, str]:
        """Returns (approved, reason). Reason is "" when approved."""
        c = self.config
        if est_fill <= 0:
            return False, "no_quote"

        if not signal.reduce_only:
            # --- kill-switches (new risk only) ---
            if state.peak_equity > 0:
                dd = (state.peak_equity - state.equity) / state.peak_equity
                if dd > c.max_drawdown_frac:
                    return False, "drawdown_killswitch"
            if state.day_start_equity > 0:
                dl = (state.day_start_equity - state.equity) / state.day_start_equity
                if dl > c.daily_loss_limit_frac:
                    return False, "daily_loss_killswitch"

            # --- exposure gates ---
            notional = self.signal_notional(signal, est_fill)
            strat_cap = c.per_strategy_capital.get(signal.strategy,
                                                   c.default_strategy_capital)
            if state.strategy_exposure.get(signal.strategy, 0.0) + notional > strat_cap:
                return False, "strategy_budget_exceeded"
            if state.symbol_exposure.get(signal.symbol, 0.0) + notional > c.max_position_notional:
                return False, "position_limit_exceeded"
            total = sum(state.symbol_exposure.values()) + notional
            if state.equity > 0 and total > c.max_portfolio_exposure_frac * state.equity:
                return False, "portfolio_exposure_exceeded"

            # --- don't chase ---
            if signal.limit_price and signal.limit_price > 0:
                drift = abs(est_fill - signal.limit_price) / signal.limit_price
                if drift > c.price_band_frac:
                    return False, "price_band_violated"

        return True, ""

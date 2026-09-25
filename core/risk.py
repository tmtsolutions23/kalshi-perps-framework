"""
Risk management — position sizing, drawdown limits, circuit breakers.
Uses state.equity (not a hardcoded balance) so drawdowns actually work.
"""

import logging
from datetime import datetime, timezone, date
from typing import Optional

log = logging.getLogger(__name__)


class RiskManager:
    """Enforces risk parameters and makes position-sizing decisions."""

    def __init__(self, config: dict, state_manager):
        self.cfg = config.get("risk", {})
        self.state = state_manager

    def compute_position_size(self, suggested_leverage: float) -> tuple:
        """
        Returns (position_contracts, effective_leverage) or (0, 0) if no trade.

        Sizing is risk-first: contracts = (equity × risk_per_trade_pct) /
        stop_distance_per_contract, then clamped by leverage cap.
        Falls back to leverage-based sizing with account_fraction_per_trade.
        """
        state = self.state.get()
        equity = state.get("equity", 10000.0)
        price = self._current_price()

        if equity <= 0 or price <= 0:
            return 0, 0

        # Check circuit breakers first
        reason = self._check_circuit_breakers()
        if reason:
            log.warning("Circuit breaker: %s", reason)
            return 0, 0

        lev = max(
            self.cfg.get("min_leverage", 2.0),
            min(suggested_leverage, self.cfg.get("max_leverage", 4.0)),
        )

        # Risk-first sizing: fixed fraction of equity at risk
        risk_pct = self.cfg.get("risk_per_trade_pct", 0.01)  # 1% default
        risk_dollars = equity * risk_pct

        # R2-4: stop_dist must match actual SL placement (atr × atr_multiplier_sl)
        sl_mult = self.cfg.get("atr_multiplier_sl", 1.5)
        stop_dist = getattr(self, "_last_atr", price * 0.015) * sl_mult
        if stop_dist <= 0:
            stop_dist = price * 0.015 * sl_mult

        contracts = risk_dollars / stop_dist if stop_dist > 0 else 0
        contracts = round(contracts)

        # Clamp by leverage cap: notional must not exceed equity × lev
        max_contracts = int((equity * lev) / price)
        if contracts > max_contracts:
            contracts = max_contracts

        # Also clamp by account fraction
        fraction = self.cfg.get("account_fraction_per_trade", 0.25)
        fraction_contracts = int((equity * fraction * lev) / price)
        contracts = min(contracts, fraction_contracts)

        # R2-5: if clamps reduced to 0, return 0 — "too small to trade"
        if contracts < 1:
            return 0, 0

        realized_lev = (contracts * price) / max(equity, 1)
        return max(1, contracts), round(min(realized_lev, lev), 2)

    def set_atr(self, atr: float):
        """Store the latest ATR estimate for risk-first sizing."""
        self._last_atr = atr

    def _current_price(self) -> float:
        """Get current price from snapshot or state position entry price."""
        # This is set before each cycle by main.py
        return getattr(self, "_snapshot_price", 0)

    def _check_circuit_breakers(self) -> Optional[str]:
        """Returns reason string if a breaker is tripped, else None."""
        state = self.state.get()
        equity = state.get("equity", 10000.0)
        peak = state.get("peak_equity", equity)
        daily_start = state.get("daily_start_equity", equity)

        today = date.today().isoformat()
        last_check = state.get("last_daily_check_ts")
        if last_check != today:
            # New UTC day — roll daily anchor
            state["daily_start_equity"] = equity
            state["last_daily_check_ts"] = today
            daily_start = equity

        if equity > peak:
            state["peak_equity"] = equity
            peak = equity

        total_dd_pct = self.cfg.get("max_total_drawdown_pct", 25.0)
        daily_dd_pct = self.cfg.get("max_daily_drawdown_pct", 10.0)
        cb_dd_pct = self.cfg.get("circuit_breaker_drawdown_pct", 15.0)

        total_dd = (peak - equity) / peak * 100 if peak > 0 else 0

        if total_dd >= total_dd_pct:
            self.state.save()
            return f"Total drawdown {total_dd:.1f}% >= {total_dd_pct}% — manual reset required"

        if total_dd >= cb_dd_pct:
            self.state.save()
            return f"Circuit breaker: total drawdown {total_dd:.1f}% >= {cb_dd_pct}%"

        if daily_start > 0:
            daily_dd = (daily_start - equity) / daily_start * 100
            if daily_dd >= daily_dd_pct:
                self.state.save()
                return f"Daily drawdown {daily_dd:.1f}% >= {daily_dd_pct}% — paused until tomorrow"

        self.state.save()  # persist peak/daily anchor
        return None

    def check_entry_allowed(self) -> tuple:
        """
        Returns (allowed: bool, reason: str).
        """
        if self.state.is_paused():
            return False, f"Trading paused: {self.state.get().get('pause_reason', 'unknown')}"

        breaker = self._check_circuit_breakers()
        if breaker:
            return False, breaker

        return True, "ok"
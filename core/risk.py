"""
Risk management — position sizing, drawdown limits, circuit breakers.
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

    def compute_position_size(
        self,
        available_balance: float,
        current_price: float,
        current_leverage: float,
    ) -> tuple:
        """
        Returns (position_contracts, effective_leverage) or (0, 0) if no trade.

        Constraints applied:
          - Leverage clamped to [min_leverage, max_leverage]
          - Account fraction per trade capped
          - Circuit breakers checked
        """
        # Clamp leverage
        lev = max(
            self.cfg.get("min_leverage", 2.0),
            min(current_leverage, self.cfg.get("max_leverage", 4.0)),
        )

        # Check drawdown circuit breakers
        reason = self._check_circuit_breakers(available_balance)
        if reason:
            log.warning("Circuit breaker: %s", reason)
            return 0, 0

        # Notional = available * fraction * leverage
        fraction = self.cfg.get("account_fraction_per_trade", 0.25)
        notional = available_balance * fraction * lev

        if notional <= 0 or current_price <= 0:
            return 0, 0

        # Convert to contracts. NOTE: Kalshi market `price` is already the
        # per-contract price (0.0001 BTC at $84k ≈ $8.41), so contracts =
        # notional / per-contract-price. Do NOT multiply by contract_size again.
        contracts = notional / current_price

        # Round to whole contracts (min size)
        contracts = max(1, round(contracts))

        realized_leverage = (contracts * current_price) / (available_balance * fraction)
        realized_leverage = min(realized_leverage, self.cfg.get("max_leverage", 4.0))

        return int(contracts), round(realized_leverage, 2)

    def _check_circuit_breakers(self, current_balance: float) -> Optional[str]:
        """Returns reason string if a breaker is tripped, else None."""

        state = self.state.get()
        peak = state.get("peak_balance")
        if not peak or current_balance > peak:
            # update peak
            state["peak_balance"] = current_balance
            self.state.save()

        total_dd_pct = self.cfg.get("max_total_drawdown_pct", 25.0)
        daily_dd_pct = self.cfg.get("max_daily_drawdown_pct", 10.0)
        cb_dd_pct = self.cfg.get("circuit_breaker_drawdown_pct", 15.0)

        if peak and peak > 0:
            total_dd = (peak - current_balance) / peak * 100
            if total_dd >= total_dd_pct:
                return f"Total drawdown {total_dd:.1f}% >= {total_dd_pct}% — manual reset required"

            if total_dd >= cb_dd_pct:
                return f"Circuit breaker: total drawdown {total_dd:.1f}% >= {cb_dd_pct}%"

        # Daily drawdown
        today = date.today().isoformat()
        daily_start = state.get("daily_start_balance")
        if daily_start and daily_start > 0:
            daily_dd = (daily_start - current_balance) / daily_start * 100
            if daily_dd >= daily_dd_pct:
                return f"Daily drawdown {daily_dd:.1f}% >= {daily_dd_pct}% — paused until tomorrow"

        return None

    def check_entry_allowed(self, available_balance: float) -> tuple:
        """
        Returns (allowed: bool, reason: str).
        """
        if self.state.is_paused():
            return False, f"Trading paused: {self.state.get().get('pause_reason', 'unknown')}"

        breaker = self._check_circuit_breakers(available_balance)
        if breaker:
            return False, breaker

        return True, "ok"

    def compute_stop_prices(
        self,
        entry_price: float,
        side: str,  # "long" or "short"
        atr: float,
    ) -> dict:
        """
        Returns { stop_loss_price, take_profit_price } based on ATR multipliers.
        side: 'long' or 'short'
        """
        sl_mult = self.cfg.get("atr_multiplier_sl", 1.5)
        tp_mult = self.cfg.get("atr_multiplier_tp", 1.5)

        if side == "long":
            sl_price = entry_price - (atr * sl_mult)
            tp_price = entry_price + (atr * tp_mult)
        else:
            sl_price = entry_price + (atr * sl_mult)
            tp_price = entry_price - (atr * tp_mult)

        # Price must be positive
        sl_price = max(sl_price, 0.0001)
        tp_price = max(tp_price, 0.0001)

        return {
            "stop_loss_price": round(sl_price, 4),
            "take_profit_price": round(tp_price, 4),
        }
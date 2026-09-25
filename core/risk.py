"""Risk management — position sizing, Kelly-driven risk, drawdown circuit breakers."""
import logging
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)


class RiskManager:
    """Enforces risk parameters and makes position-sizing decisions."""

    def __init__(self, config: dict, state_manager):
        self.cfg = config.get("risk", {})
        self.state = state_manager
        self._atr_4h = None  # 4h resampled ATR for stop/target distances
        self._snapshot_price = 0

    def set_atr_4h(self, atr: float):
        """Set the 4h resampled ATR for stop/target sizing."""
        self._atr_4h = atr

    def compute_position_size(self, suggested_leverage: float) -> tuple:
        """
        Returns (contracts, effective_leverage) or (0, 0).
        Sizing is risk-first: contracts = (equity × risk_pct) / stop_distance.
        stop_distance comes from 4h resampled ATR × atr_multiplier_sl.
        """
        state = self.state.get()
        equity = state.get("equity", 500.0)
        price = self._snapshot_price

        if equity <= 0 or price <= 0:
            return 0, 0

        reason = self._check_circuit_breakers()
        if reason:
            log.warning("Circuit breaker: %s", reason)
            return 0, 0

        # Kelly-driven risk per trade (from state, updated each cycle)
        risk_pct = state.get("risk_per_trade_pct", self.cfg.get("risk_per_trade_pct", 0.01))
        risk_dollars = equity * risk_pct

        lev = max(
            self.cfg.get("min_leverage", 2.0),
            min(suggested_leverage, self.cfg.get("max_leverage", 6.0)),
        )

        sl_mult = self.cfg.get("atr_multiplier_sl", 1.5)
        stop_dist = (self._atr_4h or (price * 0.02)) * sl_mult
        if stop_dist <= 0:
            stop_dist = price * 0.02 * sl_mult

        contracts = risk_dollars / stop_dist if stop_dist > 0 else 0
        contracts = round(contracts)

        max_contracts = int((equity * lev) / price)
        contracts = min(contracts, max_contracts)

        fraction = self.cfg.get("account_fraction_per_trade", 0.5)
        fraction_contracts = int((equity * fraction * lev) / price)
        contracts = min(contracts, fraction_contracts)

        if contracts < 1:
            return 0, 0

        realized_lev = (contracts * price) / max(equity, 1)
        return max(1, contracts), round(min(realized_lev, lev), 2)

    def compute_4h_stop_prices(self, entry_price: float, side: str) -> dict:
        """Return {stop_loss, take_profit} using 4h resampled ATR."""
        atr = self._atr_4h or (entry_price * 0.02)
        sl_mult = self.cfg.get("atr_multiplier_sl", 1.5)
        tp_mult = self.cfg.get("atr_multiplier_tp", 3.0)

        if side == "long":
            sl = entry_price - (atr * sl_mult)
            tp = entry_price + (atr * tp_mult)
        else:
            sl = entry_price + (atr * sl_mult)
            tp = entry_price - (atr * tp_mult)

        return {
            "stop_loss": round(max(sl, 0.0001), 4),
            "take_profit": round(max(tp, 0.0001), 4),
        }

    def _check_circuit_breakers(self) -> Optional[str]:
        state = self.state.get()
        equity = state.get("equity", 500.0)
        peak = state.get("peak_equity", equity)
        daily_start = state.get("daily_start_equity", equity)

        today = date.today().isoformat()
        last_check = state.get("last_daily_check_ts")
        if last_check != today:
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
            return f"Total drawdown {total_dd:.1f}% >= {total_dd_pct}%"
        if total_dd >= cb_dd_pct:
            self.state.save()
            return f"Circuit breaker: total drawdown {total_dd:.1f}% >= {cb_dd_pct}%"
        if daily_start > 0:
            daily_dd = (daily_start - equity) / daily_start * 100
            if daily_dd >= daily_dd_pct:
                self.state.save()
                return f"Daily drawdown {daily_dd:.1f}% >= {daily_dd_pct}%"

        self.state.save()
        return None

    def check_entry_allowed(self) -> tuple:
        if self.state.is_paused():
            return False, f"Paused: {self.state.get().get('pause_reason', 'unknown')}"
        breaker = self._check_circuit_breakers()
        if breaker:
            return False, breaker
        return True, "ok"
"""
PB-EMA Trend Strategy — dual-entry (pullback + breakout) within a PB-EMA regime.

Trend filter:
  - PB-EMA(50) on daily candles determines the regime (UP/DOWN/NEUTRAL)
  - Provided by main.py via MarketSnapshot.trend_regime
  - No funding conflict in entry logic — funding used for exits only

Entry types (both active within their respective regime):
  - Pullback: price retraces to the fast EMA within the ATR band
  - Breakout: price breaks above fast EMA with momentum (acceleration)
  - Longs only in UP regime, shorts only in DOWN regime, hold in NEUTRAL

Exit:
  - TP/SL enforced by main loop
  - Trend reversal (regime changes)
  - Extreme funding against position
  - Max hold duration
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from strategies.base import BaseStrategy, Signal, MarketSnapshot

log = logging.getLogger(__name__)


class PBEMATrendStrategy(BaseStrategy):
    """Two entry types within a PB-EMA regime filter. Funding for exits only."""

    def __init__(self, params: dict):
        super().__init__("pb_ema_trend", params)
        self._cached_atr = None

    def _compute_ema(self, prices: list, period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def _compute_atr(self, candles: list, period: int = 14) -> Optional[float]:
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(-period, 0):
            try:
                pc = candles[i].get("price", {})
                prev = candles[i - 1].get("price", {})
                high = float(pc.get("high", 0))
                low = float(pc.get("low", 0))
                close = pc.get("close")
                if close is None:
                    close = prev.get("close") or prev.get("previous")
                    if close is None:
                        continue
                prev_close = prev.get("close")
                if prev_close is None:
                    prev_close = prev.get("previous", 0)
                tr = max(high - low, abs(high - float(prev_close)), abs(low - float(prev_close)))
                trs.append(tr)
            except (TypeError, ValueError):
                continue
        if len(trs) < period:
            return None
        atr = sum(trs) / len(trs)
        self._cached_atr = atr
        return atr

    def _build_prices(self, candles: list) -> list:
        prices = []
        null_count = 0
        for i, c in enumerate(candles):
            try:
                pc = c.get("price", {})
                close = pc.get("close")
                if close is None:
                    if i > 0:
                        prev_close = candles[i - 1].get("price", {}).get("close")
                        if prev_close is not None:
                            prices.append(float(prev_close))
                            null_count += 1
                            continue
                    close = pc.get("previous")
                    if close is None:
                        null_count += 1
                        continue
                prices.append(float(close))
            except (TypeError, ValueError):
                null_count += 1
                continue
        if null_count > 0:
            log.warning("Forward-filled %d null closes in PB-EMA strategy", null_count)
        return prices

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        regime = snapshot.trend_regime
        candles = snapshot.candles_1h

        if regime == 'UNKNOWN':
            return Signal("hold", reason="PB-EMA regime not yet available")

        if len(candles) < 50:
            return Signal("hold", reason="Not enough candle data")

        prices = self._build_prices(candles)
        if len(prices) < 50:
            return Signal("hold", reason="Not enough price data after fill")

        # Live params (self-adapted)
        fast_period = int(snapshot.live_params.get("fast_ema", self.params.get("fast_ema_period", 12)))
        atr_period = int(self.params.get("atr_period", 14))

        # EMA
        fast_ema = self._compute_ema(prices, fast_period)
        if fast_ema is None:
            return Signal("hold", reason="EMA computation failed")

        # ATR with cache fallback
        atr = self._compute_atr(candles, atr_period)
        if atr is None or atr == 0:
            if self._cached_atr:
                atr = self._cached_atr
            else:
                return Signal("hold", reason="ATR unavailable")

        price = snapshot.current_price
        if price <= 0:
            return Signal("hold", reason="Invalid price")

        ema_dist_atr = (price - fast_ema) / atr if atr > 0 else 0.0
        has_position = snapshot.current_position is not None

        # Funding — exits only
        fund = snapshot.funding_rate or 0
        min_funding = snapshot.live_params.get("min_funding_bias", self.params.get("min_funding_bias", 0.0001))

        # Regime-based params
        pb_threshold = snapshot.live_params.get("pullback", self.params.get("pullback_threshold", 0.3))
        bo_threshold = snapshot.live_params.get("breakout", self.params.get("breakout_threshold", 0.4))

        if has_position:
            pos_side = snapshot.current_position.get("side", "")
            entry_price = float(snapshot.current_position.get("entry_price", price))
            max_hold = self.params.get("max_position_hours", 168)

            # Max hold
            entry_ts = snapshot.current_position.get("entry_ts", "")
            if entry_ts:
                try:
                    held = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_ts)).total_seconds() / 3600
                    if held > max_hold:
                        return Signal("exit", reason=f"Max hold {held:.0f}h exceeded")
                except (ValueError, TypeError):
                    pass

            # Exit on regime reversal
            if pos_side == "long" and regime != "UP":
                return Signal("exit", reason=f"Regime changed from UP to {regime}")
            if pos_side == "short" and regime != "DOWN":
                return Signal("exit", reason=f"Regime changed from DOWN to {regime}")

            # Exit on extreme funding against position
            if abs(fund) >= min_funding * 3:
                if pos_side == "long" and fund > 0:
                    return Signal("exit", reason=f"Funding strongly positive ({fund:.6f}) — longs crowded")
                if pos_side == "short" and fund < 0:
                    return Signal("exit", reason=f"Funding strongly negative ({fund:.6f}) — shorts crowded")

            return Signal("hold", reason=f"Holding {pos_side} in {regime}")

        # ── No position — look for entries ──────────────────────────────

        if regime == "NEUTRAL":
            return Signal("hold", reason="PB-EMA regime is NEUTRAL — no trades")

        if regime == "UP":
            lev = snapshot.live_params.get("leverage", self.params.get("max_leverage", 4.0))
            sl_mult = self.params.get("atr_multiplier_sl", 1.5)
            tp_mult = self.params.get("atr_multiplier_tp", 2.0)

            # Entry type 1: Pullback — price dipped to or below fast EMA
            if ema_dist_atr <= pb_threshold and ema_dist_atr >= -1.0:
                sl = price - (atr * sl_mult)
                tp = price + (atr * tp_mult)
                confidence = min(0.8, max(0.3, 1.0 - abs(ema_dist_atr)))
                return Signal(
                    "enter_long",
                    confidence=round(confidence, 2),
                    reason=f"Pullback {ema_dist_atr:.2f} ATRs from EMA{fast_period} in {regime}",
                    suggested_leverage=min(lev, 4.0),
                    suggested_stop_loss=round(sl, 4),
                    suggested_take_profit=round(tp, 4),
                )

            # Entry type 2: Breakout — price breaking above EMA with momentum
            if ema_dist_atr >= bo_threshold and ema_dist_atr <= 2.0:
                # Momentum check: price must be accelerating away from EMA
                # (current distance > average distance over last 6 bars)
                lookback_distances = []
                for j in range(1, min(7, len(prices) - fast_period)):
                    e = self._compute_ema(prices[:-(j+1)], fast_period)
                    if e:
                        lookback_distances.append(abs(prices[-(j+1)] - e) / max(atr, 0.001))
                avg_dist = sum(lookback_distances) / len(lookback_distances) if lookback_distances else 0

                if avg_dist > 0 and ema_dist_atr > avg_dist * 1.3:
                    sl = price - (atr * sl_mult)
                    tp = price + (atr * tp_mult * 1.25)  # wider TP for breakouts
                    return Signal(
                        "enter_long",
                        confidence=round(min(0.9, ema_dist_atr / 2.0), 2),
                        reason=f"Breakout {ema_dist_atr:.2f} ATRs (avg {avg_dist:.2f}) in {regime}",
                        suggested_leverage=min(lev, 4.0),
                        suggested_stop_loss=round(sl, 4),
                        suggested_take_profit=round(tp, 4),
                    )

        elif regime == "DOWN":
            lev = snapshot.live_params.get("leverage", self.params.get("max_leverage", 4.0))
            sl_mult = self.params.get("atr_multiplier_sl", 1.5)
            tp_mult = self.params.get("atr_multiplier_tp", 2.0)

            # Entry type 1: Pullback — price rallied to or above fast EMA
            if ema_dist_atr >= -pb_threshold and ema_dist_atr <= 1.0:
                sl = price + (atr * sl_mult)
                tp = price - (atr * tp_mult)
                confidence = min(0.8, max(0.3, 1.0 - abs(ema_dist_atr)))
                return Signal(
                    "enter_short",
                    confidence=round(confidence, 2),
                    reason=f"Pullback {ema_dist_atr:.2f} ATRs from EMA{fast_period} in {regime}",
                    suggested_leverage=min(lev, 4.0),
                    suggested_stop_loss=round(sl, 4),
                    suggested_take_profit=round(tp, 4),
                )

            # Entry type 2: Breakdown — price breaking below EMA with momentum
            if ema_dist_atr <= -bo_threshold and ema_dist_atr >= -2.0:
                lookback_distances = []
                for j in range(1, min(7, len(prices) - fast_period)):
                    e = self._compute_ema(prices[:-(j+1)], fast_period)
                    if e:
                        lookback_distances.append(abs(prices[-(j+1)] - e) / max(atr, 0.001))
                avg_dist = sum(lookback_distances) / len(lookback_distances) if lookback_distances else 0

                if avg_dist > 0 and abs(ema_dist_atr) > avg_dist * 1.3:
                    sl = price + (atr * sl_mult)
                    tp = price - (atr * tp_mult * 1.25)
                    return Signal(
                        "enter_short",
                        confidence=round(min(0.9, abs(ema_dist_atr) / 2.0), 2),
                        reason=f"Breakdown {ema_dist_atr:.2f} ATRs (avg {avg_dist:.2f}) in {regime}",
                        suggested_leverage=min(lev, 4.0),
                        suggested_stop_loss=round(sl, 4),
                        suggested_take_profit=round(tp, 4),
                    )

        return Signal("hold", reason=f"Entry conditions not met in {regime}")
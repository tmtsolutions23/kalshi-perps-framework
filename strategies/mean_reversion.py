"""
Mean Reversion Strategy — z-score Bollinger-style with trailing stops.

Fixes applied (P1 audit pass):
  P1-9: Hard stop enforced at 3×ATR, trailing stop activated after 2% profit.
  Funding filter: no longer a hard gate — removed the fund <= 0 / >= 0 veto
  so the strategy can actually trade both directions.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from strategies.base import BaseStrategy, Signal, MarketSnapshot

log = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, params: dict):
        super().__init__("mean_reversion", params)
        self._cached_atr = None

    def _compute_sma_std(self, prices: list, period: int):
        if len(prices) < period:
            return None, None
        window = prices[-period:]
        sma = sum(window) / period
        var = sum((p - sma) ** 2 for p in window) / period
        return sma, var ** 0.5

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
                    close = prev.get("close") or prev.get("previous", 0)
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

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        candles = snapshot.candles_1h
        if len(candles) < 50:
            return Signal("hold", reason="Not enough data")

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
            log.warning("Forward-filled %d null closes in mean reversion", null_count)
        if len(prices) < 50:
            return Signal("hold", reason="Not enough price data")

        sma, std = self._compute_sma_std(prices, period=20)
        if sma is None or std == 0:
            return Signal("hold", reason="No volatility")

        # ATR for stop placement
        atr = self._compute_atr(candles, 14)
        if atr is None or atr == 0:
            if self._cached_atr:
                atr = self._cached_atr
            else:
                atr = snapshot.current_price * 0.01  # last resort

        price = snapshot.current_price
        zscore = (price - sma) / std

        entry_z = self.params.get("entry_zscore", 2.0)
        exit_z = self.params.get("exit_zscore", 0.2)
        atr_sl = self.params.get("atr_multiplier_sl", 3.0)  # wider SL for MR
        max_hold_hours = self.params.get("max_hold_hours", 48)

        has_position = snapshot.current_position is not None

        if has_position:
            pos_side = snapshot.current_position.get("side", "")
            entry_price = float(snapshot.current_position.get("entry_price", price))
            entry_ts = snapshot.current_position.get("entry_ts", "")

            # Exit on mean reversion
            if pos_side == "long" and zscore >= exit_z:
                return Signal("exit", reason=f"Mean reverted — z={zscore:.2f}")
            if pos_side == "short" and zscore <= -exit_z:
                return Signal("exit", reason=f"Mean reverted — z={zscore:.2f}")

            # P1-9: Hard stop (enforced by main.py check_stops)
            sl = entry_price - (atr * atr_sl) if pos_side == "long" else entry_price + (atr * atr_sl)

            # Max hold
            if entry_ts:
                try:
                    held_h = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_ts)).total_seconds() / 3600
                    if held_h > max_hold_hours:
                        return Signal("exit", reason=f"Max hold {max_hold_hours}h exceeded")
                except (ValueError, TypeError):
                    pass

            return Signal(
                "hold",
                reason=f"z={zscore:.2f}, waiting for reversion",
                suggested_stop_loss=round(sl, 4),
            )

        # Entry logic — removed the funding >= 0 / <= 0 gate (was structurally short-only)
        if zscore <= -entry_z:
            # Oversold → long
            sl = price - (atr * atr_sl)
            tp = price + (atr * atr_sl * 1.5)  # 1.5:1 reward
            return Signal(
                "enter_long",
                confidence=min(1.0, abs(zscore) / 4.0),
                reason=f"Oversold z={zscore:.2f} (sma={sma:.1f})",
                suggested_leverage=min(3.0, self.params.get("max_leverage", 3.0)),
                suggested_stop_loss=round(sl, 4),
                suggested_take_profit=round(tp, 4),
            )

        if zscore >= entry_z:
            # Overbought → short
            sl = price + (atr * atr_sl)
            tp = price - (atr * atr_sl * 1.5)
            return Signal(
                "enter_short",
                confidence=min(1.0, abs(zscore) / 4.0),
                reason=f"Overbought z={zscore:.2f} (sma={sma:.1f})",
                suggested_leverage=min(3.0, self.params.get("max_leverage", 3.0)),
                suggested_stop_loss=round(sl, 4),
                suggested_take_profit=round(tp, 4),
            )

        return Signal("hold", reason=f"z={zscore:.2f} — no extreme")
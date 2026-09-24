"""
Funding Momentum Strategy — the primary BTC perps strategy.

Concept:
  - Use funding rate as a sentiment filter (don't fight the funding)
  - Enter on pullbacks to fast EMA when the trend (slow EMA) agrees with funding bias
  - Exit at TP/SL or when the trend reverses

Self-adaptation:
  - After every N trades, evaluate win rate and volatility conditions
  - Tighten/loosen entry thresholds, ATR multipliers, and leverage
  - All adapted params stored in state so they persist across restarts
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from strategies.base import BaseStrategy, Signal, MarketSnapshot

log = logging.getLogger(__name__)


class FundingMomentumStrategy(BaseStrategy):
    """
    Combines EMA trend direction with funding rate sentiment.

    Entry logic:
      1. If fast EMA > slow EMA → uptrend (bias long unless funding is extremely negative)
      2. If fast EMA < slow EMA → downtrend (bias short unless funding is extremely positive)
      3. Funding rate filter: if |funding| > min_funding_bias, override trend bias
         (positive funding = longs crowded → bias short, negative → bias long)
      4. Entry: price must pull back toward the fast EMA (within pullback_threshold % of ATR)
      5. Confidence scales with how far price is from EMA and trend strength
    """

    def __init__(self, params: dict):
        super().__init__("funding_momentum", params)

    def _compute_ema(self, prices: list, period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def _compute_atr(self, candles: list, period: int = 14) -> Optional[float]:
        """Compute ATR from 1h candles."""
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(-period, 0):
            try:
                high = float(candles[i].get("ask", {}).get("high", 0))
                low = float(candles[i].get("bid", {}).get("low", 0))
                prev_close = float(candles[i - 1].get("price", {}).get("close", 0))
            except (TypeError, IndexError):
                continue
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
            trs.append(tr)
        if not trs:
            return None
        return sum(trs) / len(trs)

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        candles = snapshot.candles_1h
        if len(candles) < 50:  # need enough data
            return Signal("hold", reason="Not enough candle data yet")

        # Extract close prices
        prices = []
        for c in candles:
            try:
                prices.append(float(c.get("price", {}).get("close", 0)))
            except (TypeError, ValueError):
                continue
        if len(prices) < 50:
            return Signal("hold", reason="Not enough price data")

        # Live params from self-adaptation (fallback to static)
        fast_period = int(snapshot.live_params.get("fast_ema", self.params.get("fast_ema_period", 12)))
        slow_period = int(snapshot.live_params.get("slow_ema", self.params.get("slow_ema_period", 48)))
        pullback_threshold = snapshot.live_params.get("pullback", self.params.get("pullback_threshold", 0.3))
        min_funding = snapshot.live_params.get("min_funding_bias", self.params.get("min_funding_bias", 0.0001))

        # Compute EMAs
        fast_ema = self._compute_ema(prices, fast_period)
        slow_ema = self._compute_ema(prices, slow_period)
        if fast_ema is None or slow_ema is None:
            return Signal("hold", reason="EMA computation failed")

        atr = self._compute_atr(candles, int(self.params.get("atr_period", 14)))
        if atr is None or atr == 0:
            atr = snapshot.current_price * 0.02  # fallback: 2% of price

        price = snapshot.current_price
        if price <= 0:
            return Signal("hold", reason="Invalid price")

        # Trend direction
        uptrend = fast_ema > slow_ema
        trend_strength = abs(fast_ema - slow_ema) / price * 100  # as % of price

        # Funding bias
        fund = snapshot.funding_rate or 0
        funding_bias = None
        if abs(fund) >= min_funding:
            funding_bias = "short" if fund > 0 else "long"  # positive fund = short bias

        # Distance from fast EMA (in ATR units)
        ema_distance_atr = (price - fast_ema) / atr if atr > 0 else 0.0

        # Determine bias
        trend_bias = "long" if uptrend else "short"
        bias = funding_bias if funding_bias else trend_bias

        # Entry conditions
        has_position = snapshot.current_position is not None

        if not has_position:
            # We want to enter
            if bias == "long" and uptrend:
                # Entering long: price pulled back to within pullback*ATR of fast EMA
                if abs(ema_distance_atr) <= pullback_threshold:
                    # Compute stop and take profit
                    sl_price = price - (atr * self.params.get("atr_multiplier_sl", 1.5))
                    tp_price = price + (atr * self.params.get("atr_multiplier_tp", 1.5))

                    confidence = min(1.0, trend_strength / 2.0) * (0.5 if funding_bias else 1.0)
                    lev = snapshot.live_params.get("leverage", self.params.get("max_leverage", 4.0))

                    return Signal(
                        "enter_long",
                        confidence=round(confidence, 2),
                        reason=(
                            f"Uptrend (EMA{fast_period}:{fast_ema:.0f} > EMA{slow_period}:{slow_ema:.0f}), "
                            f"price at {price:.0f} pulled back {ema_distance_atr:.1f} ATRs, "
                            f"funding {'positive' if fund > 0 else 'negative'}"
                            + (f" ({fund:.6f})" if funding_bias else "")
                        ),
                        suggested_leverage=min(lev, 4.0),
                        suggested_stop_loss=round(sl_price, 1),
                        suggested_take_profit=round(tp_price, 1),
                    )

            elif bias == "short" and not uptrend:
                # Entering short: price pulled back UP to within pullback*ATR of fast EMA
                if abs(ema_distance_atr) <= pullback_threshold:
                    sl_price = price + (atr * self.params.get("atr_multiplier_sl", 1.5))
                    tp_price = price - (atr * self.params.get("atr_multiplier_tp", 1.5))

                    confidence = min(1.0, trend_strength / 2.0) * (0.5 if funding_bias else 1.0)
                    lev = snapshot.live_params.get("leverage", self.params.get("max_leverage", 4.0))

                    return Signal(
                        "enter_short",
                        confidence=round(confidence, 2),
                        reason=(
                            f"Downtrend (EMA{fast_period}:{fast_ema:.0f} < EMA{slow_period}:{slow_ema:.0f}), "
                            f"price at {price:.0f} pulled back {ema_distance_atr:.1f} ATRs, "
                            f"funding {'positive' if fund > 0 else 'negative'}"
                            + (f" ({fund:.6f})" if funding_bias else "")
                        ),
                        suggested_leverage=min(lev, 4.0),
                        suggested_stop_loss=round(sl_price, 1),
                        suggested_take_profit=round(tp_price, 1),
                    )

        else:
            # We have an open position — check exit conditions
            pos_side = snapshot.current_position.get("side", "")
            entry_price = float(snapshot.current_position.get("entry_price", price))
            max_hold = self.params.get("max_position_hours", 168)

            # Check hold duration
            entry_ts = snapshot.current_position.get("entry_ts", "")
            if entry_ts:
                try:
                    held = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_ts)).total_seconds() / 3600
                    if held > max_hold:
                        return Signal("exit", reason=f"Max hold time exceeded ({held:.0f}h > {max_hold}h)")
                except (ValueError, TypeError):
                    pass

            # Exit if trend reversed against us
            if pos_side == "long" and not uptrend:
                return Signal("exit", reason=f"Trend reversed to downtrend — EMA{fast_period} crossed below EMA{slow_period}")
            if pos_side == "short" and uptrend:
                return Signal("exit", reason=f"Trend reversed to uptrend — EMA{fast_period} crossed above EMA{slow_period}")

            # Exit if funding flipped hard against us
            if pos_side == "long" and funding_bias == "short" and abs(fund) > min_funding * 5:
                return Signal("exit", reason=f"Funding strongly negative ({fund:.6f}) — crowded longs")
            if pos_side == "short" and funding_bias == "long" and abs(fund) > min_funding * 5:
                return Signal("exit", reason=f"Funding strongly positive ({fund:.6f}) — crowded shorts")

            return Signal("hold", reason=f"Holding {pos_side} — trend intact, funding neutral")

        return Signal("hold", reason="No conditions met")
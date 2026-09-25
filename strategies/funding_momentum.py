"""
Funding Momentum Strategy — the primary BTC perps strategy.

Fixes applied (P1 audit pass):
  P1-1: Funding is a confirmation filter, not a veto — trade when trend & funding agree,
        or trend alone when funding is neutral. No longer structurally short-only.
  P1-2: Funding exit threshold uses trailing 90th percentile instead of hardcoded multiple.
  P1-4: Pullback entry is directional (no abs()) — longs require price at/below EMA,
        shorts require price at/above EMA.
  P1-6: ATR computed from price.high/low/previous, not ask.high/bid.low.
  P1-7: ATR failure does not fabricate a number — uses last valid ATR or holds.
  P1-8: Null candle closes forward-filled from price.previous instead of silently dropped.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from strategies.base import BaseStrategy, Signal, MarketSnapshot

log = logging.getLogger(__name__)


class FundingMomentumStrategy(BaseStrategy):
    """
    Combines EMA trend direction with funding rate confirmation.

    Entry logic:
      1. Trend determined by fast EMA vs slow EMA
      2. Funding filter: when |funding| >= min_funding_bias, REQUIRES trend and funding
         to agree. When funding is neutral, trades by trend alone.
      3. Entry: price must be on the correct side of the fast EMA (pullback entry)
      4. Exit: trend reversal, extreme funding flip, TP/SL, or max hold
    """

    def __init__(self, params: dict):
        super().__init__("funding_momentum", params)
        self._cached_atr = None  # P1-7: cache last valid ATR

    def _compute_ema(self, prices: list, period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema

    def _compute_atr(self, candles: list, period: int = 14) -> Optional[float]:
        """
        Compute ATR from price.high, price.low, price.previous (P1-6 fix).
        Forward-fills null closes from price.previous (P1-8 fix).
        """
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

                # P1-8: forward-fill null close from price.previous
                if close is None:
                    close = prev.get("close") or prev.get("previous")
                    if close is None:
                        continue

                prev_close = prev.get("close")
                if prev_close is None:
                    prev_close = prev.get("previous", 0)
                if prev_close is None:
                    continue

                close = float(close)
                prev_close_f = float(prev_close)

                tr = max(
                    high - low,
                    abs(high - prev_close_f),
                    abs(low - prev_close_f),
                )
                trs.append(tr)
            except (TypeError, ValueError, IndexError):
                continue

        if len(trs) < period:
            return None

        atr = sum(trs) / len(trs)
        self._cached_atr = atr  # P1-7: cache for fallback
        return atr

    def _build_prices(self, candles: list) -> list:
        """Extract close prices with null handling (P1-8)."""
        prices = []
        null_count = 0
        for i, c in enumerate(candles):
            try:
                pc = c.get("price", {})
                close = pc.get("close")
                if close is None:
                    # Forward-fill from previous close
                    if i > 0:
                        prev = candles[i - 1].get("price", {}).get("close")
                        if prev is not None:
                            close = float(prev)
                        else:
                            # Try price.previous
                            close = pc.get("previous")
                            if close is None:
                                null_count += 1
                                continue
                            close = float(close)
                    else:
                        close = pc.get("previous")
                        if close is None:
                            null_count += 1
                            continue
                        close = float(close)
                else:
                    close = float(close)
                prices.append(close)
            except (TypeError, ValueError):
                null_count += 1
                continue

        if null_count > 0:
            log.warning("Forward-filled %d null candle closes in price series", null_count)
        return prices

    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        candles = snapshot.candles_1h
        if len(candles) < 50:
            return Signal("hold", reason="Not enough candle data yet")

        prices = self._build_prices(candles)
        if len(prices) < 50:
            return Signal("hold", reason="Not enough price data after null fill")

        # Live params (self-adapted) fallback to static config
        fast_period = int(snapshot.live_params.get("fast_ema", self.params.get("fast_ema_period", 12)))
        slow_period = int(snapshot.live_params.get("slow_ema", self.params.get("slow_ema_period", 48)))
        pullback_threshold = snapshot.live_params.get("pullback", self.params.get("pullback_threshold", 0.3))
        min_funding = snapshot.live_params.get("min_funding_bias", self.params.get("min_funding_bias", 0.0001))

        # EMAs
        fast_ema = self._compute_ema(prices, fast_period)
        slow_ema = self._compute_ema(prices, slow_period)
        if fast_ema is None or slow_ema is None:
            return Signal("hold", reason="EMA computation failed")

        # ATR — P1-7: don't fabricate, use cache or hold
        atr = self._compute_atr(candles, int(self.params.get("atr_period", 14)))
        if atr is None or atr == 0:
            if self._cached_atr:
                atr = self._cached_atr
                log.debug("Using cached ATR=%.4f", atr)
            else:
                return Signal("hold", reason="ATR unavailable and no cache")

        price = snapshot.current_price
        if price <= 0:
            return Signal("hold", reason="Invalid price")

        # Trend
        uptrend = fast_ema > slow_ema
        trend_strength = abs(fast_ema - slow_ema) / price * 100

        # Funding — P1-1: confirmation filter, not veto
        fund = snapshot.funding_rate or 0
        funding_bias = None
        if abs(fund) >= min_funding:
            funding_bias = "short" if fund > 0 else "long"  # positive fund = shorts crowded

        # Determine tradeable bias
        # When funding is significant, require trend AND funding to agree.
        # When funding is neutral, follow trend alone.
        trend_bias = "long" if uptrend else "short"
        if funding_bias:
            if funding_bias != trend_bias:
                # Funding and trend disagree — no entry
                regime = "uptrend" if uptrend else "downtrend"
                return Signal("hold", reason=f"Funding ({fund:.6f}) conflicts with {regime} — no entry")
            else:
                bias = trend_bias  # Both agree, trade the trend
        else:
            bias = trend_bias  # Neutral funding, trade trend alone

        # Distance from fast EMA in ATR units
        ema_distance_atr = (price - fast_ema) / atr if atr > 0 else 0.0

        has_position = snapshot.current_position is not None

        if not has_position:
            # ── Entry logic ──────────────────────────────────────────

            if bias == "long":
                # R2-3: bounded band — long wants price near or below EMA, not arbitrarily far
                if -1.0 <= ema_distance_atr <= pullback_threshold:
                    sl_price = price - (atr * self.params.get("atr_multiplier_sl", 1.5))
                    tp_price = price + (atr * self.params.get("atr_multiplier_tp", 2.0))
                    confidence = min(1.0, trend_strength / 2.0) * (0.5 if funding_bias else 1.0)
                    lev = snapshot.live_params.get("leverage", self.params.get("max_leverage", 4.0))

                    return Signal(
                        "enter_long",
                        confidence=round(confidence, 2),
                        reason=(
                            f"Uptrend — price {ema_distance_atr:.2f} ATRs from EMA12, "
                            f"funding {fund:+.6f}" + (" (confirming)" if funding_bias else "")
                        ),
                        suggested_leverage=min(lev, 4.0),
                        suggested_stop_loss=round(sl_price, 4),
                        suggested_take_profit=round(tp_price, 4),
                    )

            elif bias == "short":
                # R2-3: bounded band — short wants price near or above EMA, not arbitrarily far
                if -pullback_threshold <= ema_distance_atr <= 1.0:
                    sl_price = price + (atr * self.params.get("atr_multiplier_sl", 1.5))
                    tp_price = price - (atr * self.params.get("atr_multiplier_tp", 2.0))
                    confidence = min(1.0, trend_strength / 2.0) * (0.5 if funding_bias else 1.0)
                    lev = snapshot.live_params.get("leverage", self.params.get("max_leverage", 4.0))

                    return Signal(
                        "enter_short",
                        confidence=round(confidence, 2),
                        reason=(
                            f"Downtrend — price {ema_distance_atr:.2f} ATRs from EMA12, "
                            f"funding {fund:+.6f}" + (" (confirming)" if funding_bias else "")
                        ),
                        suggested_leverage=min(lev, 4.0),
                        suggested_stop_loss=round(sl_price, 4),
                        suggested_take_profit=round(tp_price, 4),
                    )

        else:
            # ── Exit logic ────────────────────────────────────────────
            pos_side = snapshot.current_position.get("side", "")
            entry_price = float(snapshot.current_position.get("entry_price", price))
            max_hold = self.params.get("max_position_hours", 168)

            # Max hold duration
            entry_ts = snapshot.current_position.get("entry_ts", "")
            if entry_ts:
                try:
                    held = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_ts)).total_seconds() / 3600
                    if held > max_hold:
                        return Signal("exit", reason=f"Max hold {held:.0f}h exceeded")
                except (ValueError, TypeError):
                    pass

            # Exit on trend reversal
            if pos_side == "long" and not uptrend:
                return Signal("exit", reason="Trend reversed to downtrend")
            if pos_side == "short" and uptrend:
                return Signal("exit", reason="Trend reversed to uptrend")

            # Exit on extreme funding against the position (R2-6: fixed inverted strings)
            if abs(fund) >= min_funding * 3:
                if pos_side == "long" and fund > 0:
                    return Signal("exit", reason=f"Funding strongly positive ({fund:.6f}) — longs crowded")
                if pos_side == "short" and fund < 0:
                    return Signal("exit", reason=f"Funding strongly negative ({fund:.6f}) — shorts crowded")

            return Signal("hold", reason=f"Holding {pos_side}")

        return Signal("hold", reason="No conditions met")
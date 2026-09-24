"""
Mean Reversion Strategy — a second strategy to demonstrate the plug-and-play design.

Concept:
  - Trade against short-term overextension: buy when price drops far below the
    mean (Bollinger-style bands), sell when it spikes above.
  - Funding filter: prefer longs when funding is negative (shorts crowded),
    prefer shorts when funding is positive (longs crowded).

This strategy exists to show that swapping strategies is a one-line config change:
    strategy.name: mean_reversion
"""

import logging

from strategies.base import BaseStrategy, Signal, MarketSnapshot

log = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, params: dict):
        super().__init__("mean_reversion", params)

    def _compute_sma_std(self, prices: list, period: int):
        """Return (sma, std) of last `period` prices."""
        if len(prices) < period:
            return None, None
        window = prices[-period:]
        sma = sum(window) / period
        var = sum((p - sma) ** 2 for p in window) / period
        return sma, var ** 0.5

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
            return Signal("hold", reason="No volatility to revert")

        price = snapshot.current_price
        zscore = (price - sma) / std

        # Funding filter
        fund = snapshot.funding_rate or 0
        entry_z = self.params.get("entry_zscore", 2.0)
        exit_z = self.params.get("exit_zscore", 0.2)
        max_hold_hours = self.params.get("max_hold_hours", 48)
        from datetime import datetime, timezone

        has_position = snapshot.current_position is not None

        if has_position:
            pos_side = snapshot.current_position.get("side", "")
            entry_price = float(snapshot.current_position.get("entry_price", price))
            entry_ts = snapshot.current_position.get("entry_ts", "")

            # Exit when reverting to mean
            if pos_side == "long" and zscore >= exit_z:
                return Signal("exit", reason=f"Reverted to mean — z={zscore:.2f}")
            if pos_side == "short" and zscore <= -exit_z:
                return Signal("exit", reason=f"Reverted to mean — z={zscore:.2f}")

            # Max hold
            if entry_ts:
                try:
                    held_h = (datetime.now(timezone.utc) - datetime.fromisoformat(entry_ts)).total_seconds() / 3600
                    if held_h > max_hold_hours:
                        return Signal("exit", reason=f"Max hold {max_hold_hours}h exceeded")
                except (ValueError, TypeError):
                    pass

            return Signal("hold", reason=f"Waiting for mean reversion — z={zscore:.2f}")

        # Entry logic — buy oversold, sell overbought (with funding bias preference)
        if zscore <= -entry_z:
            # Prefer longs when funding is negative (shorts crowded) or neutral
            if fund <= 0:
                return Signal(
                    "enter_long",
                    confidence=min(1.0, abs(zscore) / 4.0),
                    reason=f"Oversold — z={zscore:.2f} below {sma:.1f}, funding {fund:.6f}",
                    suggested_leverage=min(4.0, self.params.get("max_leverage", 3.0)),
                )
        elif zscore >= entry_z:
            # Prefer shorts when funding is positive (longs crowded) or neutral
            if fund >= 0:
                return Signal(
                    "enter_short",
                    confidence=min(1.0, abs(zscore) / 4.0),
                    reason=f"Overbought — z={zscore:.2f} above {sma:.1f}, funding {fund:.6f}",
                    suggested_leverage=min(4.0, self.params.get("max_leverage", 3.0)),
                )

        return Signal("hold", reason=f"z={zscore:.2f} — no extreme")
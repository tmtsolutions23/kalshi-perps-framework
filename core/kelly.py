"""
Kelly sizing calculator — grows bet size with account performance.
"""
import math
import logging
from typing import Optional

log = logging.getLogger(__name__)


def half_kelly(win_rate: float, payoff_ratio: float, max_fraction: float = 0.25,
                min_fraction: float = 0.005) -> float:
    """
    Compute half-Kelly fraction of equity to risk per trade.
    Kelly = win_rate - (1 - win_rate) / payoff_ratio
    Half-Kelly = Kelly / 2, clamped to [min_fraction, max_fraction].
    """
    if win_rate <= 0 or payoff_ratio <= 0:
        return min(max_fraction, 0.01)
    if win_rate >= 1.0:
        return min(max_fraction, 0.08)

    kelly = win_rate - ((1 - win_rate) / payoff_ratio)
    if kelly <= 0:
        return min_fraction

    half = kelly / 2.0
    return max(min_fraction, min(half, max_fraction))


def compute_from_metrics(metrics: dict, config: dict = None) -> Optional[float]:
    """Compute half-Kelly fraction from a metrics dict. Returns risk_per_trade_pct or None."""
    n = metrics.get("total_trades", 0)
    if n < 10:
        return None

    win_rate = metrics.get("win_rate", 0)
    payoff = metrics.get("payoff_ratio", 0)
    if payoff == float("inf") or payoff == 0:
        return None

    cfg = config or {}
    max_fraction = cfg.get("kelly_fraction", 0.25)
    min_fraction = cfg.get("min_risk_pct", 0.005)
    return half_kelly(win_rate, payoff, max_fraction, min_fraction)
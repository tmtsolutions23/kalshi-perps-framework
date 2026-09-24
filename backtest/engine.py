"""
Performance tracker — compute win rate, Sharpe, max drawdown from trade history.
"""

import json
import logging
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)


class PerformanceTracker:
    """Compute and report strategy performance metrics from trade history."""

    def __init__(self, trade_log_path: str):
        self.path = Path(trade_log_path)

    def load_trades(self) -> list:
        if not self.path.exists():
            return []
        trades = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return trades

    def metrics(self, trades: Optional[list] = None) -> dict:
        if trades is None:
            trades = self.load_trades()
        if not trades:
            return {"total_trades": 0}

        n = len(trades)
        wins = [t for t in trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl", 0) < 0]
        win_rate = len(wins) / n if n > 0 else 0

        pnls = [t.get("net_pnl", 0) for t in trades]
        total_pnl = sum(pnls)
        avg_win = sum(t.get("net_pnl", 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.get("net_pnl", 0) for t in losses) / len(losses) if losses else 0
        profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

        # Simple Sharpe (assuming risk-free = 0)
        mean_pnl = sum(pnls) / n
        var_pnl = sum((p - mean_pnl) ** 2 for p in pnls) / n
        sharpe = mean_pnl / math.sqrt(var_pnl) if var_pnl > 0 else 0

        # Max drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return {
            "total_trades": n,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(max_dd, 2),
            "wins": len(wins),
            "losses": len(losses),
        }
"""
Performance metrics — correct definitions per quantitative-systems.md.

Metrics:
  - Profit factor: Σwins / |Σlosses|
  - Sharpe: mean(r) / σ(r) × √(n), on returns, sample stdev (n-1)
  - Sortino: mean(r) / σ⁻(r) × √(n)
  - Calmar: total_return% / max_drawdown%
  - Max drawdown: peak-to-trough as % of equity curve
  - Expectancy: (win% × avg_win) − (loss% × avg_loss), net of all costs
  - Buy-and-hold BTC benchmark against identical window
"""

import math
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

SEED_EQUITY = 10000.0


class PerformanceTracker:
    """Compute metrics from state trade_history and equity. Tracks BTC benchmark."""

    def __init__(self, state_manager):
        self.state = state_manager
        self._btc_start_price = None

    def set_btc_start_price(self, price: float):
        if self._btc_start_price is None:
            self._btc_start_price = price

    def metrics(self, btc_current_price: Optional[float] = None) -> dict:
        state = self.state.get()
        trades = state.get("trade_history", [])
        equity = state.get("equity", SEED_EQUITY)

        if not trades:
            return {
                "total_trades": 0, "status": "no_trades_yet",
                "equity": round(equity, 2),
                "return_pct": round((equity - SEED_EQUITY) / SEED_EQUITY * 100, 2),
            }

        n = len(trades)
        wins = [t for t in trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl", 0) < 0]
        win_rate = len(wins) / n if n > 0 else 0

        sum_wins = sum(t.get("net_pnl", 0) for t in wins)
        sum_losses = abs(sum(t.get("net_pnl", 0) for t in losses))
        profit_factor = sum_wins / max(sum_losses, 0.01)

        avg_win = sum_wins / len(wins) if wins else 0
        avg_loss = sum_losses / len(losses) if losses else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
        payoff_ratio = abs(avg_win / max(avg_loss, 0.01)) if avg_loss != 0 else float("inf")

        # Build equity curve from state.equity_timeline (includes funding, R2-8)
        sorted_by_ts = sorted(
            [t for t in trades if t.get("exit_ts") and t.get("net_pnl") is not None],
            key=lambda t: t.get("exit_ts", ""),
        )
        timeline = state.get("equity_timeline", [])
        if timeline:
            equity_curve = [SEED_EQUITY]
            for pt in timeline:
                equity_curve.append(pt["equity"])
            timestamps = []
            for pt in timeline:
                try:
                    timestamps.append(datetime.fromisoformat(pt["ts"]))
                except (ValueError, TypeError):
                    timestamps.append(None)
        else:
            # Fallback: reconstruct from trade PnL only (excludes funding)
            equity_curve = [SEED_EQUITY]
            timestamps = []
            for t in sorted_by_ts:
                equity_curve.append(equity_curve[-1] + t["net_pnl"])
                try:
                    timestamps.append(datetime.fromisoformat(t.get("exit_ts", "")))
                except (ValueError, TypeError):
                    timestamps.append(None)

        # Returns from equity curve (per-trade returns)
        returns = []
        for i in range(1, len(equity_curve)):
            prev = equity_curve[i - 1]
            if prev > 0:
                returns.append((equity_curve[i] - prev) / prev)

        # Sharpe (R2-7): annualized by elapsed time, not sqrt(total trades)
        if len(returns) >= 2:
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)

            # Compute elapsed days from trade timestamps
            elapsed_days = 0
            if timestamps and timestamps[0] and timestamps[-1]:
                elapsed_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400
            periods_per_year = max((n / max(elapsed_days, 1) * 365.25) if elapsed_days > 0 else math.sqrt(n), 0.01)

            sharpe = (mean_r / math.sqrt(var_r)) * math.sqrt(periods_per_year) if var_r > 0 else 0
        else:
            sharpe = 0

        # Sortino (same time-based annualization)
        if len(returns) >= 2:
            downside = [r for r in returns if r < 0]
            if downside:
                dd_var = sum(r ** 2 for r in downside) / (len(downside) - 1) if len(downside) > 1 else sum(r ** 2 for r in downside) / max(len(downside), 1)
                sortino = (mean_r / math.sqrt(dd_var)) * math.sqrt(periods_per_year) if dd_var > 0 else 0
            else:
                sortino = float("inf") if len(returns) > 0 else 0
        else:
            sortino = 0

        # Max drawdown as % (P2-3)
        peak = SEED_EQUITY
        max_dd_pct = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd_pct = (peak - eq) / peak * 100 if peak > 0 else 0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        total_return_pct = (equity - SEED_EQUITY) / SEED_EQUITY * 100
        calmar = total_return_pct / max_dd_pct if max_dd_pct > 0 else float("inf")

        # Holding period
        hold_times = []
        for t in trades:
            try:
                entry = datetime.fromisoformat(t.get("entry_ts", ""))
                ext = datetime.fromisoformat(t.get("exit_ts", ""))
                hold_times.append((ext - entry).total_seconds() / 3600)
            except (ValueError, TypeError):
                pass
        avg_hold_h = sum(hold_times) / len(hold_times) if hold_times else 0

        # Long/short split
        long_pnl = sum(t.get("net_pnl", 0) for t in trades if t.get("side") == "long")
        short_pnl = sum(t.get("net_pnl", 0) for t in trades if t.get("side") == "short")

        worst = min(trades, key=lambda t: t.get("net_pnl", 0))
        best = max(trades, key=lambda t: t.get("net_pnl", 0))

        # Buy-and-hold benchmark (P2-5)
        bh_return = 0.0
        if self._btc_start_price and btc_current_price and btc_current_price > 0:
            bh_return = (btc_current_price - self._btc_start_price) / self._btc_start_price * 100

        return {
            "total_trades": n,
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(win_rate, 3),
            "profit_factor": round(profit_factor, 2),
            "payoff_ratio": round(payoff_ratio, 2),
            "expectancy": round(expectancy, 2),
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "calmar": round(calmar, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "total_return_pct": round(total_return_pct, 2),
            "avg_hold_hours": round(avg_hold_h, 1),
            "long_pnl": round(long_pnl, 2),
            "short_pnl": round(short_pnl, 2),
            "worst_trade": round(worst.get("net_pnl", 0), 2),
            "best_trade": round(best.get("net_pnl", 0), 2),
            "benchmark_bh_return_pct": round(bh_return, 2),
            "equity": round(equity, 2),
            "status": "active",
        }

    def summary_text(self, btc_current_price: Optional[float] = None) -> Optional[str]:
        """Return a Discord-formatted summary or None if no trades yet."""
        m = self.metrics(btc_current_price)
        if m.get("status") == "no_trades_yet":
            return None

        emoji = "🟢" if m["profit_factor"] > 1.0 and m["max_drawdown_pct"] < 15 else "🟡" if m["profit_factor"] > 0 else "🔴"

        lines = [
            f"{emoji} **Perf Summary** — {m['total_trades']} trades",
            f"  PnL={m['total_return_pct']:+.1f}%  Win={m['win_rate']:.0%}  PF={m['profit_factor']:.2f}  Expectancy=${m['expectancy']:+.2f}",
            f"  Sharpe={m['sharpe']:.2f}  Calmar={m['calmar']:.1f}  MaxDD={m['max_drawdown_pct']:.1f}%",
            f"  Long=${m['long_pnl']:+.2f}  Short=${m['short_pnl']:+.2f}  AvgHold={m['avg_hold_hours']:.1f}h",
        ]
        if m.get("benchmark_bh_return_pct") != 0:
            vs = m["total_return_pct"] - m["benchmark_bh_return_pct"]
            lines.append(f"  BTC buy-hold: {m['benchmark_bh_return_pct']:+.1f}%  vs strat: {vs:+.1f}%")
        return "\n".join(lines)
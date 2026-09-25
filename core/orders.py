"""
Order simulation — paper mode with realistic fill mechanics.
Tracks equity via state, uses bid/ask + slippage, persists stop levels.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class PaperOrderManager:
    """
    Simulates orders against live bid/ask prices with configurable slippage.
    Equity is tracked in state — losses shrink it, wins grow it.
    Stop-loss / take-profit levels are persisted on the position.
    """

    def __init__(self, auth, market_data, state_manager):
        self.auth = auth
        self.market = market_data
        self.state = state_manager

    def place_order(
        self,
        ticker: str,
        side: str,  # "bid" for long, "ask" for short
        count: int,
        limit_price: float,
        bid: float,
        ask: float,
        slippage_bps: int = 5,
    ) -> Optional[dict]:
        """
        Paper-mode order placement with realistic fill simulation.
        Long fills at ask + slippage; short fills at bid - slippage.
        Returns simulated {order_id, fill_price, fill_count, position} or None.
        """
        if side == "bid":
            fill_price = ask * (1 + slippage_bps / 10000)
            if limit_price and fill_price > limit_price:
                log.info("PAPER: limit $%.4f below fill $%.4f — no fill", limit_price, fill_price)
                return None
        else:
            fill_price = bid * (1 - slippage_bps / 10000)
            if limit_price and fill_price < limit_price:
                log.info("PAPER: limit $%.4f above fill $%.4f — no fill", limit_price, fill_price)
                return None

        order_id = f"paper_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        direction = "long" if side == "bid" else "short"
        fees = round(count * fill_price * 0.0005, 2)

        pos = {
            "ticker": ticker,
            "side": direction,
            "entry_price": fill_price,
            "size": count,
            "entry_ts": datetime.now(timezone.utc).isoformat(),
            "entry_order_id": order_id,
            "entry_notional": round(count * fill_price, 2),
            "fees_paid": fees,
            "unrealized_pnl": 0.0,
            "stop_loss_price": None,
            "take_profit_price": None,
            "trail_activate_price": None,
            "trail_bps": None,
            "trail_watermark": None,
        }

        self.state.update(current_position=pos, pending_order=None)

        log.info(
            "PAPER FILL: %s %d @ $%.4f (slip %dbps) — notional $%.0f",
            direction.upper(), count, fill_price, slippage_bps, pos["entry_notional"],
        )
        return {"order_id": order_id, "fill_price": fill_price, "fill_count": count, "position": pos}

    def set_stops(self, stop_loss: float = None, take_profit: float = None,
                  trail_bps: int = None, trail_activate_price: float = None):
        """Persist stop/take-profit/trailing levels on the current position."""
        pos = self.state.get().get("current_position")
        if not pos:
            log.warning("No position to set stops on")
            return
        if stop_loss is not None:
            pos["stop_loss_price"] = stop_loss
        if take_profit is not None:
            pos["take_profit_price"] = take_profit
        if trail_bps is not None:
            pos["trail_bps"] = trail_bps
            pos["trail_activate_price"] = trail_activate_price
            pos["trail_watermark"] = pos.get("entry_price")
        self.state.save()
        log.info("PAPER STOPS: SL=%s TP=%s trail=%s", stop_loss, take_profit, trail_bps)

    def check_stops(self, current_price: float, high_water: float) -> Optional[str]:
        """
        Check all stop/TP/trailing levels. Return exit_reason string or None.
        Trailing: once high_water passes trail_activate_price, ratchet a stop
        trail_bps behind the best watermark seen.
        """
        pos = self.state.get().get("current_position")
        if not pos:
            return None

        side = pos["side"]
        sl = pos.get("stop_loss_price")
        tp = pos.get("take_profit_price")
        trail_bps = pos.get("trail_bps")
        trail_activate = pos.get("trail_activate_price")
        watermark = pos.get("trail_watermark", pos["entry_price"])

        if not sl and not tp and not trail_bps:
            return None

        # Stop loss
        if sl and ((side == "long" and current_price <= sl) or
                   (side == "short" and current_price >= sl)):
            return f"Stop loss at ${current_price:.2f} (SL ${sl:.2f})"

        # Take profit
        if tp and ((side == "long" and current_price >= tp) or
                   (side == "short" and current_price <= tp)):
            return f"Take profit at ${current_price:.2f} (TP ${tp:.2f})"

        # Trailing stop
        if trail_bps and trail_activate:
            if side == "long":
                new_water = max(watermark, high_water)
                if high_water >= trail_activate:
                    pos["trail_watermark"] = new_water
                    self.state.save()  # R2-10: persist watermark mutation
                    trail_price = new_water * (1 - trail_bps / 10000)
                    if current_price <= trail_price:
                        return f"Trailing stop at ${current_price:.2f} (${trail_bps}bps from ${new_water:.2f})"
            else:
                new_water = min(watermark, high_water)
                if high_water <= trail_activate:
                    pos["trail_watermark"] = new_water
                    self.state.save()  # R2-10
                    trail_price = new_water * (1 + trail_bps / 10000)
                    if current_price >= trail_price:
                        return f"Trailing stop at ${current_price:.2f} (${trail_bps}bps from ${new_water:.2f})"

        return None

    def close_position(self, exit_price: float, reason: str = "manual") -> Optional[dict]:
        """Close position at exit_price. Records trade, updates state.equity."""
        pos = self.state.get().get("current_position")
        if not pos:
            return None

        size = pos["size"]
        entry = pos["entry_price"]
        raw_pnl = (exit_price - entry) * size if pos["side"] == "long" else (entry - exit_price) * size
        exit_fees = round(size * exit_price * 0.0005, 2)
        entry_fees = pos.get("fees_paid", 0)
        total_fees = entry_fees + exit_fees
        net_pnl = round(raw_pnl - total_fees, 2)

        # Update equity
        state = self.state.get()
        state["equity"] = round(state.get("equity", 10000.0) + net_pnl, 2)

        # Append to equity timeline (R2-8)
        timeline = state.setdefault("equity_timeline", [])
        timeline.append({"ts": datetime.now(timezone.utc).isoformat(), "equity": state["equity"], "source": "trade"})
        if len(timeline) > 1000:
            state["equity_timeline"] = timeline[-500:]

        trade = {
            "ticker": pos["ticker"],
            "side": pos["side"],
            "entry_price": entry,
            "exit_price": exit_price,
            "size": size,
            "entry_ts": pos["entry_ts"],
            "exit_ts": datetime.now(timezone.utc).isoformat(),
            "raw_pnl": round(raw_pnl, 2),
            "entry_fees": entry_fees,
            "exit_fees": exit_fees,
            "total_fees": total_fees,
            "net_pnl": net_pnl,
            "reason": reason,
        }
        self.state.add_trade(trade)
        self.state.update(current_position=None, pending_order=None)

        log.info(
            "PAPER CLOSE: %s %d @ $%.2f — raw $%.2f fees $%.2f net $%.2f equity $%.2f",
            pos["side"].upper(), size, exit_price, raw_pnl, total_fees, net_pnl, state["equity"],
        )
        return trade

    def apply_funding(self, rate: float, notional: float, side: str, events: int = 1) -> float:
        """
        Apply funding P&L for `events` funding periods (default 1).
        Short receives when rate > 0; long pays.
        Returns the dollar amount.
        """
        amount = -rate * notional * events if side == "long" else rate * notional * events
        state = self.state.get()
        state["equity"] = round(state.get("equity", 10000.0) + amount, 2)
        state["last_funding_applied_ts"] = datetime.now(timezone.utc).isoformat()
        # Append to equity timeline (R2-8)
        timeline = state.setdefault("equity_timeline", [])
        timeline.append({"ts": datetime.now(timezone.utc).isoformat(), "equity": state["equity"], "source": "funding"})
        if len(timeline) > 1000:
            state["equity_timeline"] = timeline[-500:]
        self.state.save()
        log.info("PAPER FUNDING: $%.2f → equity $%.2f", amount, state["equity"])
        return amount

    def get_position(self) -> Optional[dict]:
        return self.state.get().get("current_position")

    def has_position(self) -> bool:
        return self.state.get().get("current_position") is not None

    def cancel_pending(self):
        self.state.update(pending_order=None)
"""
Order simulation — paper mode.
Tracks what orders would have been placed without touching real money.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class PaperOrderManager:
    """
    Simulates order placement, amendment, and cancellation in paper mode.
    Keeps an in-memory view of "what the exchange would show if we were live."
    Positions are reconciled from the state file.
    """

    def __init__(self, auth, market_data, state_manager):
        self.auth = auth
        self.market = market_data
        self.state = state_manager

    def place_limit_order(
        self,
        ticker: str,
        side: str,  # "bid" for long, "ask" for short
        count: int,
        price: float,
        time_in_force: str = "good_till_canceled",
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> dict:
        """
        Paper-mode order placement — logs intent, updates state, does NOT call API.
        Returns simulated response matching the API shape.
        """
        order_id = f"paper_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        client_id = f"client_{order_id}"

        entry = {
            "ticker": ticker,
            "side": side,
            "count": f"{count}.00",
            "price": f"{price:.4f}",
            "time_in_force": time_in_force,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "client_order_id": client_id,
        }

        # Update state with pending order
        self.state.update(
            pending_order={
                "order_id": order_id,
                "client_order_id": client_id,
                "ticker": ticker,
                "side": side,
                "count": count,
                "price": price,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )

        log.info(
            "PAPER ORDER: %s %d @ %.4f (%s) — %s",
            side.upper(), count, price, ticker, time_in_force,
        )

        return {
            "order_id": order_id,
            "client_order_id": client_id,
            "fill_count": "0.00",
            "remaining_count": f"{count}.00",
        }

    def simulate_fill(self, fill_price: float, fill_count: int):
        """
        Simulate a fill of the pending order (e.g., market moved to our limit).
        Updates state to show an open position.
        """
        pending = self.state.get().get("pending_order")
        if not pending:
            log.warning("No pending order to simulate fill for")
            return None

        side = "long" if pending["side"] == "bid" else "short"

        pos = {
            "ticker": pending["ticker"],
            "side": side,
            "entry_price": fill_price,
            "size": fill_count,
            "entry_ts": datetime.now(timezone.utc).isoformat(),
            "entry_order_id": pending["order_id"],
            "unrealized_pnl": 0.0,
            "entry_notional": round(fill_count * fill_price, 2),
            "fees": round(fill_count * fill_price * 0.0005, 2),  # est 5bps taker
        }

        self.state.update(
            current_position=pos,
            pending_order=None,
        )

        log.info(
            "PAPER FILL: %s %d @ %.4f — position opened",
            side.upper(), fill_count, fill_price,
        )
        return pos

    def simulate_exit(self, exit_price: float, exit_count: int) -> Optional[dict]:
        """Simulate closing the current position. Records trade in history."""
        pos = self.state.get().get("current_position")
        if not pos:
            log.warning("No position to exit")
            return None

        pnl = round(
            (exit_price - pos["entry_price"]) * pos["size"]
            if pos["side"] == "long"
            else (pos["entry_price"] - exit_price) * pos["size"],
            2,
        )
        fees = round(exit_count * exit_price * 0.0005, 2)
        net_pnl = round(pnl - fees, 2)

        trade_record = {
            "ticker": pos["ticker"],
            "side": pos["side"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "size": pos["size"],
            "entry_ts": pos["entry_ts"],
            "exit_ts": datetime.now(timezone.utc).isoformat(),
            "pnl": pnl,
            "fees": fees,
            "net_pnl": net_pnl,
        }

        self.state.add_trade(trade_record)
        self.state.update(current_position=None, pending_order=None)

        log.info(
            "PAPER EXIT: %s %d @ %.4f — PnL: $%.2f (net: $%.2f)",
            pos["side"].upper(), exit_count, exit_price, pnl, net_pnl,
        )
        return trade_record

    def cancel_pending(self):
        """Cancel any pending order."""
        self.state.update(pending_order=None)
        log.info("PAPER CANCEL: pending order cleared")

    def get_position(self) -> Optional[dict]:
        """Get the current simulated position."""
        return self.state.get().get("current_position")

    def has_position(self) -> bool:
        return self.state.get().get("current_position") is not None

    def has_pending(self) -> bool:
        return self.state.get().get("pending_order") is not None
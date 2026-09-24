"""
Alert dispatcher — sends notifications to Discord via stdout (for cron delivery)
and/or writes to a local log file.
"""

import logging
import json
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class AlertDispatcher:
    """
    Manages alerts. In cron mode, only stdout goes to Discord.
    The framework prints to stdout when there's something to report,
    and the 'no_agent' cron job delivers it.
    """

    def __init__(self, config: dict, state_manager):
        self.cfg = config.get("alerts", {})
        self.state = state_manager
        self._buffer = []

    def signal(self, message: str, data: Optional[dict] = None):
        """A new trading signal was generated."""
        if not self.cfg.get("on_signal", True):
            return
        self._emit("SIGNAL", message, data)

    def entry(self, side: str, price: float, size: int, leverage: float, reason: str):
        """Entered a position."""
        if not self.cfg.get("on_entry", True):
            return
        msg = (
            f"**PAPER ENTRY** — {side.upper()} {size} contract(s) @ ${price:.1f}\n"
            f"Leverage: {leverage}x | {reason}"
        )
        self._emit("ENTRY", msg)

    def exit(self, side: str, price: float, pnl: float, reason: str):
        """Exited a position."""
        if not self.cfg.get("on_exit", True):
            return
        emoji = "🟢" if pnl > 0 else "🔴"
        msg = (
            f"{emoji} **PAPER EXIT** — {side.upper()} @ ${price:.1f}\n"
            f"PnL: ${pnl:+.2f} | {reason}"
        )
        self._emit("EXIT", msg)

    def error(self, message: str):
        """An error occurred."""
        if not self.cfg.get("on_error", True):
            return
        self._emit("ERROR", f"⚠️ {message}")

    def drawdown_warning(self, message: str):
        """Drawdown threshold breached."""
        if not self.cfg.get("on_drawdown_warning", True):
            return
        self._emit("DRAWDOWN", f"📉 {message}")

    def heartbeat(self, message: str):
        """Periodic heartbeat (usually disabled)."""
        if not self.cfg.get("heartbeat", False):
            return
        self._emit("HEARTBEAT", message)

    def flush(self):
        """Print all buffered alerts to stdout (for cron delivery)."""
        if self._buffer:
            for alert in self._buffer:
                print(alert)
            self._buffer.clear()

    def _emit(self, level: str, message: str, data: Optional[dict] = None):
        """Format and buffer an alert."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        formatted = f"[{level}] [{ts}] {message}"
        self._buffer.append(formatted)
        log.info(formatted)
        if data:
            log.debug("Alert data: %s", json.dumps(data, default=str))
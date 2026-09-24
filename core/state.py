"""
State persistence — JSON-based crash recovery.
Saves/loads the framework's runtime state so a crash mid-cycle
restores from where we left off.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional
import copy

log = logging.getLogger(__name__)

DEFAULT_STATE = {
    "version": 2,
    "last_run_ts": None,           # ISO timestamp of last successful cycle
    "last_funding_applied_ts": None,
    "last_daily_check_ts": None,   # date used for daily drawdown anchor
    "cycle_count": 0,
    "error_count": 0,
    "max_error_count": 5,
    "paused": False,
    "pause_reason": None,
    "equity": 10000.0,             # paper trading equity (seeded + funded P&L)
    "peak_equity": 10000.0,        # for drawdown computation
    "daily_start_equity": 10000.0, # today's opening equity
    "current_position": None,      # { ticker, side, entry_price, size, entry_ts,
                                   #   stop_loss_price, take_profit_price,
                                   #   trail_activate_price, trail_bps, trail_watermark,
                                   #   entry_notional, fees_paid }
    "pending_order": None,
    "trade_history": [],
    "params": {},
}


class StateManager:
    """Persist and restore framework state to a JSON file."""

    def __init__(self, state_path: str):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = None

    def load(self) -> dict:
        """Load state from disk, or return defaults."""
        if self._state is not None:
            return self._state
        if self.path.exists():
            try:
                with open(self.path) as f:
                    raw = json.load(f)
                # merge with defaults so new fields are never missing
                merged = copy.deepcopy(DEFAULT_STATE)
                merged.update(raw)
                self._state = merged
                log.info("State loaded from %s — cycle %s", self.path, merged.get("cycle_count", 0))
                return self._state
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Could not load state (%s), starting fresh", e)
        self._state = copy.deepcopy(DEFAULT_STATE)
        return self._state

    def save(self):
        """Write current state to disk atomically."""
        if self._state is None:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=2, default=str)
            tmp.replace(self.path)
        except OSError as e:
            log.error("Failed to save state: %s", e)

    def get(self) -> dict:
        """Get mutable reference to the in-memory state dict."""
        if self._state is None:
            return self.load()
        return self._state

    def update(self, **kwargs):
        """Update state fields and save."""
        state = self.get()
        state.update(kwargs)
        if "cycle_count" in kwargs:
            state["cycle_count"] = kwargs["cycle_count"]
        self.save()

    def set_param(self, key: str, value: Any):
        """Set a strategy parameter in state and save."""
        state = self.get()
        state.setdefault("params", {})[key] = value
        self.save()

    def get_param(self, key: str, default: Any = None) -> Any:
        """Get a strategy parameter from state."""
        return self.get().get("params", {}).get(key, default)

    def record_success(self):
        """Mark a successful cycle — reset error count, update timestamps."""
        state = self.get()
        state["last_run_ts"] = datetime.now(timezone.utc).isoformat()
        state["cycle_count"] = state.get("cycle_count", 0) + 1
        state["error_count"] = 0
        self.save()

    def record_error(self):
        """Increment error count. Returns True if we should pause."""
        state = self.get()
        state["error_count"] = state.get("error_count", 0) + 1
        should_pause = state["error_count"] >= state.get("max_error_count", 5)
        if should_pause:
            state["paused"] = True
            state["pause_reason"] = f"Too many consecutive errors ({state['error_count']})"
        self.save()
        return should_pause

    def pause(self, reason: str):
        """Pause trading."""
        state = self.get()
        state["paused"] = True
        state["pause_reason"] = reason
        self.save()

    def resume(self):
        """Resume trading (manual)."""
        state = self.get()
        state["paused"] = False
        state["pause_reason"] = None
        state["error_count"] = 0
        self.save()

    def is_paused(self) -> bool:
        return self.get().get("paused", False)

    def add_trade(self, trade: dict):
        """Append a completed trade to history, trim to max size."""
        state = self.get()
        history = state.setdefault("trade_history", [])
        history.append(trade)
        max_history = 200
        if len(history) > max_history:
            state["trade_history"] = history[-max_history:]
        self.save()
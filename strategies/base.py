"""
Base strategy interface — all strategies follow this contract.

A strategy is stateful: it receives market data and returns a Signal.
The framework handles position management, risk, and execution.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Signal:
    """What the strategy wants to do. Exactly one action per cycle."""

    action: str  # "enter_long" | "enter_short" | "exit" | "hold"
    confidence: float = 0.0  # 0.0 to 1.0
    reason: str = ""
    metadata: dict = field(default_factory=dict)

    # For enter signals — suggested sizing
    suggested_leverage: Optional[float] = None
    suggested_stop_loss: Optional[float] = None
    suggested_take_profit: Optional[float] = None
    suggested_trailing_bps: Optional[int] = None
    suggested_trailing_activate: Optional[float] = None


@dataclass
class MarketSnapshot:
    """
    What the framework passes to the strategy each cycle.
    All price values are floats in USD.
    """
    ticker: str

    # Current market state
    current_price: float          # last trade price
    bid: float
    ask: float
    mark_price: float

    # 1h candles (last ~200)
    candles_1h: list = field(default_factory=list)

    # Funding
    funding_rate: Optional[float] = None        # current estimate
    next_funding_ts: Optional[str] = None
    historical_funding: list = field(default_factory=list)

    # Risk context
    available_balance: float = 0.0
    current_position: Optional[dict] = None      # None if flat
    current_leverage_estimate: Optional[float] = None

    # Strategy's own params from state (for self-iteration)
    live_params: dict = field(default_factory=dict)

    # Trade history for performance tracking
    recent_trades: list = field(default_factory=list)

    # PB-EMA trend regime (set by main.py from daily candles)
    trend_regime: str = 'UNKNOWN'  # 'UP' | 'DOWN' | 'NEUTRAL'

    # 4h ATR (resampled from 1h by main.py) for wider stop placement
    atr_4h: Optional[float] = None


class BaseStrategy(ABC):
    """Override evaluate() to generate signals. Access live_params from snapshot."""

    def __init__(self, name: str, params: dict):
        self.name = name
        self.params = params  # static config from YAML
        self._performance = {"trades": 0, "wins": 0, "losses": 0}

    @abstractmethod
    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        """
        The core strategy method.
        Receives the current market snapshot and returns a Signal.
        """
        ...

    def get_static_param(self, key: str, default=None):
        """Get a parameter from the YAML config."""
        return self.params.get(key, default)

    def on_trade_completed(self, trade: dict):
        """Called by the framework after each completed trade for performance tracking."""
        self._performance["trades"] += 1
        if trade.get("net_pnl", 0) > 0:
            self._performance["wins"] += 1
        else:
            self._performance["losses"] += 1

    def get_win_rate(self) -> float:
        n = self._performance["trades"]
        return self._performance["wins"] / n if n > 0 else 0.0
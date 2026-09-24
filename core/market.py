"""
Market data fetching — Kalshi perps API.
Handles candles, orderbook, funding rates, positions, balance.
All methods return dicts; exceptions propagate to the main loop for self-healing.
NOTE: api_base already includes /trade-api/v2/margin — paths are endpoint-short.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)


class MarketData:
    """Pulls market data from the Kalshi perps REST API."""

    def __init__(self, auth):
        self.auth = auth

    # ── public endpoints (no auth) ──────────────────────────────────────

    def get_market(self, ticker: str = "KXBTCPERP") -> dict:
        """Full market detail: price, OI, volume, leverage estimates."""
        return self.auth.public_get(f"/markets/{ticker}")

    def get_orderbook(self, ticker: str = "KXBTCPERP", depth: int = 0) -> dict:
        """Orderbook. depth=0 means all levels."""
        return self.auth.public_get(f"/markets/{ticker}/orderbook", depth=depth)

    def get_trades(self, ticker: str = "KXBTCPERP", limit: int = 100) -> dict:
        """Recent public trades."""
        return self.auth.public_get(f"/markets/{ticker}/trades", limit=limit)

    def get_candlesticks(
        self,
        ticker: str = "KXBTCPERP",
        period_minutes: int = 60,
        limit: int = 200,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> dict:
        """
        OHLCV candlesticks.
        period_interval is in MINUTES (60 = 1h, 1440 = 1d).
        start_ts/end_ts in Unix SECONDS; defaults to last `limit` candles.
        Returns: { ticker, candlesticks: [...] }
        """
        import time as _time
        end_ts = end_ts or int(_time.time())
        start_ts = start_ts or (end_ts - limit * period_minutes * 60)
        return self.auth.public_get(
            f"/markets/{ticker}/candlesticks",
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=period_minutes,
            limit=limit,
        )

    def get_funding_rate_estimate(self, ticker: str = "KXBTCPERP") -> dict:
        """Next funding time + estimated rate for the in-progress period."""
        return self.auth.public_get(f"/funding_rates/estimate", ticker=ticker)

    def get_historical_funding_rates(self, ticker: str = "KXBTCPERP", limit: int = 30) -> dict:
        """Past funding rates."""
        import time as _time
        return self.auth.public_get(
            f"/funding_rates/historical",
            ticker=ticker,
            end_ts=int(_time.time()),
        )

    # ── authenticated endpoints ─────────────────────────────────────────

    def get_balance(self) -> dict:
        """Account balance: cash, equity, margin, available per subaccount."""
        return self.auth.signed_get("/portfolio/balance")

    def get_positions(self, ticker: Optional[str] = None) -> dict:
        """Open positions. Filter by ticker if provided."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        return self.auth.signed_get("/positions", **params)

    def get_risk(self) -> dict:
        """Portfolio risk: total notional, maintenance margin, per-position liquidation prices."""
        return self.auth.signed_get("/portfolio/risk")

    def get_fills(self, ticker: Optional[str] = None, limit: int = 50) -> dict:
        """Fill history."""
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.auth.signed_get("/fills", **params)

    def get_funding_history(self, ticker: str = "KXBTCPERP", limit: int = 30) -> dict:
        """Historical funding payments received/paid (authenticated)."""
        from datetime import date, timedelta as _td
        end = date.today()
        start = end - _td(days=30)
        return self.auth.signed_get(
            "/funding_history",
            ticker=ticker,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )

    # ── convenience helpers ─────────────────────────────────────────────

    def get_current_price(self, ticker: str = "KXBTCPERP") -> float:
        """Get last-trade price as float."""
        data = self.get_market(ticker)
        mkt = data.get("market", data)
        return float(mkt.get("price", 0))

    def get_available_balance(self, subaccount: int = 0) -> float:
        """Available balance for a subaccount."""
        data = self.get_balance()
        for sb in data.get("subaccount_balances", []):
            if sb.get("subaccount") == subaccount:
                return float(sb.get("available_balance", 0))
        return 0.0

    def get_position(self, ticker: str = "KXBTCPERP") -> Optional[dict]:
        """Get position for a specific ticker, or None if no position."""
        data = self.get_positions(ticker)
        for pos in data.get("positions", []):
            if pos.get("market_ticker") == ticker:
                return pos
        return None
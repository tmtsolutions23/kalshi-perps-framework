"""
Kalshi BTC Perps Trading Framework — Main Loop
================================================
Orchestrates market data → strategy → execution → alerting in a
self-healing, state-persistent cycle.

Run modes:
  - Once (cron):  python main.py
  - Loop:         python main.py --loop
  - Override:     python main.py --strategy=mean_reversion --leverage=3.0

All state persists in data/state.json for crash recovery.
"""

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import yaml

# ── core modules ─────────────────────────────────────────────────────────
from core.auth import KalshiAuth
from core.market import MarketData
from core.state import StateManager
from core.risk import RiskManager
from core.orders import PaperOrderManager
from alerts.discord import AlertDispatcher

log = logging.getLogger("perps")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    # Env var overrides
    if "KALSHI_PERPS_KEY_CONFIG" in cfg.setdefault("kalshi", {}):
        pass  # already set
    return cfg


def import_strategy(name: str):
    """Dynamically import a strategy module by name."""
    import importlib
    module = importlib.import_module(f"strategies.{name}")
    # Find the strategy class
    for attr in dir(module):
        cls = getattr(module, attr)
        if isinstance(cls, type) and hasattr(cls, "evaluate") and attr != "BaseStrategy":
            return cls
    raise ImportError(f"No strategy class found in strategies.{name}")


class PerpsLoop:
    """The core evaluation loop — ties everything together."""

    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = load_config(config_path)
        self.mode = self.cfg.get("mode", "paper")
        ticker = self.cfg["market"]["ticker"]
        kalshi_cfg = self.cfg["kalshi"]

        # Core instances
        self.auth = KalshiAuth(
            key_config_path=kalshi_cfg["key_config"],
            private_key_path=kalshi_cfg["key_path"],
            api_base=kalshi_cfg["api_base"],
        )
        self.market = MarketData(self.auth)
        self.state = StateManager(self.cfg["paths"]["state_file"])
        self.risk = RiskManager(self.cfg, self.state)
        self.orders = PaperOrderManager(self.auth, self.market, self.state)
        self.alerts = AlertDispatcher(self.cfg, self.state)

        # Strategy
        strategy_name = self.cfg["strategy"]["name"]
        strategy_cls = import_strategy(strategy_name)
        self.strategy = strategy_cls(self.cfg["strategy"].get("params", {}))

        # Runtime state
        self.ticker = ticker
        self.contract_size = 0.0001  # KXBTCPERP fixed
        self._cycle_start = None

    # ── Self-healing helpers ─────────────────────────────────────────────

    def _safe_api_call(self, fn, *args, retries=3, **kwargs):
        """Wrapper with exponential backoff for any API call."""
        last_err = None
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    log.warning("API call failed (attempt %d/%d): %s — retrying in %ds", 
                                attempt + 1, retries, e, wait)
                    time.sleep(wait)
                else:
                    log.error("API call failed after %d retries: %s", retries, e)
        raise last_err

    def _reconcile_position(self):
        """Verify our state matches what the exchange reports (paper mode: just log)."""
        state = self.state.get()
        pos = state.get("current_position")
        if pos:
            log.info("Position held: %s %d @ %.4f",
                     pos.get("side", "?"), pos.get("size", 0), pos.get("entry_price", 0))
        else:
            log.info("Position: flat")

    # ── Data fetching ────────────────────────────────────────────────────

    def _fetch_snapshot(self) -> Optional[dict]:
        """Fetch all data needed for one evaluation cycle. Returns None on failure."""
        try:
            # Market data (public, no auth needed)
            market = self._safe_api_call(self.market.get_market, self.ticker)
            mkt = market.get("market", market)
            price = float(mkt.get("price", 0))
            bid = float(mkt.get("bid", price))
            ask = float(mkt.get("ask", price))
            mark = float(mkt.get("settlement_mark_price", {}).get("price", price))
            lev_est = mkt.get("leverage_estimate")

            # Candles
            candles_resp = self._safe_api_call(self.market.get_candlesticks,
                                                self.ticker, period_minutes=60, limit=200)
            candles = candles_resp.get("candlesticks", [])

            # Funding
            try:
                fund = self._safe_api_call(self.market.get_funding_rate_estimate, self.ticker)
                fund_rate = fund.get("funding_rate")
                next_fund_ts = fund.get("next_funding_time")
            except Exception:
                fund_rate = None
                next_fund_ts = None

            # Balance and position (authenticated)
            available = 0.0
            if self.mode == "paper":
                available = 10000.0  # paper starting balance
                # Use state's simulated balance
                state = self.state.get()
                if state.get("peak_balance") is None:
                    state["peak_balance"] = available
                    state["daily_start_balance"] = available
                    self.state.save()

            current_pos = self.state.get().get("current_position")

            recent_trades = self.state.get().get("trade_history", [])[-30:]

            # Live params from state (self-adapted)
            live_params = self.state.get().get("params", {})

            return {
                "price": price,
                "bid": bid,
                "ask": ask,
                "mark": mark,
                "candles": candles,
                "funding_rate": fund_rate,
                "next_funding_ts": next_fund_ts,
                "available_balance": available,
                "current_position": current_pos,
                "leverage_estimate": lev_est,
                "recent_trades": recent_trades,
                "live_params": live_params,
            }

        except Exception as e:
            log.error("Failed to fetch market snapshot: %s", e)
            return None

    # ── Adaptation ───────────────────────────────────────────────────────

    def _adapt_strategy(self):
        """Self-iteration: tune strategy parameters based on recent trade performance."""
        state = self.state.get()
        trades = state.get("trade_history", [])
        adapt_cfg = self.cfg.get("performance", {})

        if not adapt_cfg.get("adapt_params", True):
            return

        min_trades = adapt_cfg.get("adaptation_min_trades", 10)
        lookback = adapt_cfg.get("adaptation_lookback", 30)

        if len(trades) < min_trades:
            return

        recent = trades[-lookback:]
        wins = sum(1 for t in recent if t.get("net_pnl", 0) > 0)
        losses = len(recent) - wins
        win_rate = wins / len(recent) if recent else 0.5
        avg_win = sum(t.get("net_pnl", 0) for t in recent if t.get("net_pnl", 0) > 0) / max(wins, 1)
        avg_loss = abs(sum(t.get("net_pnl", 0) for t in recent if t.get("net_pnl", 0) < 0)) / max(losses, 1)
        profit_factor = avg_win / max(avg_loss, 0.01)

        params = state.setdefault("params", {})
        adapted = False

        # Adjust leverage based on win rate
        if win_rate < 0.35:
            # Losing too much — reduce leverage
            new_lev = max(2.0, float(params.get("leverage", 4.0)) - 0.5)
            if new_lev != params.get("leverage"):
                params["leverage"] = new_lev
                adapted = True
                log.info("Adaptation: lowering leverage to %.1fx (win rate %.0f%%)", new_lev, win_rate * 100)
        elif win_rate > 0.65:
            new_lev = min(4.0, float(params.get("leverage", 2.0)) + 0.5)
            if new_lev != params.get("leverage"):
                params["leverage"] = new_lev
                adapted = True
                log.info("Adaptation: raising leverage to %.1fx (win rate %.0f%%)", new_lev, win_rate * 100)

        # Adjust pullback threshold based on profit factor
        if profit_factor < 0.8:
            # Tighten entries when PnL isn't making up for losses
            new_pullback = max(0.1, float(params.get("pullback", 0.3)) - 0.05)
            if new_pullback != params.get("pullback"):
                params["pullback"] = new_pullback
                adapted = True
                log.info("Adaptation: tightening pullback to %.2f (PF %.1f)", new_pullback, profit_factor)
        elif profit_factor > 2.0 and win_rate > 0.5:
            new_pullback = min(0.5, float(params.get("pullback", 0.3)) + 0.05)
            if new_pullback != params.get("pullback"):
                params["pullback"] = new_pullback
                adapted = True
                log.info("Adaptation: loosening pullback to %.2f (PF %.1f)", new_pullback, profit_factor)

        if adapted:
            self.state.save()

    # ── Execution ────────────────────────────────────────────────────────

    def _execute_signal(self, signal, snapshot: dict):
        """Execute a strategy signal in paper mode."""
        price = snapshot["price"]

        if signal.action == "enter_long":
            count, lev = self.risk.compute_position_size(
                snapshot["available_balance"],
                price,
                signal.suggested_leverage or 4.0,
            )
            if count <= 0:
                log.info("Entry skipped: position sizing returned 0 (circuit breaker?)")
                return

            self.orders.place_limit_order(self.ticker, "bid", count, price)
            self.orders.simulate_fill(price, count)
            self.alerts.entry("long", price, count, lev, signal.reason)

        elif signal.action == "enter_short":
            count, lev = self.risk.compute_position_size(
                snapshot["available_balance"],
                price,
                signal.suggested_leverage or 4.0,
            )
            if count <= 0:
                return
            self.orders.place_limit_order(self.ticker, "ask", count, price)
            self.orders.simulate_fill(price, count)
            self.alerts.entry("short", price, count, lev, signal.reason)

        elif signal.action == "exit":
            pos = self.orders.get_position()
            if pos:
                size = pos.get("size", 0)
                trade = self.orders.simulate_exit(price, size)
                if trade:
                    self.strategy.on_trade_completed(trade)
                    self.alerts.exit(pos["side"], price, trade["net_pnl"], signal.reason)
            else:
                log.info("Exit signal but no position to close")

        elif signal.action == "hold":
            if snapshot.get("current_position"):
                log.info("HOLD: %s", signal.reason)
            else:
                log.debug("HOLD (flat): %s", signal.reason)

    # ── Main cycle ───────────────────────────────────────────────────────

    def run_cycle(self) -> bool:
        """
        Execute one complete evaluation cycle.
        Returns True if successful, False on unhandled error.
        """
        self._cycle_start = datetime.now(timezone.utc)
        log.info("=== Cycle start ===")

        try:
            # 1. Check if paused
            if self.state.is_paused():
                reason = self.state.get().get("pause_reason", "unknown")
                log.warning("Trading paused: %s", reason)
                self.alerts.error(f"Trading paused: {reason}")
                return True  # not a crash, just paused

            # 2. Fetch snapshot (self-healing retries built in)
            snapshot_data = self._fetch_snapshot()
            if snapshot_data is None:
                log.error("Failed to fetch snapshot — recording error")
                should_pause = self.state.record_error()
                if should_pause:
                    self.alerts.error(f"Paused after {self.state.get().get('error_count')} consecutive fetch failures")
                return False

            price = snapshot_data["price"]
            if price <= 0:
                log.error("Invalid price ($%.2f) — skipping cycle", price)
                self.state.record_error()
                return False

            log.info("BTC: $%.2f | bid=$%.2f ask=$%.2f | funding=%s",
                     price, snapshot_data["bid"], snapshot_data["ask"],
                     snapshot_data.get("funding_rate", "N/A"))

            # 3. Reconcile known state
            self._reconcile_position()

            # 4. Build market snapshot for strategy
            from strategies.base import MarketSnapshot
            ms = MarketSnapshot(
                ticker=self.ticker,
                current_price=price,
                bid=snapshot_data["bid"],
                ask=snapshot_data["ask"],
                mark_price=snapshot_data["mark"],
                candles_1h=snapshot_data["candles"],
                funding_rate=snapshot_data.get("funding_rate"),
                next_funding_ts=snapshot_data.get("next_funding_ts"),
                available_balance=snapshot_data["available_balance"],
                current_position=snapshot_data["current_position"],
                current_leverage_estimate=snapshot_data.get("leverage_estimate"),
                live_params=snapshot_data.get("live_params", {}),
                recent_trades=snapshot_data.get("recent_trades", []),
            )

            # 5. Run strategy
            signal = self.strategy.evaluate(ms)

            log.info("Signal: %s (conf=%.2f) — %s", signal.action, signal.confidence, signal.reason)

            # 6. Alert on signal
            if signal.action in ("enter_long", "enter_short"):
                self.alerts.signal(
                    f"{signal.action.replace('enter_', '').upper()} signal "
                    f"(conf={signal.confidence:.0%}) — {signal.reason}",
                )
            elif signal.action == "exit":
                self.alerts.signal(f"EXIT signal — {signal.reason}")

            # 7. Execute (paper mode)
            if self.mode == "paper":
                self._execute_signal(signal, snapshot_data)
            else:
                log.info("Live mode — not yet implemented, pending perps access")

            # 8. Self-adaptation
            self._adapt_strategy()

            # 9. Record success, flush alerts
            self.state.record_success()
            self.alerts.flush()

            elapsed = (datetime.now(timezone.utc) - self._cycle_start).total_seconds()
            log.info("=== Cycle complete (%.1fs) ===", elapsed)
            return True

        except Exception as e:
            log.error("Unhandled error in cycle: %s", e)
            log.debug(traceback.format_exc())
            self.state.record_error()
            self.alerts.error(f"Cycle failed: {e}")
            self.alerts.flush()
            return False

    def run_loop(self, interval_minutes: int = 240):
        """Run in continuous loop mode."""
        log.info("Starting continuous loop (interval=%d min)", interval_minutes)
        while True:
            self.run_cycle()
            log.info("Sleeping %d minutes...", interval_minutes)
            time.sleep(interval_minutes * 60)

    def close(self):
        """Clean shutdown — save state, flush alerts."""
        self.state.save()
        self.alerts.flush()
        log.info("Shutdown complete")


# ── CLI entry point ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi BTC Perps Trading Framework")
    parser.add_argument("--loop", action="store_true", help="Run in continuous loop mode")
    parser.add_argument("--interval", type=int, default=240, help="Loop interval in minutes (default: 240)")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("--strategy", help="Override strategy name")
    parser.add_argument("--leverage", type=float, help="Override max leverage")
    parser.add_argument("--mode", choices=["paper", "live"], help="Override mode")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Logging setup
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Override config
    if args.strategy:
        with open(args.config) as f:
            cfg = yaml.safe_load(f)
        cfg["strategy"]["name"] = args.strategy
        with open(args.config, "w") as f:
            yaml.dump(cfg, f)

    # Build and run
    engine = PerpsLoop(config_path=args.config)

    if args.leverage:
        engine.cfg.setdefault("risk", {})["max_leverage"] = args.leverage
    if args.mode:
        engine.mode = args.mode

    try:
        if args.loop:
            engine.run_loop(args.interval)
        else:
            engine.run_cycle()
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
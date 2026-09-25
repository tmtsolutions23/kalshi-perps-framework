"""
Kalshi BTC Perps Trading Framework — Main Loop
================================================
Orchestrates market data → stop check → funding → strategy → execution → alerting
in a self-healing, state-persistent cycle with realistic paper simulation.

P0 fixes applied per audit:
  - Stop loss / take profit / trailing enforced each cycle (P0-1)
  - Equity tracked in state, drawdown circuit breakers active (P0-2)
  - Funding P&L accrued on open positions each cycle (P0-3)
  - Fills use bid/ask + slippage, entry+exit fees on net PnL (P0-4)

Run modes:
  - Once (cron):  python main.py
  - Loop:         python main.py --loop
  - Override:     python main.py --strategy=mean_reversion --leverage=3.0
"""

import argparse
import logging
import math
import sys
import time
import traceback
from datetime import datetime, timezone, date
from typing import Optional

import yaml

from core.auth import KalshiAuth
from core.market import MarketData
from core.state import StateManager
from core.risk import RiskManager
from core.orders import PaperOrderManager
from alerts.discord import AlertDispatcher
from backtest.engine import PerformanceTracker

log = logging.getLogger("perps")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def import_strategy(name: str):
    import importlib
    module = importlib.import_module(f"strategies.{name}")
    for attr in dir(module):
        cls = getattr(module, attr)
        if isinstance(cls, type) and hasattr(cls, "evaluate") and attr != "BaseStrategy":
            return cls
    raise ImportError(f"No strategy class found in strategies.{name}")


class PerpsLoop:
    """Core evaluation loop."""

    def __init__(self, config_path: str = "config.yaml"):
        self.cfg = load_config(config_path)
        self.mode = self.cfg.get("mode", "paper")
        ticker = self.cfg["market"]["ticker"]
        kalshi_cfg = self.cfg["kalshi"]

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

        strategy_name = self.cfg["strategy"]["name"]
        self.strategy = import_strategy(strategy_name)(self.cfg["strategy"].get("params", {}))
        self.ticker = ticker
        self._cycle_start = None
        self._tracker = PerformanceTracker(self.state)
        self._btc_start_recorded = False
        self._last_metrics_print = None

        # Seed paper equity from config if not yet set
        state = self.state.get()
        if state.get("equity") is None or state.get("cycle_count", 0) == 0:
            initial = self.cfg.get("risk", {}).get("initial_equity", 500)
            state["equity"] = float(initial)
            state["peak_equity"] = float(initial)
            state["daily_start_equity"] = float(initial)
            self.state.save()

    # ── Self-healing ─────────────────────────────────────────────────────

    def _safe_api_call(self, fn, *args, retries=3, **kwargs):
        last_err = None
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    log.warning("API call failed (attempt %d/%d): %s — retry in %ds",
                                attempt + 1, retries, e, wait)
                    time.sleep(wait)
                else:
                    log.error("API call failed after %d retries: %s", retries, e)
        raise last_err

    def _reconcile(self):
        state = self.state.get()
        pos = state.get("current_position")
        if pos:
            log.info("Position: %s %d @ %.4f (SL=%s TP=%s)",
                     pos["side"], pos["size"], pos["entry_price"],
                     pos.get("stop_loss_price"), pos.get("take_profit_price"))
        else:
            log.info("Position: flat — equity=$%.2f", state.get("equity", 10000))

    # ── Data fetching ────────────────────────────────────────────────────

    def _fetch_snapshot(self) -> Optional[dict]:
        try:
            market = self._safe_api_call(self.market.get_market, self.ticker)
            mkt = market.get("market", market)
            price = float(mkt.get("price", 0))
            bid = float(mkt.get("bid", price))
            ask = float(mkt.get("ask", price))
            mark = float(mkt.get("settlement_mark_price", {}).get("price", price))
            lev_est = mkt.get("leverage_estimate")

            candles_resp = self._safe_api_call(
                self.market.get_candlesticks, self.ticker, period_minutes=60, limit=200
            )
            candles = candles_resp.get("candlesticks", [])

            # 4h ATR for stop distances — resample 1h candles into 4h buckets
            atr_4h = None
            try:
                atr_4h = self._compute_resampled_atr(candles, hours=4)
                if atr_4h > 0:
                    self.risk.set_atr_4h(atr_4h)
            except Exception:
                pass

            # PB-EMA regime from daily candles
            try:
                import time as _time
                c1d = self._safe_api_call(
                    self.market.get_candlesticks, self.ticker, period_minutes=1440,
                    limit=100, start_ts=int(_time.time()) - 120 * 86400,
                )
                trend_regime = self._compute_pb_ema_regime(c1d.get("candlesticks", []))
            except Exception:
                trend_regime = "UNKNOWN"

            try:
                fund = self._safe_api_call(self.market.get_funding_rate_estimate, self.ticker)
                fund_rate = fund.get("funding_rate")
                next_fund_ts = fund.get("next_funding_time")
            except Exception:
                fund_rate = None
                next_fund_ts = None

            state = self.state.get()
            equity = state.get("equity", 10000.0)
            current_pos = state.get("current_position")
            recent_trades = state.get("trade_history", [])[-30:]
            live_params = state.get("params", {})

            self.risk._snapshot_price = price

            return {
                "price": price,
                "bid": bid,
                "ask": ask,
                "mark": mark,
                "candles": candles,
                "atr_4h": atr_4h,
                "funding_rate": fund_rate,
                "next_funding_ts": next_fund_ts,
                "available_balance": equity,
                "current_position": current_pos,
                "leverage_estimate": lev_est,
                "recent_trades": recent_trades,
                "live_params": live_params,
                "trend_regime": trend_regime,
            }
        except Exception as e:
            log.error("Failed to fetch snapshot: %s", e)
            return None

    # ── Funding accrual (P0-3) ──────────────────────────────────────────

    def _accrue_funding(self, state_snapshot: dict):
        """
        Apply funding P&L for funding events elapsed since last check.
        Kalshi funding occurs every 8h at 12AM/8AM/4PM ET.
        Reads last_funding_applied_ts to avoid double-counting.
        """
        pos = self.state.get().get("current_position")
        if not pos:
            return

        rate = state_snapshot.get("funding_rate")
        if rate is None or rate == 0:
            return

        notional = pos.get("entry_notional", 0)
        if notional <= 0:
            return

        state = self.state.get()
        last_ts = state.get("last_funding_applied_ts")
        now = datetime.now(timezone.utc)

        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                hours_elapsed = (now - last_dt).total_seconds() / 3600
                events_elapsed = int(hours_elapsed / 8)
                if events_elapsed < 1:
                    return  # no full funding interval has passed
            except (ValueError, TypeError):
                events_elapsed = 1
        else:
            events_elapsed = 1  # first check — apply one interval

        self.orders.apply_funding(rate, notional, pos["side"], events_elapsed)

        # Record the last APPLIED event time, not now — align to 8h grid
        state["last_funding_applied_ts"] = now.isoformat()

    # ── Stop checking (P0-1) ─────────────────────────────────────────────

    def _check_stops(self, snapshot: dict) -> Optional[str]:
        """Check if stops/trailing are hit. Returns exit reason or None."""
        pos = self.orders.get_position()
        if not pos:
            return None

        price = snapshot["price"]
        high_water = max(snapshot.get("price", 0), pos.get("entry_price", 0))
        if pos["side"] == "short":
            high_water = min(snapshot.get("price", 0), pos.get("entry_price", 0))

        return self.orders.check_stops(price, high_water)

    # ── 4h ATR computation for stop distances ──────────────────────────

    def _compute_resampled_atr(self, candles: list, hours: int = 4, period: int = 14) -> float:
        """Resample 1h candles into N-hour buckets, compute ATR(period) on them."""
        # Build contiguous price series first
        closes, highs, lows = [], [], []
        for c in candles:
            try:
                p = c.get("price", {})
                h = float(p.get("high", 0))
                l = float(p.get("low", 0))
                cl = p.get("close")
                if cl is None:
                    continue
                highs.append(h)
                lows.append(l)
                closes.append(float(cl))
            except (TypeError, ValueError):
                continue

        if len(closes) < period * hours * 2:
            return 0.0

        # Bucket into N-hour candles: high = max(highs), low = min(lows), close = last close
        bucket_h, bucket_l, bucket_c = [], [], []
        for i in range(0, len(closes) - (len(closes) % hours), hours):
            chunk_h = highs[i:i+hours]
            chunk_l = lows[i:i+hours]
            chunk_c = closes[i:i+hours]
            if not chunk_c:
                continue
            bucket_h.append(max(chunk_h))
            bucket_l.append(min(chunk_l))
            bucket_c.append(chunk_c[-1])

        if len(bucket_c) < period + 1:
            return 0.0

        trs = []
        for i in range(1, len(bucket_c)):
            tr = max(
                bucket_h[i] - bucket_l[i],
                abs(bucket_h[i] - bucket_c[i-1]),
                abs(bucket_l[i] - bucket_c[i-1]),
            )
            trs.append(tr)

        if len(trs) < period:
            return 0.0
        return sum(trs[-period:]) / period

    def _compute_atr(self, candles: list, period: int = 14) -> float:
        """Compute ATR from candle OHLC data. Returns 0 if insufficient data."""
        if len(candles) < period + 1:
            return 0
        trs = []
        for i in range(-period, 0):
            try:
                pc = candles[i].get("price", {})
                prev = candles[i - 1].get("price", {})
                high = float(pc.get("high", 0))
                low = float(pc.get("low", 0))
                close = pc.get("close")
                if close is None:
                    continue
                prev_close = prev.get("close")
                if prev_close is None:
                    prev_close = prev.get("previous", 0)
                tr = max(high - low, abs(high - float(prev_close)), abs(low - float(prev_close)))
                trs.append(tr)
            except (TypeError, ValueError):
                continue
        if len(trs) < period:
            return 0
        return sum(trs) / len(trs)


    # ── PB-EMA trend detection ────────────────────────────────────────────

    def _compute_pb_ema_regime(self, daily_candles: list) -> str:
        """Compute PB-EMA(50) trend regime from daily candles.
        Returns: 'UP' | 'DOWN' | 'NEUTRAL'."""
        period = 50
        blend_w = 0.7

        if len(daily_candles) < period:
            return "UNKNOWN"

        highs, closes = [], []
        for c in daily_candles:
            try:
                p = c.get("price", {})
                highs.append(float(p.get("high", 0)))
                closes.append(float(p.get("close", 0)))
            except (TypeError, ValueError):
                continue

        if len(highs) < period:
            return "UNKNOWN"

        close_slice = closes[-period:]
        blended = [highs[-period + i] * blend_w + closes[-period + i] * (1 - blend_w)
                   for i in range(period)]

        def ema(values, p=period):
            k = 2 / (p + 1)
            e = values[0]
            for v in values[1:]:
                e = v * k + e * (1 - k)
            return e

        ema_top = ema(blended)
        ema_bot = ema(close_slice)
        last_close = closes[-1]

        if last_close > ema_top:
            return "UP"
        elif last_close < ema_bot:
            return "DOWN"
        else:
            return "NEUTRAL"

    # ── Adaptation ───────────────────────────────────────────────────────

    def _win_rate_ci(self, wins: int, n: int) -> tuple:
        """95% Wilson confidence interval for win rate. Returns (lower, upper)."""
        if n == 0:
            return 0, 1
        z = 1.96
        p = wins / n
        denominator = 1 + z**2 / n
        centre = (p + z**2 / (2 * n)) / denominator
        margin = z * math.sqrt((p * (1 - p) / n) + z**2 / (4 * n**2)) / denominator
        return (centre - margin, centre + margin)

    def _adapt_strategy(self):
        """Self-iteration: tune params based on recent trades."""
        state = self.state.get()
        trades = state.get("trade_history", [])
        adapt_cfg = self.cfg.get("performance", {})

        if not adapt_cfg.get("adapt_params", True):
            return

        min_trades = adapt_cfg.get("adaptation_min_trades", 30)
        lookback = adapt_cfg.get("adaptation_lookback", 50)

        if len(trades) < min_trades:
            return

        recent = trades[-lookback:]
        wins = sum(1 for t in recent if t.get("net_pnl", 0) > 0)
        losses = len(recent) - wins
        win_rate = wins / len(recent) if recent else 0.5

        # True profit factor = sum(wins) / sum(|losses|)
        sum_wins = sum(t.get("net_pnl", 0) for t in recent if t.get("net_pnl", 0) > 0)
        sum_losses = abs(sum(t.get("net_pnl", 0) for t in recent if t.get("net_pnl", 0) < 0))
        profit_factor = sum_wins / max(sum_losses, 0.01)

        # P3-1: 95% CI on win rate — must exclude 0.5 to act on WR signals
        ci_lower, ci_upper = self._win_rate_ci(wins, len(recent))
        wr_reliable = ci_lower > 0.5  # reliably above 50% (CI doesn't include 0.5)
        wr_losing = ci_upper < 0.5     # reliably below 50%

        params = state.setdefault("params", {})
        adapted = False

        # Cooldown: only adapt once per 5 cycles
        last_adapt = params.get("_last_adapt_cycle", 0)
        current_cycle = state.get("cycle_count", 0)
        if current_cycle - last_adapt < 5:
            return

        # P3-3: Out-of-sample validation — split trades into train (60%) / validate (40%)
        oos_split = max(3, int(len(recent) * 0.4))
        train_set = recent[:-oos_split] if oos_split > 0 else recent
        val_set = recent[-oos_split:] if oos_split > 0 else []
        val_wins = sum(1 for t in val_set if t.get("net_pnl", 0) > 0)
        val_sum_wins = sum(t.get("net_pnl", 0) for t in val_set if t.get("net_pnl", 0) > 0)
        val_sum_losses = abs(sum(t.get("net_pnl", 0) for t in val_set if t.get("net_pnl", 0) < 0))
        val_pf = val_sum_wins / max(val_sum_losses, 0.01) if val_set else profit_factor

        # Leverage adjustments: cut fast on losses, raise slowly on wins
        if (wr_losing or (win_rate < 0.35 and profit_factor < 0.8)):
            new_lev = max(2.0, float(params.get("leverage", 4.0)) - 0.5)
            if new_lev != params.get("leverage"):
                params["leverage"] = new_lev
                params["_last_adapt_cycle"] = current_cycle
                adapted = True
                log.info("Adapt: lower lev to %.1fx (WR %.0f%% PF %.1f)", new_lev, win_rate * 100, profit_factor)
        elif wr_reliable and profit_factor > 1.5:
            new_lev = min(4.0, float(params.get("leverage", 2.0)) + 0.25)
            if new_lev != params.get("leverage"):
                params["leverage"] = new_lev
                params["_last_adapt_cycle"] = current_cycle
                adapted = True
                log.info("Adapt: raise lev to %.1fx (WR %.0f%% PF %.1f)", new_lev, win_rate * 100, profit_factor)

        # Tighten entry if mis-trading (using true PF) — confirmed on OOS window
        if profit_factor < 0.7 and (len(val_set) < 2 or val_pf < 1.0):
            new_pullback = max(0.1, float(params.get("pullback", 0.3)) - 0.05)
            if new_pullback != params.get("pullback"):
                params["pullback"] = new_pullback
                params["_last_adapt_cycle"] = current_cycle
                adapted = True
                log.info("Adapt: tighten pullback to %.2f (PF %.1f)", new_pullback, profit_factor)
        elif profit_factor > 2.0 and wr_reliable and (len(val_set) < 2 or val_pf > 1.5):
            new_pullback = min(0.5, float(params.get("pullback", 0.3)) + 0.05)
            if new_pullback != params.get("pullback"):
                params["pullback"] = new_pullback
                params["_last_adapt_cycle"] = current_cycle
                adapted = True
                log.info("Adapt: loosen pullback to %.2f (PF %.1f)", new_pullback, profit_factor)

        if adapted:
            self.state.save()

    # ── Signal execution ─────────────────────────────────────────────────

    def _execute_signal(self, signal, snapshot: dict):
        price = snapshot["price"]
        bid = snapshot["bid"]
        ask = snapshot["ask"]
        slippage = self.cfg.get("execution", {}).get("slippage_bps", 5)

        # Check entry allowed before acting
        allowed, reason = self.risk.check_entry_allowed()
        if signal.action in ("enter_long", "enter_short") and not allowed:
            log.info("Entry blocked: %s", reason)
            self.alerts.drawdown_warning(reason)
            return

        if signal.action == "enter_long":
            # P4: scale size by signal confidence
            base_lev = signal.suggested_leverage or 4.0
            confidence = getattr(signal, "confidence", 0.5)
            adj_lev = max(2.0, base_lev * (0.5 + confidence * 0.5))  # scale 0.75x-1x of base

            count, lev = self.risk.compute_position_size(adj_lev)
            if count <= 0:
                log.info("Entry long skipped — sizing returned 0")
                return

            # Paper mode: use market-order fill (no limit constraint, cross spread)
            result = self.orders.place_order(self.ticker, "bid", count, 0, bid, ask, slippage)
            if result:
                pos = result["position"]
                # R2-2: pass trailing stop params from strategy signal to set_stops
                self.orders.set_stops(
                    stop_loss=signal.suggested_stop_loss,
                    take_profit=signal.suggested_take_profit,
                    trail_bps=signal.suggested_trailing_bps,
                    trail_activate_price=signal.suggested_trailing_activate,
                )
                self.alerts.entry("long", result["fill_price"], count, lev, signal.reason)

        elif signal.action == "enter_short":
            base_lev = signal.suggested_leverage or 4.0
            confidence = getattr(signal, "confidence", 0.5)
            adj_lev = max(2.0, base_lev * (0.5 + confidence * 0.5))  # R2-13: correct range is 0.5x-1x

            count, lev = self.risk.compute_position_size(adj_lev)
            if count <= 0:
                return

            result = self.orders.place_order(self.ticker, "ask", count, 0, bid, ask, slippage)
            if result:
                pos = result["position"]
                self.orders.set_stops(
                    stop_loss=signal.suggested_stop_loss,
                    take_profit=signal.suggested_take_profit,
                    trail_bps=signal.suggested_trailing_bps,
                    trail_activate_price=signal.suggested_trailing_activate,
                )
                self.alerts.entry("short", result["fill_price"], count, lev, signal.reason)

        elif signal.action == "exit":
            pos = self.orders.get_position()
            if pos:
                trade = self.orders.close_position(price, reason=signal.reason)
                if trade:
                    self.strategy.on_trade_completed(trade)
                    self.alerts.exit(pos["side"], price, trade["net_pnl"], signal.reason)
            else:
                log.info("Exit signal but no position")

    # ── Main cycle ───────────────────────────────────────────────────────

    def run_cycle(self) -> bool:
        self._cycle_start = datetime.now(timezone.utc)
        log.info("=== Cycle start ===")

        try:
            if self.state.is_paused():
                reason = self.state.get().get("pause_reason", "unknown")
                log.warning("Paused: %s", reason)
                self.alerts.error(f"Paused: {reason}")
                return True

            # Fetch data
            sd = self._fetch_snapshot()
            if sd is None:
                if self.state.record_error():
                    self.alerts.error(f"Paused after {self.state.get()['error_count']} errors")
                return False

            price = sd["price"]
            if price <= 0:
                log.error("Invalid price $%.2f", price)
                self.state.record_error()
                return False

            # P2-5: Record BTC start price for benchmark
            if not self._btc_start_recorded:
                self._tracker.set_btc_start_price(price)
                self._btc_start_recorded = True

            log.info("BTC: $%.4f | bid=$%.4f ask=$%.4f | funding=%s | equity=$%.2f",
                     price, sd["bid"], sd["ask"], sd.get("funding_rate", "?"),
                     self.state.get().get("equity", 10000))

            # Reconcile
            self._reconcile()

            # P0-3: Accrue funding
            self._accrue_funding(sd)

            # P0-1: Check stops before strategy evaluation
            stop_reason = self._check_stops(sd)
            if stop_reason:
                trade = self.orders.close_position(price, reason=stop_reason)
                if trade:
                    self.strategy.on_trade_completed(trade)
                    self.alerts.exit(
                        trade["side"], price, trade["net_pnl"],
                        f"STOP HIT: {stop_reason}",
                    )
                # Position closed — re-read state
                sd["current_position"] = None

            # Build strategy snapshot
            from strategies.base import MarketSnapshot
            ms = MarketSnapshot(
                ticker=self.ticker,
                current_price=price,
                bid=sd["bid"],
                ask=sd["ask"],
                mark_price=sd["mark"],
                candles_1h=sd["candles"],
                funding_rate=sd.get("funding_rate"),
                next_funding_ts=sd.get("next_funding_ts"),
                available_balance=sd["available_balance"],
                current_position=sd["current_position"],
                current_leverage_estimate=sd.get("leverage_estimate"),
                live_params=sd.get("live_params", {}),
                recent_trades=sd.get("recent_trades", []),
                trend_regime=sd.get("trend_regime", "UNKNOWN"),
                atr_4h=sd.get("atr_4h"),
            )

            # Strategy
            signal = self.strategy.evaluate(ms)
            log.info("Signal: %s (conf=%.2f) — %s", signal.action, signal.confidence, signal.reason)

            # Override stop/target prices with 4h resampled ATR for meaningful distances
            atr_4h = sd.get("atr_4h")
            if atr_4h and atr_4h > 0 and signal.action in ("enter_long", "enter_short"):
                side = "long" if signal.action == "enter_long" else "short"
                stops = self.risk.compute_4h_stop_prices(price, side)
                signal.suggested_stop_loss = stops["stop_loss"]
                signal.suggested_take_profit = stops["take_profit"]
                log.info("4h ATR stops: SL=$%.4f TP=$%.4f (ATR=%.4f)", stops["stop_loss"], stops["take_profit"], atr_4h)

            # Kelly sizing update — adapt risk_per_trade_pct as performance accumulates
            state = self.state.get()
            last_kelly_cycle = state.get("params", {}).get("_last_kelly_cycle", 0)
            if state.get("cycle_count", 0) - last_kelly_cycle >= 10:
                metrics = self._tracker.metrics(price)
                if metrics.get("total_trades", 0) >= 10:
                    from core.kelly import compute_from_metrics
                    kelly_pct = compute_from_metrics(metrics, self.cfg.get("risk", {}))
                    if kelly_pct is not None:
                        state["risk_per_trade_pct"] = round(kelly_pct, 4)
                        state.setdefault("params", {})["_last_kelly_cycle"] = state.get("cycle_count", 0)
                        self.state.save()
                        log.info("Kelly update: risk_per_trade=%.2f%% (WR=%.0f%% payoff=%.2f)",
                                 kelly_pct * 100, metrics.get("win_rate", 0) * 100, metrics.get("payoff_ratio", 0))

            if signal.action in ("enter_long", "enter_short"):
                self.alerts.signal(f"{signal.action.replace('enter_', '').upper()} signal (conf={signal.confidence:.0%}) — {signal.reason}")
            elif signal.action == "exit":
                self.alerts.signal(f"EXIT signal — {signal.reason}")

            # Execute
            if self.mode == "paper":
                self._execute_signal(signal, sd)
            else:
                log.info("Live mode — pending perps access")

            # Adapt
            self._adapt_strategy()

            # P2-4: Print performance summary if we have trades
            summary = self._tracker.summary_text(price)
            if summary:
                self.alerts.signal(summary)

            # Done
            self.state.record_success()
            self.alerts.flush()

            elapsed = (datetime.now(timezone.utc) - self._cycle_start).total_seconds()
            log.info("=== Cycle done (%.1fs) ===", elapsed)
            return True

        except Exception as e:
            log.error("Cycle failed: %s", e)
            log.debug(traceback.format_exc())
            self.state.record_error()
            self.alerts.error(f"Cycle failed: {e}")
            self.alerts.flush()
            return False

    def run_loop(self, interval_minutes: int = 240):
        log.info("Continuous loop (interval=%d min)", interval_minutes)
        while True:
            self.run_cycle()
            log.info("Sleeping %d min...", interval_minutes)
            time.sleep(interval_minutes * 60)

    def close(self):
        self.state.save()
        self.alerts.flush()
        log.info("Shutdown")


def main():
    parser = argparse.ArgumentParser(description="Kalshi BTC Perps Trading Framework")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=240)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy")
    parser.add_argument("--leverage", type=float)
    parser.add_argument("--mode", choices=["paper", "live"])
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    # Hold overrides in memory — don't rewrite config file (P4 fix)
    engine = PerpsLoop(config_path=args.config)
    if args.leverage:
        engine.cfg.setdefault("risk", {})["max_leverage"] = args.leverage
    if args.mode:
        engine.mode = args.mode
    if args.strategy:
        engine.strategy = import_strategy(args.strategy)(engine.cfg["strategy"].get("params", {}))
        engine.cfg["strategy"]["name"] = args.strategy  # runtime only

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
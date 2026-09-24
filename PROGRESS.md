# Project Progress — Kalshi BTC Perps Trading Framework

**Repo:** `github.com/tmtsolutions23/kalshi-perps-framework`
**Status:** Phase 1 — Paper trading, live data, self-healing loop running via cron

---

## Project Description

A self-healing, self-iterating paper-trading framework for the Kalshi Bitcoin perp market (KXBTCPERP). Designed to run standalone with zero LLM tokens at runtime — the strategy evaluates market data, simulates entries/exits, adapts its own parameters, and alerts Discord only when there's something to report.

The framework is strategy-agnostic: swap strategies via a one-line config change. Currently ships with two strategies:

- **Funding Momentum** — EMA trend direction + funding rate sentiment filter + ATR-based stops
- **Mean Reversion** — Z-score Bollinger-style mean reversion with funding bias filter

---

## Changelog

### 2026-09-24 — Phase 1 build & initial commit

**What was built:**

- Full project scaffold: `core/`, `strategies/`, `alerts/`, `backtest/`, `tests/`
- `core/auth.py` — RSA-PSS signed requests to Kalshi perps API (same key as predictions, `/margin/` namespace)
- `core/market.py` — fetches prices, orderbook, 1h OHLCV candles, funding estimates, account balance, open positions
- `core/state.py` — JSON state persistence for crash recovery (survives process death mid-cycle)
- `core/risk.py` — position sizing, drawdown circuit breakers (daily 10%, total 25%), SL/TP price computation
- `core/orders.py` — paper order simulation (enter → fill → exit → trade history)
- `strategies/base.py` — abstract `BaseStrategy` with `evaluate(snapshot) -> Signal` interface
- `strategies/funding_momentum.py` — primary strategy: EMA12/EMA48 trend + funding rate sentiment override + pullback entry within pullback×ATR of fast EMA + TP/SL at ATR multipliers + trailing stop at 200bps after 3% profit + max hold 7 days
- `strategies/mean_reversion.py` — second strategy: 20-period z-score with funding bias filter, entry at 2σ, exit at 0.2σ
- `alerts/discord.py` — alert dispatcher; stdout-only output follows the no_agent cron pattern (empty stdout = silent, non-empty → Discord)
- `backtest/engine.py` — performance metrics from trade history (win rate, Sharpe, profit factor, max drawdown)
- `main.py` — orchestration loop: fetch → strategy → execute → adapt → alert
- `config.yaml` — all tuning knobs in one place
- `tests/smoke_test.py` — passes all 6 sections live

**Config defaults:**

| Parameter | Value |
|---|---|
| Leverage range | 2–4x |
| Account per trade | 25% of available |
| Max daily drawdown | 10% |
| Max total drawdown | 25% |
| Check interval | 4 hours |
| Funding adaptation | on (min 0.0001 bias) |
| Self-adaptation | on (≥10 trades) |

**Live data verified:**

- BTC implied price: ~$84,100 via contract price $8.41 (0.0001 BTC per contract)
- Funding rate: +0.0001 (positive, longs paying ~1 bps/h)
- 198 1h candles returned from `candlesticks?period_interval=60`
- EMA12 < EMA48 → downtrend; price 0.4 ATRs above EMA → no entry (correct)

**Bugs found and fixed during build:**

1. Double path prefix in API URLs (`/trade-api/v2/margin` appearing twice) — fixed by using endpoint-relative paths
2. Candlestick endpoint required `start_ts`, `end_ts`, `period_interval` (not just `period`) — discovered via 400 errors
3. Funding endpoint at `/funding_rates/estimate?ticker=` not `/funding/rate/estimate/{ticker}` — discovered via 404
4. Auth signature must be over full path (`/trade-api/v2/margin/enabled`) not short path (`/enabled`) — caused 401 on auth'd endpoints
5. Position sizing was dividing by `contract_size × current_price` when Kalshi prices are already per-contract — resulted in 11.9M contracts instead of 1,194
6. PnL calculation was multiplying by 0.0001 contract_size — inflated losses by 10,000x

**Cron job:** `47c8dfe1c7c5` — every 4h, no_agent, delivers to origin chat. First run ~19:31 UTC.

**Remote:** Pushed to `github.com/tmtsolutions23/kalshi-perps-framework` (public)

### Next up (Phase 2 — Backtesting)

- Historical OHLCV download from Kalshi API (3+ months)
- Walk-forward parameter optimization
- Validate both strategies against past funding cycles
- Sharpe, max DD, profit factor reporting
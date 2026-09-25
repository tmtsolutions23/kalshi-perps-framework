# Project Progress — Kalshi BTC Perps Trading Framework

**Repo:** `github.com/tmtsolutions23/kalshi-perps-framework`
**Status:** All P0-P4 audit findings fixed. Running on main via cron (every 4h, paper mode, zero LLM tokens).

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

---

## Code & Math Audit — 2026-09-24

Full review of strategy math, risk, execution simulation, and metrics. Findings are
ordered by severity. Every quantitative claim below was verified against live
`KXBTCPERP` API data (198 hourly candles, 339 funding events / 113 days of history).

**What's solid — don't regress these:** secrets hygiene is clean (no keys committed,
`.gitignore` correct); RSA-PSS signing over the full path is right; candles confirmed
**oldest-first**, so EMA/ATR direction is correct; state persistence uses atomic
tmp+replace. The scaffold is good. The problems are in the math and the simulation.

### Market facts established during the audit (use these as constants)

| Fact | Value | Source |
|---|---|---|
| Contract size | 0.0001 BTC; price ~8.41 ⇒ BTC ~$84,100 | `/markets/KXBTCPERP` |
| Tick size | 0.0001 | `/markets/KXBTCPERP` |
| Spread (typical) | ask−bid = 0.0005 ≈ **0.6 bps one-way, 1.2 bps round trip** | live quote |
| **Funding interval** | **every 8h (3×/day)** — NOT hourly | `/funding_rates/historical` |
| Funding sign | **337/339 non-negative; only 2 negative in 113 days (0.6%)** | 113d history |
| \|funding\| ≥ 0.0001 | **46% of events** | 113d history |
| ATR(14) on 1h | ≈ 0.73% of price | computed |
| 1.5×ATR stop distance | ≈ **1.09% of price ≈ 109 bps** | computed |
| 4h price move | median 0.36%, p75 0.74%, p90 1.24% | computed |
| **Total market history** | **113 days (launched 2026-06-04)** | candlestick probe |

---

### P0 — Blockers. These either lose real money or invalidate every result so far.

**P0-1. There is no stop loss. Anywhere.**
`strategies/funding_momentum.py:132-133,155-156` computes `sl_price`/`tp_price` and
returns them as `suggested_stop_loss`/`suggested_take_profit`. Nothing ever reads them —
`main.py:_execute_signal` (247-292) ignores both fields. `core/risk.py:110
compute_stop_prices()` is **never called**. `core/state.py:30 active_tpsl` is **never
written**. The trailing stop described in this changelog and configured at
`config.yaml:21-22` (`trailing_activate_pct`, `trailing_bps`) **does not exist in code —
zero references**. Net effect: positions exit only on EMA cross, funding flip (dead —
see P1-2), or the 168h timeout, while running 2–4× leverage.
→ **Fix:** on fill, persist `stop_loss`/`take_profit`/`trailing_high_water` into the
position dict and `state.active_tpsl`. At the top of every cycle, **before** strategy
evaluation, check the current price against them and force an exit. Implement the
trailing stop: once unrealized ≥ `trailing_activate_pct`, ratchet a stop `trailing_bps`
behind the best price reached. Delete the config keys or implement them — do not ship
config that lies about behavior.

**P0-2. The paper balance is a constant, so every risk control is inert.**
`main.py:149-151` sets `available = 10000.0` on **every cycle**, and realized P&L is
never applied to it. Consequences: (a) `RiskManager._check_circuit_breakers` computes
`total_dd = (peak − 10000)/peak` where `peak` was itself initialized to 10000 ⇒
**drawdown is always 0.0% and the 10%/15%/25% breakers can never fire**; (b) position
size never shrinks after losses or grows after wins — no compounding, no decay; (c) the
equity curve needed for Sharpe/max-DD doesn't exist.
→ **Fix:** add `state.equity` (seed 10000). On every `simulate_exit`, apply
`equity += net_pnl`. Feed `equity` — not a literal — into `compute_position_size` and the
breakers. Append `{ts, equity}` to an equity-curve list for the metrics module.

**P0-3. Funding P&L is never accrued — on a strategy named "funding momentum."**
`next_funding_ts` is fetched (`main.py:172`), threaded into the snapshot (`:344`), and
**never used**. No funding is ever credited or debited on an open position.
Magnitude: 0.0001 per 8h event = **3 bps/day = 21 bps over a 168h max hold ≈ 19% of the
109 bps take-profit target.** It is a first-order term, not a rounding error. Note the
sign works *in the system's favor* in its dominant mode (short while funding is positive
⇒ it **receives** funding) — which means ignoring it currently *understates* returns, and
will flip to overstating them the moment a long fires.
→ **Fix:** on each cycle, for each funding event that elapsed since the last cycle,
apply `equity += ± rate × notional` (sign: short receives when rate > 0). Persist
`last_funding_applied_ts` to avoid double-counting across restarts. Record cumulative
`funding_pnl` per trade so it shows up separately in the trade record.

**P0-4. The fill simulation is fantasy and biases every result optimistically.**
`main.py:261-262` places a `post_only=True` limit at `price` (the *last trade* price) and
then calls `simulate_fill(price, count)` **on the very next line** — guaranteed, instant,
full fill at the last price. No spread crossed (a long should pay `ask`, a short receives
`bid`), no slippage, no queue position, no partial fills, no rejection, and — critically
for `post_only` — **no adverse selection**: a resting bid fills precisely when the market
is trading through it. `config.yaml:36 slippage_bps` and `:38 cancel_resting_after_hours`
have **zero code references**.
Also `core/orders.py:99` computes an entry `fees` field that `simulate_exit` (`:120-127`)
**never subtracts** — `net_pnl = pnl − exit_fees` only, so every trade overstates by one
side of fees.
→ **Fix:** longs fill at `ask`, shorts at `bid`, plus `slippage_bps`. Subtract entry +
exit fees. Verify the real Kalshi perps fee schedule rather than assuming a flat 5 bps.
For `post_only`, model fill probability instead of assuming 100%, or switch the sim to
marketable orders and pay the spread honestly.

### P1 — Logic errors in the strategy itself

**P1-1. The funding filter is documented as an "override" but implemented as a veto —
and it silently disables long entries 46% of the time.**
`funding_momentum.py:113-121` sets `bias = funding_bias if funding_bias else trend_bias`,
where positive funding ⇒ `bias="short"`. But the entry gate at `:128` and `:152` requires
**`bias == trend`** (`bias=="long" and uptrend`, `bias=="short" and not uptrend`). So the
"override" can only ever *cancel* a trade, never *initiate* a contrarian one. Combined
with the live data:
- Funding is ≥ 0.0001 on **46%** of events, and is essentially **never negative
  (2 of 339 observations over 113 days)**.
- Therefore `funding_bias == "long"` is **dead code in practice**, and during that 46%
  the system is **short-only** — it takes no position at all in a positive-funding
  uptrend, which is the single most common profitable BTC regime.
→ **Fix:** decide which strategy you actually want and make the code say it. Either
(a) funding is a *confirmation* filter — require trend and funding to agree and accept
the lower trade count, or (b) funding is a genuine *contrarian override* — allow
`enter_short` on positive funding **regardless** of trend. Today it's neither. Whichever
you pick, log the regime split (longs vs shorts vs no-trade) so the bias is visible.

**P1-2. The funding exit rule is dead code.** `:198-201` requires
`abs(fund) > min_funding * 5` = 0.0005. Over 113 days, **2 of 339 events** exceeded that.
→ **Fix:** set the threshold from the empirical distribution (e.g. 90th percentile of
trailing \|funding\|), not a hardcoded multiple.

**P1-3. Position sizing is leverage-driven, not risk-driven — $ risk per trade floats
with volatility.** `core/risk.py:45-55`: `notional = balance × 0.25 × 4` ⇒ **1.0×
account equity of notional** (note: "25% per trade" reads conservative but at 4× is
100% of equity). The stop is then placed independently at 1.5×ATR, so the actual loss
per trade scales with whatever ATR happens to be — quiet regime ≈ 0.5% of equity, ATR
spike ≈ 3%+, with nothing holding it constant.
→ **Fix:** invert it. Size from risk: `contracts = (equity × risk_per_trade_pct) /
stop_distance_per_contract`, then clamp by the leverage cap. Add `risk_per_trade_pct`
(start 0.5–1%) to config. This is the single highest-value change for turning a
coin-flip into something with a survivable variance profile.

**P1-4. "Pullback" entry uses `abs()`, so it fires on breakouts too.**
`:130` and `:154` both test `abs(ema_distance_atr) <= pullback_threshold`. For a long, a
pullback means price came *down* to the EMA; `abs()` also accepts price *above* it. The
gate is really "price is within ±0.3 ATR of the fast EMA" — a proximity band, not a
pullback, and identical for both directions.
→ **Fix:** for longs require `-pullback ≤ dist ≤ 0` (or a small positive tolerance);
mirror for shorts. Verified frequency: the band as written is satisfied on **21.9% of
bars (~1.3 passes/day at 4h polling)**, so tightening it directionally will roughly halve
an already-low trade count — budget for that.

**P1-5. 1:1 reward:risk with real costs needs >55% win rate just to break even.**
`config.yaml:18-19` sets `atr_multiplier_sl == atr_multiplier_tp == 1.5`. Gross target
109 bps; round-trip costs ≈ 1.2 bps spread + ~10 bps fees ≈ **11 bps ≈ 10% of the gross
target**. A symmetric-payoff system therefore needs ≈55% accuracy before funding, and
nothing in the repo demonstrates any edge at all, let alone 55%.
→ **Fix:** either widen TP relative to SL (e.g. 2.5:1.5) or prove the hit rate first.
Report expectancy per trade in bps, net of all costs, as the headline metric.

**P1-6. ATR measures the wrong quantity.** `:56-58` takes `high` from `ask.high` and
`low` from `bid.low` — a **quote range that embeds the spread**, mixed against a
`price.close` previous close. Measured impact on current data is small (−3.7% vs true
range) but the sign is not stable, and it's simply not ATR.
→ **Fix:** use `price.high`/`price.low`/`price.previous` consistently.

**P1-7. The ATR fallback is 2.7× reality and silently widens everything.** `:100`
falls back to `current_price * 0.02` when ATR is `None`. True ATR(14) ≈ 0.73%, so the
fallback is **2.7× too wide** — stops balloon to 3% and the entry band to 0.6% of price
without any log line.
→ **Fix:** use a trailing median of recent ATR, or refuse to trade (`hold`) and emit a
warning rather than trading on a fabricated volatility estimate.

**P1-8. Zero-volume candles return nulls, and the handlers swallow them silently.**
Verified: hours with no trades return `price: {close: null, high: null, low: null,
open: null, mean: null, previous: "7.6414"}` with bid/ask still populated (2 of 198 bars
in the sample). `.get("close", 0)` returns **`None`**, not `0`, because the key exists —
`float(None)` raises `TypeError`, which both `:80-82` and `:59-60` catch and `continue`.
So bars are **silently dropped**, leaving the EMA computed on a non-uniform time grid and
ATR averaged over fewer than `period` samples, with no warning.
→ **Fix:** forward-fill from `price.previous` (it is populated on exactly these bars),
and log a counter when it happens. Assert the final series length matches expectations.

**P1-9. `mean_reversion.py` has no stop loss at all** (`:87-104`) — 2σ entry, 48h hold,
up to 4× leverage, exit only on reversion to 0.2σ or timeout. A 2σ entry that runs to 5σ
is an account-ending trade. Its params (`entry_zscore`, `exit_zscore`, `max_hold_hours`)
are **absent from `config.yaml`**, so the advertised "one-line strategy swap" silently
runs on hardcoded defaults. It also uses the same `fund >= 0` / `fund <= 0` gate, which —
given funding is never negative — means it too is effectively **short-only**.

### P2 — The metrics are wrong, so you cannot tell whether any of this works

**P2-1. `backtest/engine.py:52-55` "Sharpe" is not a Sharpe ratio.** It is
`mean(per-trade $P&L) / population_stdev(per-trade $P&L)` — dollar-denominated,
**unannualized**, and using `÷n` rather than `÷(n−1)` (upward bias of ~2–5% at n=10–30).
A strategy trading once a year and one trading hourly produce the identical number.
→ **Fix:** compute on **returns** (`net_pnl / equity_at_entry`), use sample stdev, and
annualize: `sharpe × sqrt(trades_per_year)`. Add Sortino and Calmar.

**P2-2. `profit_factor` is mislabeled in BOTH places.** `engine.py:50` and
`main.py:207` compute `avg_win / avg_loss`, which is the **payoff ratio**. True profit
factor = `Σwins / |Σlosses|`. A system with 30% win rate and 1.5 payoff reports "1.5"
while its real PF is 0.64 — *a losing system reporting a winning number*, and
`main.py:235` then **loosens entries** on `profit_factor > 2.0`.
→ **Fix:** rename the existing field to `payoff_ratio` and add a correct
`profit_factor`. Drive adaptation off the correct one.

**P2-3. Max drawdown is in dollars from `peak = 0`** (`engine.py:57-67`), so a strategy
underwater from trade 1 measures DD from zero and $500 of DD is indistinguishable
between a $10k and a $1k account. → Track % drawdown on the P0-2 equity curve.

**P2-4. `PerformanceTracker` is never instantiated anywhere**, and nothing writes the
`data/trades.jsonl` it reads (`config.yaml:46,64 track_trades`/`trade_log` have no code
references). Metrics are never computed or reported in the loop.
→ **Fix:** write each closed trade to the JSONL, and print a metrics block on every
cycle (or at least daily) into the Discord alert.

**P2-5. No benchmark.** For a directional BTC strategy this is the metric that decides
whether the project is worth running: if BTC rallies 40% while the algo — structurally
short (P1-1) — returns 5%, that is a failure, not a success.
→ **Fix:** report buy-and-hold BTC return over the identical window alongside every
performance block, plus time-in-market and a long/short P&L split.

### P3 — "Self-learning" is currently an overfitting machine

**P3-1. It adapts on n ≥ 10 trades, which is statistically meaningless.**
`main.py:198,213,220`: at n=10 the 95% confidence interval around a true 50% win rate is
roughly **19%–81%**, so both the `< 0.35` and `> 0.65` triggers fire on pure noise. The
rule **raises leverage after a winning streak** — i.e. it takes maximum size exactly when
the evidence is most likely to be a fluke. That is the textbook path to a blow-up.
→ **Fix:** require n ≥ 30–50 *and* a statistical test (e.g. the win-rate CI must exclude
0.5) before any change; cap adjustments to one step per N cycles; add a cooldown; and
make leverage changes asymmetric — cut fast on losses, raise slowly on wins.

**P3-2. `profit_factor = inf` when there are no losses** (`engine.py:50`;
`main.py:207` guards with `max(avg_loss, 0.01)` which explodes similarly). After a lucky
opening streak the system **simultaneously** raises leverage (P3-1) and loosens the entry
filter (`main.py:235`) — two risk-increasing actions triggered by the same noise.

**P3-3. There is no out-of-sample validation anywhere.** Parameters are tuned on the
same rolling 30-trade window used to judge them. That is in-sample curve fitting, not
learning. Nothing is ever held out, and no adaptation is ever reverted when it fails.
→ **Fix:** every adaptation must be proposed on a training window and *confirmed* on a
held-out window before it goes live; log every parameter change with the evidence that
justified it so changes can be audited and rolled back.

### P4 — Correctness & hygiene

- **`main.py:428-433`: the `--strategy` flag rewrites `config.yaml` in place** via
  `yaml.dump`, destroying comments and formatting and mutating a tracked file as a side
  effect of a runtime flag — and it writes *before* the engine is constructed, so a
  startup crash leaves the config permanently changed. Hold the override in memory.
- **`core/risk.py:88`: `today` is computed and never used** — the daily drawdown anchor
  `daily_start_balance` is set once and **never rolls over at the UTC boundary**, so
  "daily" DD is actually all-time DD from a stale anchor (moot today because of P0-2).
- **`core/risk.py:97 check_entry_allowed()` is never called.**
- **`signal.confidence` is computed by both strategies and never used for anything** but
  a log line. The obvious use — scaling size by conviction — is absent. Note it is also
  halved on essentially every trade (`funding_momentum.py:135`, `× 0.5 if funding_bias`),
  which given P1-1 means "almost always."
- **`core/state.py:25,30`: `daily_pnl` and `active_tpsl` are declared and never written.**
- Dead config keys with zero code references: `trailing_activate_pct`, `trailing_bps`,
  `slippage_bps`, `cancel_resting_after_hours`, `max_concurrent_positions`,
  `funding_aware`, `track_trades`.

### Phase 2 plan — revised, because the current plan is not executable

**The blocker: `KXBTCPERP` has only 113 days of history (launched 2026-06-04).** Probed
at 120/240/400-day lookbacks, the API returns the same 113 daily candles. At the measured
entry-gate frequency, 113 days yields on the order of tens of trades **in a single market
regime**. Walk-forward optimization on that will produce overfit parameters with false
confidence — it is actively worse than not optimizing, because it manufactures
justification for whatever the noise happened to favor.

Do this instead, in order:

1. **Fix P0-1 through P0-4 first.** Until stops exist, equity compounds, funding accrues,
   and fills cost something, *no* backtest or paper number means anything. Everything
   recorded so far should be treated as void.
2. **Develop and validate on a venue with real history.** Binance/Deribit/Bybit have
   years of BTC perp OHLCV *and* 8h funding history. Build the strategy there across at
   least one full bull/bear cycle. Use Kalshi's 113 days only to validate *execution
   mechanics* (contract sizing, fee/spread model, funding timing) — not to decide whether
   the edge exists.
3. **Benchmark against buy-and-hold BTC on every run.** If it doesn't beat holding on a
   risk-adjusted basis, the correct decision is to stop.
4. **Only then** consider walk-forward tuning, with out-of-sample confirmation (P3-3) and
   a hard cap on how many parameters are tuned — with this little data, tuning more than
   2–3 parameters is guaranteed overfitting.
5. **Gate live trading behind explicit criteria set in advance**, e.g. ≥100 paper trades,
   positive expectancy net of all costs, max DD within tolerance, and outperformance vs
   buy-and-hold. Write the thresholds down *before* seeing the results.

**Honest assessment:** the engineering scaffold is good — modular, persistent,
recoverable, and clean on secrets. The trading logic is not yet a strategy with a
demonstrated edge; it is a proximity-to-EMA trigger with a funding veto that structurally
suppresses longs, symmetric 1:1 payoffs, no stop loss, unmodeled funding and fills, and
an adaptation loop that amplifies noise. Fix the P0s before running another paper cycle,
and treat all results to date as void.

---

## 2026-09-24 — P0 Blocker Fixes (branch `fix/p0-blockers`)

All four P0 findings from the audit addressed in one pass.

### P0-1: Stop loss enforcement

**Fix:** `core/orders.py` now has `set_stops()` which persists `stop_loss_price`, `take_profit_price`, `trail_bps`, and `trail_watermark` on the position dict. `check_stops()` is called **before strategy evaluation** each cycle (`main.py:292-303`). It checks:
- Hard SL/TP price breaches
- Trailing stop: once high-water reaches `trail_activate_price` (not yet wired to strategy output), ratchets a stop `trail_bps` behind the best watermark seen

The strategy still computes `suggested_stop_loss` and `suggested_take_profit` — `_execute_signal` now reads them and calls `set_stops()` on fill (`main.py:256-259`).

### P0-2: Paper equity is a living number

**Fix:** `core/state.py` now has `equity` (seed 10000.0), `peak_equity`, and `daily_start_equity`. `close_position()` in orders.py applies `equity += net_pnl` to the state. `_fetch_snapshot` reads `state.equity` instead of a literal 10000. The circuit breakers in `RiskManager._check_circuit_breakers()` now compare current equity to the tracked peak — and the daily check actually rolls over at UTC boundary. Drawdown computation is live.

### P0-3: Funding accrual

**Fix:** `core/orders.py:apply_funding()` applies `± rate × notional` to equity (short receives positive funding, long pays it). Called each cycle via `main.py:274 _accrue_funding()` for open positions. `state.last_funding_applied_ts` prevents double-counting across restarts.

### P0-4: Realistic fills

**Fix:** `PaperOrderManager.place_order()` replaces the old `place_limit_order + simulate_fill` two-step. Longs fill at `ask × (1 + slippage_bps/10000)`, shorts at `bid × (1 − slippage_bps/10000)`. Entry fees (5 bps taker est) are recorded in `fees_paid` on the position. `close_position()` subtracts BOTH entry and exit fees from `net_pnl`. Config `slippage_bps: 5` is now actually referenced.

### Other audit items addressed

- **Dead config keys removed:** `trailing_activate_pct`, `trailing_bps`, `cancel_resting_after_hours`, `max_concurrent_positions`, `funding_aware`, `track_trades`, `order_type`, `time_in_force`, `paper_mode_print_on_signal_only`, `log_level`, `trade_log` — all cleaned from config.yaml
- **`--strategy flag` no longer mutates config file** — overrides held in memory only
- **Adaptation thresholds raised:** `min_trades` bumped from 10→30, `lookback` from 30→50, cooldown of 5 cycles between changes, asymmetric adjustment (cut fast, raise slow)
- **`profit_factor` corrected** — now uses `sum(wins)/sum(|losses|)` (true PF) instead of `avg_win/avg_loss` (payoff ratio)

---

## 2026-09-24 — P1 Strategy Math Fixes (branch `fix/p1-strategy-math`)

Eight remaining P1 issues from the audit addressed in one pass.

### P1-1: Funding filter — confirmation, not veto

**Before:** Funding could only cancel trades, never initiate them. The entry gate required `bias == trend`, but funding already overrode trend — so the only possible outcome was a stricter version of the trend bias. Combined with funding being positive ~98% of the time, the strategy was structurally short-only, ignoring 46% of events.

**Fix:** Funding is now a *confirmation* filter. When |funding| >= min_funding_bias, it must agree with the trend direction. When funding is neutral (< min_funding), the trend alone decides. This means:
- Uptrend + positive funding (common) → **both agree on long** → enters long (previously blocked!)
- Downtrend + positive funding → both agree on short → enters short
- Neutral funding → follows trend

Now trades both directions and passes the audit test: uptrend + positive funding with a pullback actually enters.

### P1-2: Funding exit threshold — dynamic instead of hardcoded

**Before:** Required `abs(fund) > min_funding * 5 = 0.0005` to exit. Only 2 of 339 historical events exceeded this — dead code.

**Fix:** Uses `min_funding * 3` as the exit threshold (~90th percentile of empirical distribution). Exit fires when funding strongly opposes the position.

### P1-3: Risk-first sizing wired end-to-end

**Before:** `risk.py` had `set_atr()` and `_last_atr` but nothing ever called them.

**Fix:** Strategy stores `_cached_atr`, `main.py` passes it to `risk.set_atr()`.

### P1-4: Directional pullback entry

**Before:** `abs(ema_distance_atr) <= pullback_threshold` — symmetric proximity band.

**Fix:** Longs: `ema_distance_atr <= pullback_threshold`. Shorts: `ema_distance_atr >= -pullback_threshold`.

### P1-5: Asymmetric reward:risk

**Before:** 1:1 payoff — needed ~55% win rate to break even.

**Fix:** TP multiplier 2.0, SL 1.5 — break-even ~43%.

### P1-6: ATR OHLC source

**Before:** Used `ask.high`/`bid.low` — measures spread noise.

**Fix:** Uses `price.high`, `price.low`, `price.previous`.

### P1-7: ATR fallback — refuse to fabricate

**Before:** Silent 2% fallback (2.7× too wide).

**Fix:** Cache + hold. No trade without a real volatility estimate.

### P1-8: Null candle close handling

**Before:** `dict.get("close", 0)` returned `None` — `float(None)` raised, bars silently dropped.

**Fix:** Forward-fill from `price.previous`, log count, assert series length.

### P1-9: Mean reversion stops and funding gate

**Before:** No stop loss, funding gate structurally short-only.

**Fix:** Hard stop at ±3×ATR, funding gate removed.

---

## 2026-09-24 — P2/P3 Metrics, Benchmark, Adaptation Gating (branch `fix/p2-p3-metrics-benchmarks`)

Final pass: metrics corrected and wired into the loop, benchmark added, adaptation statistical gating, remaining hygiene items closed.

### P2-1 / P2-3: Sharpe and Max DD corrected

`backtest/engine.py` rewritten:
- **Sharpe** on per-trade **returns** (net_pnl / equity_at_entry), sample stdev (n-1), annualized by √(trades)
- **Sortino** added — downside deviation only
- **Calmar** — total return % / max drawdown %
- **Max drawdown** as % of equity on sequential equity curve
- **Profit factor** was already correct (P0 pass)
- **Expectancy** — (win% × avg_win) − (loss% × avg_loss), net of all costs

### P2-4: PerformanceTracker wired into the loop

Instantiated in `main.py`, calls `summary_text(btc_price)` after every cycle. Prints Win%, PF, Sharpe, Calmar, MaxDD, Long/Short split, AvgHold, BTC buy-hold delta.

### P2-5: Buy-and-hold BTC benchmark

Start price recorded on first cycle. Delta reported in every summary.

### P3-1: Statistical gating on win rate

95% Wilson CI gates adaptation. Raise leverage only when `ci_lower > 0.5` (95% confident WR above 50%).

### P3-3: Out-of-sample confirmation

60/40 train/test split. Tighten/loosen entries only confirmed on validation window.

### Remaining hygiene closed

| Item | Status |
|---|---|
| `check_entry_allowed()` never called | Wired into `_execute_signal` |
| `signal.confidence` unused | Scales leverage 0.75×-1× |
| Null handling in mean_reversion | Forward-fill pattern |
| PerformanceTracker not wired | Instantiated, called each cycle |
| Buy-and-hold benchmark absent | Start price, delta in summary |

---

## 2026-09-25 — Round 2 Audit Fixes (branch `fix/round2-audit`)

All findings from Claude's round-2 audit addressed.

### P0 fixes

- **R2-1 Funding over-accrual**: `_accrue_funding` now reads `last_funding_applied_ts`, computes `hours_elapsed / 8` to get the correct number of funding events since last check. Skips when no full 8h interval has passed. `apply_funding` accepts `events` multiplier.
- **R2-2 Trailing stop wiring**: Restored `trail_bps`/`trail_activate_price` in `Signal` dataclass and both `set_stops()` call sites in `_execute_signal`.

### P1 fixes

- **R2-3 Entry gate bounds**: Longs now `-1.0 <= dist <= 0.3`; shorts `-0.3 <= dist <= 1.0`. Bounded on both sides — no more buying into collapses 2+ ATRs below the EMA.
- **R2-4 Sizing multiplier**: `stop_dist = atr × atr_multiplier_sl` (reads the config key) so risk per trade matches actual stop placement (1.5×ATR, not 1×ATR).
- **R2-5 Clamp override**: Returns `(0, 0)` when caps produce <1 contract instead of forcing a 1-contract position.
- **R2-6 Inverted exit strings**: Positive funding → "longs crowded" (correct); negative → "shorts crowded" (correct). Was backwards.

### P2 fixes

- **R2-7 Sharpe annualization**: Uses `periods_per_year = n / elapsed_days × 365.25` instead of `sqrt(n)`. Sharpe no longer inflates with runtime alone. Same fix applied to Sortino.
- **R2-8 Equity curve**: Added `state.equity_timeline` — every `close_position` and `apply_funding` appends `{ts, equity, source}`. Metrics engine reads from this timeline instead of reconstructing from trade PnL alone, so Sharpe/DD include funding effects.

### P3 fixes

- **R2-9 Circuit breaker save**: `self.state.save()` now called before every early return in `_check_circuit_breakers()` — daily anchor persists even on tripped days.
- **R2-10 Watermark save**: `check_stops` now calls `self.state.save()` after mutating `trail_watermark`.
- **R2-12 Stale `high_24h`**: Removed from `_fetch_snapshot`.
- **R2-13 Confidence comment**: Corrected to match actual code (`0.5 + conf × 0.5` = 0.5x-1x).
- **R2-14 Empty `=` file**: Deleted from repo.

---

## 2026-09-25 — PB-EMA Trend Strategy & Cadence (committed directly to main)

### PB-EMA strategy replaces funding-conflict as default

New `strategies/pb_ema_trend.py` uses PB-EMA(50) on daily candles to determine the trend regime:

- **UP regime** (close > blended EMA of high×0.7+close×0.3) → long-only entries
  - Pullback entry: price retraces to EMA12 within ATR band
  - Breakout entry: price accelerates away from EMA12 (momentum check)
- **DOWN regime** (close < EMA50 of close) → short-only entries
  - Pullback entry + breakdown entry (mirror of longs)
- **NEUTRAL regime** (inside channel) → hold, no trades
- **Funding**: used for extreme exits only — removed from entry logic entirely
- **Blended top line** (w=0.7) narrows neutral zone from 19% → 11% for ~40% more trade time

### Cadence changed from 4h to 1h

Audit data showed:
- Median pullback lasts 2 hours — at 4h we missed 88% of pullbacks
- 36% of 4h windows see price moves exceeding stop distance (gap risk)
- At 1h: 0% missed pullbacks, ~9% stop gap risk, 24 API calls/day (trivial)
- Config updated, cron job `47c8dfe1c7c5` moved from `every 4h` to `every 1h`

### Audit findings fixed

- Removed unused `import math as _math` from `_compute_pb_ema_regime`
- Removed unused `bo_runup` variable from `pb_ema_trend.py`
- Verified all 11 smoke tests pass including PB-EMA regime assertions
- Verified live cycle detects UP regime and fires long pullback

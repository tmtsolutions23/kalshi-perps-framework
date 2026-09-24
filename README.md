# Kalshi BTC Perps Trading Framework

A self-healing, self-iterating trading framework for the Kalshi Bitcoin perp
market (KXBTCPERP), built to run standalone — **zero LLM tokens at runtime**.

## Philosophy

- **Standalone loop**: runs as a cronjob or background process. No LLM calls in the hot path.
- **Self-healing**: crash recovery via persistent state, API retry with backoff, circuit breakers.
- **Self-iterating**: the strategy adapts its own parameters based on realized trade performance.
- **Strategy-agnostic**: swap strategies with a one-line config change. Add new ones by dropping a file.
- **Paper-first**: mode=paper simulates entries/exits with a virtual $10k and notifies Discord.
  Flip `mode: live` once Kalshi perps access is enabled on your account.

## Quick Start

```bash
cd kalshi-perps
python main.py            # run one evaluation cycle
python main.py --loop     # run forever (default 240 min interval)
python main.py --debug    # verbose logging
```

Config: edit `config.yaml`. All key knobs are there:
- `mode: paper | live`
- `strategy.name: funding_momentum | mean_reversion`
- `risk.max_leverage: 4.0` (hard cap), `min_leverage: 2.0`
- `schedule.check_interval_minutes: 240`

## Architecture

```
kalshi-perps/
├── main.py                 # CLI + orchestration loop
├── run.sh                  # cron-friendly entry point
├── config.yaml             # all settings
├── core/
│   ├── auth.py             # RSA-PSS signed requests to Kalshi perps API
│   ├── market.py           # candles, orderbook, funding, positions, balance
│   ├── state.py            # JSON state persistence (crash recovery)
│   ├── risk.py             # sizing, drawdown circuit breakers, SL/TP math
│   └── orders.py           # paper order simulation (flip to live later)
├── strategies/
│   ├── base.py             # Strategy interface: evaluate(snapshot) -> Signal
│   ├── funding_momentum.py # EMA trend + funding filter + ATR stops
│   └── mean_reversion.py   # Bollinger-style z-score mean reversion
├── alerts/
│   └── discord.py          # alert dispatch (stdout => cron => Discord)
├── backtest/
│   └── engine.py           # performance metrics from trade history
└── data/                   # runtime state, logs, trade history (gitignored)
```

## Adding a Strategy

Create `strategies/my_strategy.py`:

```python
from strategies.base import BaseStrategy, Signal, MarketSnapshot

class MyStrategy(BaseStrategy):
    def evaluate(self, snapshot: MarketSnapshot) -> Signal:
        # snapshot has: current_price, candles_1h, funding_rate,
        #               available_balance, current_position, live_params, ...
        if <entry condition>:
            return Signal("enter_long", confidence=0.7, reason="...",
                          suggested_leverage=3.0,
                          suggested_stop_loss=..., suggested_take_profit=...)
        return Signal("hold", reason="...")
```

Then set `strategy.name: my_strategy` in config.yaml. Done.

## Self-Healing

| Failure mode | Recovery |
|---|---|
| API call fails | Exponential backoff retry ×3, then error counter |
| 5+ consecutive errors | Auto-pause trading + Discord alert |
| Process crash mid-cycle | `data/state.json` restores position/pending order on restart |
| State file corrupted | Falls back to defaults, logs warning |
| Price feed invalid ($0) | Skips cycle, records error |
| Drawdown breach | Circuit breaker blocks new entries (total or daily) |
| Kalshi perps not enabled | Fetches market data fine; entries skipped gracefully |

## Self-Iteration (Adaptive)

After ≥10 completed trades, the framework evaluates the last 30:
- Win rate < 35% → cut leverage by 0.5x (floor 2x)
- Win rate > 65% → raise leverage by 0.5x (cap 4x)
- Profit factor < 0.8 → tighten pullback entry threshold
- Profit factor > 2.0 → loosen it

Adapted params live in `data/state.json` under `params`, so they persist and
are re-read each cycle. Disable with `performance.adapt_params: false`.

## API Endpoints Used

| Purpose | Endpoint |
|---|---|
| Market data | `GET /trade-api/v2/margin/markets/{ticker}` |
| Candles | `GET /trade-api/v2/margin/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=60` |
| Funding estimate | `GET /trade-api/v2/margin/funding_rates/estimate?ticker=` |
| Orderbook | `GET /trade-api/v2/margin/markets/{ticker}/orderbook` |
| Balance | `GET /trade-api/v2/margin/portfolio/balance` (auth) |
| Positions | `GET /trade-api/v2/margin/positions` (auth) |

## Live Mode Upgrade Path

1. Apply for perps access at kalshi.com/perps (margin account application)
2. Verify with: `python -c "from core.auth import KalshiAuth; ...; print(auth.check_enabled())"`
3. Set `mode: live` — the same orders that paper mode simulates will be placed
   via `POST /margin/orders`, with TP/SL brackets via the exit-trigger endpoints.

## Discord Alerts

Alerts print to stdout as `[LEVEL] [TS] message`. When run via the Hermes
cron `no_agent` pattern, empty stdout = silent, non-empty = delivered to
Discord alerts via Hermes cron `no_agent` pattern. See [PROGRESS.md](PROGRESS.md)
for the full changelog and project evolution.
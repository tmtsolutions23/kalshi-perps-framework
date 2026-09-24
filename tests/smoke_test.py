"""
Integration smoke test: verifies the full paper cycle
data fetch → strategy → paper entry → paper exit → trade history.
Run: python tests/smoke_test.py
"""

import sys, os, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.auth import KalshiAuth
from core.market import MarketData
from core.state import StateManager
from core.risk import RiskManager
from core.orders import PaperOrderManager
from strategies.base import MarketSnapshot
from strategies.funding_momentum import FundingMomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from alerts.discord import AlertDispatcher

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = yaml.safe_load(open(os.path.join(BASE, "config.yaml")))

def make_candles(prices):
    candles = []
    for i, p in enumerate(prices):
        candles.append({
            "price": {"open": p - 0.001, "high": p + 0.005, "low": p - 0.005, "close": p, "mean": p},
            "ask": {"open": p, "high": p + 0.005, "low": p - 0.005, "close": p},
            "bid": {"open": p, "high": p + 0.005, "low": p - 0.005, "close": p},
            "end_period_ts": 1700000000 + i * 3600,
            "volume": "1000.00",
        })
    return candles

def synthetic_uptrend_pullback():
    """200 rising candles then a small pullback — should trigger enter_long."""
    prices = [8.0 + i * 0.002 for i in range(200)]
    for j in range(3):
        prices[-1 - j] -= 0.02 - j * 0.005
    return prices

def main():
    k = CFG["kalshi"]
    auth = KalshiAuth(k["key_config"], k["key_path"], k["api_base"])
    md = MarketData(auth)

    # ── 1. Live data fetch (public endpoints) ──────────────────────────
    print("== 1. Live data ==")
    market = md.get_market("KXBTCPERP")
    live_price = float(market.get("market", market).get("price", 0))
    candles = md.get_candlesticks("KXBTCPERP", 60, 200).get("candlesticks", [])
    fund = md.get_funding_rate_estimate("KXBTCPERP").get("funding_rate")
    print(f"  price={live_price:.4f} candles={len(candles)} funding={fund}")
    assert live_price > 0, "live price failed"
    assert len(candles) > 50, "candles failed"

    # ── 2. Strategy on synthetic uptrend-pullback ──────────────────────
    print("== 2. FundingMomentum on synthetic uptrend+funding ==")
    state = StateManager("/tmp/kalshi_perps_smoke.json")
    if os.path.exists("/tmp/kalshi_perps_smoke.json"):
        os.remove("/tmp/kalshi_perps_smoke.json")
    state.load()

    strat = FundingMomentumStrategy(CFG["strategy"]["params"])
    prices = synthetic_uptrend_pullback()
    snap = MarketSnapshot(
        ticker="KXBTCPERP",
        current_price=prices[-1],
        bid=prices[-1] - 0.0001, ask=prices[-1] + 0.0001,
        mark_price=prices[-1],
        candles_1h=make_candles(prices),
        funding_rate=-0.0005,  # negative funding → long bias
        next_funding_ts=None,
        available_balance=10000.0,
        current_position=None,
        current_leverage_estimate=4.0,
        live_params={},
        recent_trades=[],
    )
    sig = strat.evaluate(snap)
    print(f"  signal={sig.action} — {sig.reason}")
    assert sig.action == "enter_long", f"expected enter_long, got {sig.action}"

    # ── 3. Risk sizing ─────────────────────────────────────────────────
    print("== 3. Risk sizing ==")
    risk = RiskManager(CFG, state)
    count, lev = risk.compute_position_size(10000.0, prices[-1], sig.suggested_leverage or 4.0)
    print(f"  notional=$10,000 @ {lev}x → {count} contracts")
    assert count > 0 and lev <= 4.0

    # ── 4. Paper entry + exit ──────────────────────────────────────────
    print("== 4. Paper entry → exit ==")
    orders = PaperOrderManager(auth, md, state)
    orders.place_limit_order("KXBTCPERP", "bid", count, prices[-1])
    pos = orders.simulate_fill(prices[-1], count)
    assert pos and state.get().get("current_position"), "position not opened"
    print(f"  opened {pos['side']} {pos['size']} @ {pos['entry_price']:.4f}")

    trade = orders.simulate_exit(prices[-1] + 0.10, count)  # profitable exit
    assert trade and len(state.get().get("trade_history", [])) == 1, "trade not logged"
    print(f"  exit pnl=${trade['net_pnl']:.2f}, history={len(state.get()['trade_history'])}")

    # ── 5. Self-adaptation after 10 trades ─────────────────────────────
    print("== 5. Self-adaptation ==")
    for i in range(12):
        pnl = 0.05 if i % 3 != 0 else -0.10  # ~67% win rate
        state.add_trade({"ticker": "KXBTCPERP", "side": "long", "net_pnl": pnl})
    state.save()
    # Re-run adaptation logic (simulate main.py's _adapt_strategy)
    trades = state.get().get("trade_history", [])[-30:]
    wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
    win_rate = wins / len(trades) if trades else 0
    print(f"  win_rate={win_rate:.0%} trades={len(trades)}")
    params = state.get().setdefault("params", {})
    if win_rate > 0.65 and len(trades) >= 10:
        current = float(params.get("leverage", CFG["strategy"]["params"].get("max_leverage", 4.0)))
        params["leverage"] = min(4.0, current + 0.5)
        state.save()
        print(f"  adapted leverage -> {params['leverage']}x")

    # ── 6. Mean reversion sanity ───────────────────────────────────────
    print("== 6. MeanReversion on overbought ==")
    mr = MeanReversionStrategy({"entry_zscore": 2.0, "exit_zscore": 0.2, "max_leverage": 3.0})
    spike = [8.0 + i * 0.001 for i in range(200)]
    spike[-1] = spike[-2] * 1.05  # big spike
    snap2 = MarketSnapshot(
        ticker="KXBTCPERP", current_price=spike[-1],
        bid=spike[-1], ask=spike[-1], mark_price=spike[-1],
        candles_1h=make_candles(spike),
        funding_rate=0.0006,  # positive funding → short bias
        next_funding_ts=None,
        available_balance=10000.0, current_position=None,
        current_leverage_estimate=3.0, live_params={}, recent_trades=[],
    )
    sig2 = mr.evaluate(snap2)
    print(f"  signal={sig2.action} — {sig2.reason}")
    assert sig2.action == "enter_short", f"expected enter_short, got {sig2.action}"

    print("\n✅ ALL SMOKE TESTS PASSED")

if __name__ == "__main__":
    main()
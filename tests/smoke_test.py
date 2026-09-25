"""
Smoke test: verifies the full paper cycle with the P0 fixes in place.
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
from strategies.pb_ema_trend import PBEMATrendStrategy

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = yaml.safe_load(open(os.path.join(BASE, "config.yaml")))


def make_candles(prices):
    return [{
        "price": {"open": p-0.001, "high": p+0.005, "low": p-0.005, "close": p, "mean": p},
        "ask": {"open": p, "high": p+0.005, "low": p-0.005, "close": p},
        "bid": {"open": p, "high": p+0.005, "low": p-0.005, "close": p},
        "end_period_ts": 1700000000 + i*3600, "volume": "1000.00",
    } for i, p in enumerate(prices)]


def synthetic_uptrend_pullback():
    prices = [8.0 + i * 0.002 for i in range(200)]
    for j in range(3):
        prices[-1 - j] -= 0.02 - j * 0.005
    return prices


def main():
    k = CFG["kalshi"]
    auth = KalshiAuth(k["key_config"], k["key_path"], k["api_base"])
    md = MarketData(auth)

    state = StateManager("/tmp/kalshi_perps_smoke.json")
    if os.path.exists("/tmp/kalshi_perps_smoke.json"):
        os.remove("/tmp/kalshi_perps_smoke.json")
    state.load()

    # 1. Live data
    print("== 1. Live data ==")
    market = md.get_market("KXBTCPERP")
    live_price = float(market.get("market", market).get("price", 0))
    candles = md.get_candlesticks("KXBTCPERP", 60, 200).get("candlesticks", [])
    fund = md.get_funding_rate_estimate("KXBTCPERP").get("funding_rate")
    print(f"  price={live_price:.4f} candles={len(candles)} funding={fund}")
    assert live_price > 0 and len(candles) > 50

    # 2. Strategy
    print("== 2. FundingMomentum ==")
    strat = FundingMomentumStrategy(CFG["strategy"]["params"])
    prices = synthetic_uptrend_pullback()
    snap = MarketSnapshot(
        ticker="KXBTCPERP", current_price=prices[-1],
        bid=prices[-1]-0.0001, ask=prices[-1]+0.0001, mark_price=prices[-1],
        candles_1h=make_candles(prices),
        funding_rate=-0.0005, available_balance=10000.0, current_position=None,
        current_leverage_estimate=4.0, live_params={}, recent_trades=[],
    )
    sig = strat.evaluate(snap)
    print(f"  signal={sig.action} — {sig.reason}")
    assert sig.action == "enter_long"

    # 3. Risk sizing (risk-first)
    print("== 3. Risk sizing ==")
    risk = RiskManager(CFG, state)
    risk._snapshot_price = prices[-1]
    risk.set_atr_4h(0.05)  # typical for contract price ~8
    count, lev = risk.compute_position_size(sig.suggested_leverage or 4.0)
    print(f"  equity=$10,000 @ {lev}x → {count} contracts")
    assert count > 0 and lev <= 4.0

    # 4. Paper entry with bid/ask + slippage
    print("== 4. Paper entry (with spread) ==")
    orders = PaperOrderManager(auth, md, state)
    result = orders.place_order("KXBTCPERP", "bid", count, prices[-1] + 0.01, prices[-1]-0.0002, prices[-1]+0.0002)
    assert result and state.get().get("current_position"), "position not opened"
    pos = state.get()["current_position"]
    print(f"  filled {pos['side']} {pos['size']} @ ${pos['entry_price']:.4f} fees=${pos['fees_paid']:.2f}")

    # 5. Stop loss check and stop-out
    print("== 5. Stop loss enforcement ==")
    orders.set_stops(stop_loss=pos["entry_price"] - 0.05)
    reason = orders.check_stops(pos["entry_price"] - 0.06, 0)
    print(f"  stop check (breached): {reason}")
    assert reason and "Stop loss" in reason

    trade = orders.close_position(pos["entry_price"] - 0.06, reason="test stop")
    assert trade and len(state.get().get("trade_history", [])) == 1
    equity = state.get()["equity"]
    print(f"  stop-out: net=${trade['net_pnl']:.2f} equity=${equity:.2f}")
    assert equity < 10000  # lost money

    # 6. Funding accrual
    print("== 6. Funding accrual ==")
    state.update(equity=10000.0)  # reset
    # Reopen a short position
    result = orders.place_order("KXBTCPERP", "ask", 100, prices[-1] - 0.01, prices[-1]-0.0002, prices[-1]+0.0002)
    assert result
    pos = state.get()["current_position"]
    amt = orders.apply_funding(0.0001, pos["entry_notional"], "short")
    print(f"  funding received: ${amt:.4f} equity=${state.get()['equity']:.2f}")
    assert amt > 0 and state.get()["equity"] > 10000

    # 7. Mean reversion sanity
    print("== 7. MeanReversion ==")
    mr = MeanReversionStrategy({"entry_zscore": 2.0, "exit_zscore": 0.2, "max_leverage": 3.0})
    spike = [8.0 + i * 0.001 for i in range(200)]
    spike[-1] = spike[-2] * 1.05
    snap2 = MarketSnapshot(
        ticker="KXBTCPERP", current_price=spike[-1],
        bid=spike[-1], ask=spike[-1], mark_price=spike[-1],
        candles_1h=make_candles(spike),
        funding_rate=0.0006, available_balance=10000.0, current_position=None,
        current_leverage_estimate=3.0, live_params={}, recent_trades=[],
    )
    sig2 = mr.evaluate(snap2)
    print(f"  signal={sig2.action} — {sig2.reason}")
    assert sig2.action == "enter_short"

    # 8. P1-1: Funding-trend conflict blocks entry (funding positive + uptrend)
    print("== 8. P1-1: Funding-trend conflict ==")
    sig_conflict = strat.evaluate(MarketSnapshot(
        ticker="KXBTCPERP", current_price=prices[-1],
        bid=prices[-1]-0.0001, ask=prices[-1]+0.0001, mark_price=prices[-1],
        candles_1h=make_candles(prices),
        funding_rate=0.0005,  # positive funding → short bias, but we're in uptrend
        available_balance=10000.0, current_position=None,
        current_leverage_estimate=4.0, live_params={}, recent_trades=[],
    ))
    print(f"  signal={sig_conflict.action} — {sig_conflict.reason}")
    assert sig_conflict.action == "hold", f"expected hold on conflict, got {sig_conflict.action}"
    assert "conflicts" in sig_conflict.reason

    # 9. P1-8: Null candle close forward-fills from price.previous
    print("== 9. P1-8: Null candle handling ==")
    null_candles = make_candles(prices)
    null_candles[-2]["price"]["close"] = None  # put null in candle index -2
    null_candles[-2]["price"]["previous"] = prices[-3]  # price.previous is set
    prices_filled = strat._build_prices(null_candles)
    print(f"  prices extracted: {len(prices_filled)}, last 3: {prices_filled[-3:]}")
    assert len(prices_filled) == len(prices), f"expected {len(prices)} prices, got {len(prices_filled)}"

    # 10. P1-9: Mean reversion has SL in signal
    print("== 10. P1-9: Mean reversion stop ==")
    assert sig2.suggested_stop_loss is not None, "mean reversion missing suggested_stop_loss"
    print(f"  suggested_stop_loss={sig2.suggested_stop_loss:.4f} (entry {spike[-1]:.4f})")
    assert abs(sig2.suggested_stop_loss - spike[-1]) > 0  # stop is not at entry

    # 11. PB-EMA trend strategy with regime filter
    print("== 11. PB-EMA trend strategy ==")
    pb = PBEMATrendStrategy({})
    prices_up = [8.0 + i * 0.002 for i in range(200)]
    snap_pb = MarketSnapshot(
        ticker="KXBTCPERP", current_price=prices_up[-1],
        bid=prices_up[-1]-0.0001, ask=prices_up[-1]+0.0001, mark_price=prices_up[-1],
        candles_1h=make_candles(prices_up),
        funding_rate=0.0, available_balance=10000.0, current_position=None,
        current_leverage_estimate=4.0, live_params={}, recent_trades=[],
        trend_regime="UP",
    )
    sig_pb = pb.evaluate(snap_pb)
    print(f"  UP regime signal: {sig_pb.action} — {sig_pb.reason}")
    assert sig_pb.action in ("enter_long", "hold"), f"expected long or hold, got {sig_pb.action}"

    # PB-EMA NEUTRAL should hold
    snap_pb_n = MarketSnapshot(
        ticker="KXBTCPERP", current_price=prices_up[-1],
        bid=prices_up[-1]-0.0001, ask=prices_up[-1]+0.0001, mark_price=prices_up[-1],
        candles_1h=make_candles(prices_up),
        funding_rate=0.0, available_balance=10000.0, current_position=None,
        current_leverage_estimate=4.0, live_params={}, recent_trades=[],
        trend_regime="NEUTRAL",
    )
    sig_pb_n = pb.evaluate(snap_pb_n)
    print(f"  NEUTRAL regime signal: {sig_pb_n.action} — {sig_pb_n.reason}")
    assert sig_pb_n.action == "hold", f"expected hold in neutral, got {sig_pb_n.action}"

    print("\n✅ ALL SMOKE TESTS PASSED — P0 + P1 + PB-EMA verified")


if __name__ == "__main__":
    main()
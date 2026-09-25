"""
Estimate optimal polling cadence for the framework.
"""
import sys, os, yaml, time
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from core.auth import KalshiAuth
from core.market import MarketData

cfg = yaml.safe_load(open(os.path.join(BASE, "config.yaml")))
k = cfg['kalshi']
auth = KalshiAuth(k['key_config'], k['key_path'], k['api_base'])
md = MarketData(auth)
now = int(time.time())

c1h = md.get_candlesticks('KXBTCPERP', 60, 500, start_ts=now - 21*86400).get('candlesticks', [])

def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

closes = []
for c in c1h:
    try: closes.append(float(c.get('price', {}).get('close', 0)))
    except: continue

# ATR(14)
atrs = []
for i in range(len(closes)):
    tr = max(closes[i]-min(closes[max(0,i-1)],closes[i]), abs(closes[i]-closes[i-1]) if i>0 else 0)
    if i == 0: atrs.append(tr)
    elif len(atrs) >= 14: atrs.append(atrs[-1] * 13/14 + tr/14)
    else: atrs.append((sum(atrs) + tr) / (len(atrs) + 1))

# Pullback duration analysis
fast_p = 12
pullback_hours = []
in_pull = False
start_hour = 0

for i in range(fast_p, len(closes)):
    fast_e = ema(closes[i-fast_p+1:i+1], fast_p)
    atr = atrs[i]
    dist = (closes[i] - fast_e) / atr if atr > 0 else 0
    in_band = -1.0 <= dist <= 0.3
    
    if in_band and not in_pull:
        start_hour = i
        in_pull = True
    elif not in_band and in_pull:
        pullback_hours.append(i - start_hour)
        in_pull = False

print(f"Pullback duration analysis ({len(pullback_hours)} events):")
if pullback_hours:
    sorted_h = sorted(pullback_hours)
    print(f"  Mean: {sum(pullback_hours)/len(pullback_hours):.1f}h")
    print(f"  Median: {sorted_h[len(pullback_hours)//2]}h")
    print(f"  Min: {min(pullback_hours)}h  Max: {max(pullback_hours)}h")
    missed_2h = sum(1 for h in pullback_hours if h < 2)
    missed_4h = sum(1 for h in pullback_hours if h < 4)
    print(f"  Shorter than 2h: {missed_2h}/{len(pullback_hours)} ({missed_2h/len(pullback_hours)*100:.0f}%)")
    print(f"  Shorter than 4h: {missed_4h}/{len(pullback_hours)} ({missed_4h/len(pullback_hours)*100:.0f}%)")

# Stop gap risk
mean_atr = sum(atrs[-14:]) / 14
sl_dist_pct = mean_atr * 1.5 / closes[-1] * 100
print(f"\nSL distance (1.5xATR): {sl_dist_pct:.2f}% of price")

for window_h in [2, 4, 8]:
    moves = []
    for i in range(0, len(closes) - window_h, window_h):
        move = abs(closes[i+window_h] - closes[i]) / closes[i] * 100
        moves.append(move)
    if moves:
        exceed = sum(1 for m in moves if m > sl_dist_pct)
        print(f"  {window_h}h windows exceeding SL: {exceed}/{len(moves)} ({exceed/len(moves)*100:.0f}%)")

# Missed entry cadence
print(f"\nEstimated missed entries:")
for cadence_h in [1, 2, 4, 8]:
    # At each cadence, we'd miss pullbacks shorter than the cadence
    if pullback_hours:
        missed = sum(1 for h in pullback_hours if h < cadence_h)
        print(f"  {cadence_h}h cadence: miss {missed}/{len(pullback_hours)} pullbacks ({missed/len(pullback_hours)*100:.0f}%)")
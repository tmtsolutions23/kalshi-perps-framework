"""
Resample 1h candles into higher-interval candles locally.
Kalshi API only supports 1/60/1440 minute periods, so 4h needs aggregation.
"""
import sys, os, yaml, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.auth import KalshiAuth
from core.market import MarketData

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = yaml.safe_load(open(os.path.join(BASE, "config.yaml")))
k = cfg['kalshi']
auth = KalshiAuth(k['key_config'], k['key_path'], k['api_base'])
md = MarketData(auth)
now = int(time.time())

c1h = md.get_candlesticks('KXBTCPERP', 60, 400, start_ts=now - 100*86400)
candles = c1h.get('candlesticks', [])

# Extract 1h candles with timestamps
bars = []
for c in candles:
    try:
        p = c.get('price', {})
        close = p.get('close')
        if close is None:
            continue
        bars.append({
            'ts': c.get('end_period_ts', 0),
            'open': float(p.get('open', 0)),
            'high': float(p.get('high', 0)),
            'low': float(p.get('low', 0)),
            'close': float(close),
        })
    except (TypeError, ValueError):
        continue

print(f'1h bars: {len(bars)}')

# Aggregate into 4h bars
agg = []
interval = 4
for i in range(0, len(bars) - interval + 1, interval):
    chunk = bars[i:i+interval]
    agg.append({
        'open': chunk[0]['open'],
        'high': max(c['high'] for c in chunk),
        'low': min(c['low'] for c in chunk),
        'close': chunk[-1]['close'],
        'ts': chunk[-1]['ts'],
    })

print(f'4h bars: {len(agg)}')

# ATR(14) on 4h
atrs = []
for i in range(len(agg)):
    c = agg[i]
    if i == 0:
        tr = c['high'] - c['low']
    else:
        prev_close = agg[i-1]['close']
        tr = max(c['high']-c['low'], abs(c['high']-prev_close), abs(c['low']-prev_close))
    if i == 0:
        atrs.append(tr)
    elif len(atrs) >= 14:
        atrs.append(atrs[-1] * 13/14 + tr/14)
    else:
        atrs.append((sum(atrs) + tr) / (len(atrs) + 1))

price = agg[-1]['close']
mean_atr = sum(atrs[-14:]) / 14
print(f'BTC price: ${price/0.0001:,.0f}')
print(f'ATR(14) on 4h: ${mean_atr:.4f} ({mean_atr/price*100:.2f}%)')
print(f'1.5x ATR stop: ${mean_atr*1.5/price*100:.2f}%')
print(f'2.0x ATR TP:   ${mean_atr*2.0/price*100:.2f}%')
print(f'3.0x ATR TP:   ${mean_atr*3.0/price*100:.2f}%')
print()
for risk_pct in [1, 2, 3, 5]:
    contracts = int(500 * risk_pct/100 / (mean_atr * 1.5))
    notional = contracts * price
    lev = notional / 500
    print(f'{risk_pct}% risk: {contracts} contracts = ${notional:.0f} notional ({lev:.1f}x)')
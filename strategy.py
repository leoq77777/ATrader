"""Only this file is mutable during autonomous research.

rank_assets receives past CLOSED daily bars and a copy of current quantities.
Return a score, eligibility, normalized exposure, reason and indicators per sector.
The fixed harness chooses holdings, enforces cash/risk limits and executes orders.
Allowed Python imports: math, statistics. No filesystem, network or process access.
"""
import math


class Strategy:
    name = 'sector_rotation_baseline_v1'
    description = '日线行业相对强弱与绝对趋势过滤；组合选取、换仓缓冲和资金约束由固定 harness 执行。'

    def __init__(self):
        self.params = {'momentum_short': 20, 'momentum_long': 60, 'trend_window': 100, 'volatility_window': 20}

    def rank_assets(self, history, positions):
        p = self.params
        benchmark = [b['close'] for b in history['SPY']]
        needed = max(p['momentum_long'] + 1, p['trend_window'], p['volatility_window'] + 1)
        market_ok = len(benchmark) >= needed and benchmark[-1] > sum(benchmark[-p['trend_window']:]) / p['trend_window']
        benchmark_momentum = benchmark[-1] / benchmark[-p['momentum_long']-1] - 1 if len(benchmark) >= needed else 0
        result = {}
        for symbol, bars in history.items():
            if symbol == 'SPY':
                continue
            closes = [b['close'] for b in bars]
            if len(closes) < needed:
                result[symbol] = {'score': 0, 'eligible': False, 'exposure': 0, 'reason': '历史不足，等待预热', 'indicators': {}}
                continue
            short = closes[-1] / closes[-p['momentum_short']-1] - 1
            long = closes[-1] / closes[-p['momentum_long']-1] - 1
            trend = sum(closes[-p['trend_window']:]) / p['trend_window']
            returns = [closes[i] / closes[i-1] - 1 for i in range(len(closes)-p['volatility_window'], len(closes))]
            mean = sum(returns) / len(returns)
            vol = math.sqrt(sum((r-mean)**2 for r in returns) / len(returns))
            score = (0.4 * short + 0.6 * long) / max(vol, 0.005)
            eligible = market_ok and short > 0 and long > 0 and closes[-1] > trend and score > 0
            reason = '趋势及动量满足，进入行业排名' if eligible else ('宽基趋势过滤，持有现金' if not market_ok else '行业趋势或动量不满足')
            result[symbol] = {
                'score': round(score, 6), 'eligible': eligible, 'exposure': min(1, 0.012 / max(vol, 0.005)),
                'reason': reason,
                'indicators': {'momentum_20d': round(short, 5), 'momentum_60d': round(long, 5),
                               'daily_volatility': round(vol, 5), 'relative_strength': round(long-benchmark_momentum, 5),
                               'trend_average': round(trend, 4)},
            }
        return result

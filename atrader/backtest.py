"""Fixed daily shared-cash engine. Decisions use prior closes, fills use next opens."""
import math
from copy import deepcopy
from atrader.portfolio import align_bars, allocate, apply_dividends, mark_equity, rebalance


def summarize(curve, trades, initial_cash, benchmark_curve, dividends=0):
    returns = [curve[i] / curve[i-1] - 1 for i in range(1, len(curve)) if curve[i-1] > 0]
    mean = sum(returns) / len(returns) if returns else 0
    variance = sum((r-mean)**2 for r in returns) / len(returns) if returns else 0
    sharpe = mean / math.sqrt(variance) * math.sqrt(252) if variance > 1e-18 else 0
    peak, drawdown = initial_cash, 0
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    final = curve[-1] if curve else initial_cash
    net_return = (final / initial_cash - 1) * 100
    benchmark_return = (benchmark_curve[-1] / initial_cash - 1) * 100 if benchmark_curve else 0
    return {'initial_cash': initial_cash, 'final_equity': round(final, 4), 'net_profit_usd': round(final-initial_cash, 4),
            'net_return_pct': round(net_return, 4), 'benchmark_return_pct': round(benchmark_return, 4),
            'alpha_pct': round(net_return-benchmark_return, 4), 'sharpe': round(sharpe, 4),
            'max_drawdown_pct': round(drawdown*100, 4), 'trade_count': len(trades),
            'total_fees_usd': round(sum(t['fee_usd'] for t in trades), 6),
            'total_slippage_usd': round(sum(t['slippage_usd'] for t in trades), 6),
            'dividend_income_usd': round(dividends, 6), 'equity_curve': [round(v, 4) for v in curve],
            'benchmark_curve': [round(v, 4) for v in benchmark_curve], 'recent_trades': trades[-30:]}


def simulate(strategy, market, cfg, start, end):
    symbols = list(cfg['watchlist']) + [cfg['benchmark_symbol']]
    days, series = align_bars(market, symbols)
    if not 1 <= start < end <= len(days):
        raise ValueError('Invalid scoring interval or missing warmup')
    initial = float(cfg.get('initial_paper_cash', 10000))
    book = {'cash': initial, 'positions': {}, 'orders': []}
    benchmark = {'cash': initial, 'positions': {}, 'orders': []}
    benchmark_cfg = dict(cfg, watchlist=[cfg['benchmark_symbol']], max_position_pct=1, max_gross_exposure=1)
    curve, benchmark_curve, income, weights = [initial], [initial], 0.0, []
    for idx in range(start, end):
        # Copies isolate candidate mutations; only past observations are accessible.
        history = {s: [dict(b) for b in rows[max(0, idx-252):idx]] for s, rows in series.items()}
        current = {s: rows[idx] for s, rows in series.items()}
        algorithm = strategy() if callable(strategy) else deepcopy(strategy)
        signals = algorithm.rank_assets(history, {s: p['qty'] for s, p in book['positions'].items()})
        plan = allocate(signals, {s: p['qty'] for s, p in book['positions'].items()}, cfg)
        weights.append([round(plan['weights'].get(s, 0), 6) for s in cfg['watchlist']])
        book, dividend = apply_dividends(book, current)
        income += dividend
        opens = {s: row['open'] for s, row in current.items()}
        book, _ = rebalance(book, plan['weights'], opens, cfg, current[cfg['benchmark_symbol']]['timestamp'], plan['reasons'])
        benchmark, _ = apply_dividends(benchmark, current)
        if idx == start:
            benchmark, _ = rebalance(benchmark, {cfg['benchmark_symbol']: 1.0}, opens, benchmark_cfg, current[cfg['benchmark_symbol']]['timestamp'])
        closes = {s: row['close'] for s, row in current.items()}
        curve.append(mark_equity(book, closes))
        benchmark_curve.append(mark_equity(benchmark, closes))
    result = summarize(curve, book['orders'], initial, benchmark_curve, income)
    result['start_date'], result['end_date'] = days[start], days[end-1]
    result['positions'] = book['positions']
    result['cash'] = book['cash']
    result['weight_path'], result['behavior_dates'] = weights, days[start:end]
    return result


def score(result):
    return (.5 * result['sharpe'] + .035 * result['net_return_pct']
            + .015 * result['alpha_pct'] - .04 * result['max_drawdown_pct'])


def evaluate(strategy_factory, market, cfg, audit=False, layout=None):
    days, _ = align_bars(market, list(cfg['watchlist']) + [cfg['benchmark_symbol']])
    n = len(days)
    if (layout or {}).get('common_sessions', n) < 800:
        raise ValueError('At least 800 common closed daily sessions are required')
    warmup = 252
    reserve = layout['reserved_sessions'] if layout else n // 5
    research_end = n if layout and not audit else n-reserve
    if layout and ((audit and (not layout.get('audit') or n != layout['common_sessions']))
                   or (not audit and n+reserve != layout['common_sessions'])):
        raise ValueError('Invalid research-only evaluation layout')
    if not 0<reserve<n or research_end<=252:
        raise ValueError('Invalid final holdout boundary')
    split = research_end // 2
    midpoint = (split+research_end)//2
    if audit:
        result = simulate(strategy_factory, market, cfg, research_end, n)
        return {'audit': result, 'used_for_promotion': False}
    train = simulate(strategy_factory, market, cfg, warmup, split)
    folds = [simulate(strategy_factory, market, cfg, split, midpoint), simulate(strategy_factory, market, cfg, midpoint, research_end)]
    full = simulate(strategy_factory, market, cfg, warmup, research_end)
    # Behavioral niches use validation only, on identical dates for all candidates.
    full['weight_path'] = [row for fold in folds for row in fold['weight_path']]
    full['behavior_dates'] = [day for fold in folds for day in fold['behavior_dates']]
    validation = sum(score(f) for f in folds) / len(folds)
    train_score = score(train)
    # Penalize a large gap; do not leak the reserved holdout into candidate ranking.
    result_score = .35*train_score + .65*validation - .25*max(0, train_score-validation-.35)
    return {'ratchet_score': round(result_score, 6), 'train_score': round(train_score, 6),
            'validation_score': round(validation, 6), 'validation_folds': folds,
            'portfolio': full, 'holdout': {'reserved_sessions': reserve, 'used_for_promotion': False},
            'scoring_sessions': {'train': [warmup, split], 'validation': [split, research_end]}, 'common_sessions': n+reserve if layout else n}

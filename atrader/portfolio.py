"""Fixed shared-account allocation and backtest fill engine; Alpaca confirms online paper fills."""
import math
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

POLICY_VERSION = 'shared_portfolio_daily_v1'
NEW_YORK = ZoneInfo('America/New_York')


def session_date(timestamp):
    return datetime.fromtimestamp(timestamp, NEW_YORK).date().isoformat()


def align_bars(market, symbols):
    if not set(symbols) <= set(market):
        raise ValueError('Missing universe or benchmark bars')
    indexed = {}
    for symbol in symbols:
        rows = {}
        for bar in market[symbol]:
            day = bar.get('session_date') or session_date(bar['timestamp'])
            if day in rows:
                raise ValueError('Duplicate daily bar for ' + symbol)
            for key in ('open', 'high', 'low', 'close'):
                if not math.isfinite(float(bar[key])) or float(bar[key]) <= 0:
                    raise ValueError('Invalid OHLC price')
            rows[day] = dict(bar, session_date=day)
        indexed[symbol] = rows
    days = sorted(set.intersection(*(set(rows) for rows in indexed.values())))
    return days, {s: [indexed[s][d] for d in days] for s in symbols}


def allocate(signals, quantities, cfg):
    universe = cfg['watchlist']
    if set(signals) != set(universe):
        raise ValueError('Strategy must score exactly the fixed tradable universe')
    clean = {}
    for symbol, item in signals.items():
        if not isinstance(item, dict) or not isinstance(item.get('eligible'), bool):
            raise ValueError('Invalid strategy signal schema')
        score, exposure = float(item['score']), float(item.get('exposure', 1))
        if not math.isfinite(score) or not math.isfinite(exposure) or not 0 <= exposure <= 1:
            raise ValueError('Invalid score or exposure')
        indicators = item.get('indicators', {})
        if not isinstance(indicators, dict) or len(indicators) > 30:
            raise ValueError('Invalid indicators')
        indicators = {str(k)[:60]: float(v) for k, v in indicators.items()}
        if not all(math.isfinite(v) for v in indicators.values()):
            raise ValueError('Non-finite indicator')
        clean[symbol] = {'score': score, 'exposure': exposure, 'eligible': item['eligible'],
                         'reason': str(item.get('reason', ''))[:2000], 'indicators': indicators}
    ranked = sorted((s for s in universe if clean[s]['eligible'] and clean[s]['score'] > 0), key=lambda s: (-clean[s]['score'], s))
    n = int(cfg.get('max_holdings', 3))
    buffer = int(cfg.get('rank_buffer', 1))
    if not 1 <= n <= len(universe) or not 0 <= buffer <= len(universe):
        raise ValueError('Invalid fixed portfolio limits')
    # Retain eligible incumbents inside the exit buffer, then fill vacancies by rank.
    retained = [s for s in ranked[:n+buffer] if quantities.get(s, 0) > 1e-9][:n]
    selected = retained + [s for s in ranked if s not in retained][:n-len(retained)]
    cap = float(cfg.get('max_position_pct', .25))
    gross = float(cfg.get('max_gross_exposure', .75))
    if not 0 < cap <= gross <= 1:
        raise ValueError('Invalid position or gross exposure limit')
    weights = {s: (cap * clean[s]['exposure'] if s in selected else 0.0) for s in universe}
    scale = min(1, gross / max(sum(weights.values()), 1e-12))
    weights = {s: w * scale for s, w in weights.items()}
    ranks = {s: i+1 for i, s in enumerate(ranked)}
    reasons = {s: (clean[s].get('reason', '') + ('；入选组合（排名缓冲保留）' if s in retained else '；入选组合' if s in selected else '；未入选，退出或保持现金')) for s in universe}
    return {'weights': weights, 'ranks': ranks, 'selected': selected, 'reasons': reasons, 'signals': clean}


def mark_equity(book, prices):
    value = float(book['cash'])
    for symbol, pos in book.get('positions', {}).items():
        price = float(prices[symbol])
        if not math.isfinite(price) or price <= 0 or pos['qty'] < 0:
            raise ValueError('Invalid position or mark price')
        value += pos['qty'] * price
    return value


def rebalance(book, weights, prices, cfg, timestamp, reasons=None):
    """Execute all sells before buys, sharing one cash balance and one equity snapshot."""
    result = deepcopy(book)
    positions = result.setdefault('positions', {})
    allowed = set(cfg['watchlist'])
    if not set(weights) <= allowed:
        raise ValueError('Order target outside fixed universe')
    cap, gross = float(cfg.get('max_position_pct', .25)), float(cfg.get('max_gross_exposure', .75))
    if any(not math.isfinite(w) or w < 0 or w > cap + 1e-9 for w in weights.values()) or sum(weights.values()) > gross + 1e-9:
        raise ValueError('Invalid long-only portfolio weights')
    symbols = set(weights) | set(positions)
    if any(s not in prices or not math.isfinite(prices[s]) or prices[s] <= 0 for s in symbols):
        raise ValueError('Missing or invalid fill price')
    equity = mark_equity(result, prices)
    if equity <= 0:
        raise ValueError('Account has no positive equity')
    fee_rate, slip_rate = float(cfg.get('trading_fee_bps', 5)) / 10000, float(cfg.get('slippage_bps', 3)) / 10000
    if not 0 <= fee_rate < .1 or not 0 <= slip_rate < .1:
        raise ValueError('Invalid transaction costs')
    # Size against estimated POST-cost equity so fees cannot push a 25% target over its cap.
    net_equity = equity
    for _ in range(8):
        targets = {s: net_equity * weights.get(s, 0) for s in symbols}
        deltas = {s: targets[s] - positions.get(s, {}).get('qty', 0) * prices[s] for s in symbols}
        costs = sum(abs(delta) * (slip_rate + fee_rate * (1 + slip_rate if delta > 0 else 1 - slip_rate)) for delta in deltas.values())
        net_equity = max(0, equity - costs)
    targets = {s: net_equity * weights.get(s, 0) for s in symbols}
    deltas = {s: targets[s] - positions.get(s, {}).get('qty', 0) * prices[s] for s in symbols}
    trades = []
    def fill(symbol, qty, side):
        reference = prices[symbol]
        price = reference * (1 + slip_rate if side == 'BUY' else 1 - slip_rate)
        notional = qty * price
        fee = notional * fee_rate
        old = positions.get(symbol, {'qty': 0.0, 'avg_price': price})
        if side == 'BUY':
            result['cash'] -= notional + fee
            new_qty = old['qty'] + qty
            positions[symbol] = {'qty': new_qty, 'avg_price': (old['qty'] * old['avg_price'] + notional) / new_qty, 'last_price': reference}
        else:
            result['cash'] += notional - fee
            new_qty = max(0.0, old['qty'] - qty)
            if new_qty < 1e-9:
                positions.pop(symbol, None)
            else:
                positions[symbol] = dict(old, qty=new_qty, last_price=reference)
        trade = {'symbol': symbol, 'broker': 'local_paper', 'timestamp': int(timestamp), 'side': side,
                 'qty': qty, 'price': price, 'reference_price': reference, 'fee_usd': fee,
                 'slippage_usd': abs(qty * (price-reference)), 'target_weight': weights.get(symbol, 0),
                 'reason': (reasons or {}).get(symbol, '退出研究范围'), 'market_data_source': cfg.get('market_data_source', 'yahoo')}
        trades.append(trade)
    current_gross = sum(p['qty'] * prices[s] for s, p in positions.items()) / equity
    def due(symbol):
        weight = weights.get(symbol, 0)
        current = positions.get(symbol, {}).get('qty', 0) * prices[symbol]
        return (abs(deltas[symbol]) >= equity * .015 or (weight == 0 and current > 1e-8)
                or current / equity > cap + 1e-8 or current_gross > gross + 1e-8)
    for symbol in sorted(symbols):
        if deltas[symbol] < 0 and due(symbol):
            qty = min(positions.get(symbol, {}).get('qty', 0), -deltas[symbol] / prices[symbol])
            if qty > 1e-10:
                fill(symbol, qty, 'SELL')
    reserve = equity * (1 - gross)
    for symbol in sorted(symbols, key=lambda s: (-weights.get(s, 0), s)):
        if deltas[symbol] > 0 and due(symbol):
            unit_cost = prices[symbol] * (1 + slip_rate) * (1 + fee_rate)
            qty = min(deltas[symbol] / prices[symbol], max(0, result['cash'] - reserve) / unit_cost)
            if qty > 1e-10:
                fill(symbol, qty, 'BUY')
    if result['cash'] < -1e-7:
        raise AssertionError('Cash conservation violated')
    result.setdefault('orders', []).extend(trades)
    result['updated_at'] = int(timestamp)
    return result, trades


def apply_dividends(book, bars_by_symbol):
    """Cash dividends on ex-date, before rebalance; split-adjusted research units."""
    result = deepcopy(book)
    income = 0.0
    for symbol, pos in result.get('positions', {}).items():
        dividend = float(bars_by_symbol[symbol].get('dividend', 0))
        if dividend < 0 or not math.isfinite(dividend):
            raise ValueError('Invalid dividend')
        income += pos['qty'] * dividend
    result['cash'] += income
    return result, income

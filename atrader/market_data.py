"""Shared public market bars for paper execution, signals and research.

Yahoo quotes may be delayed. Live-source failures never fall back to synthetic data.
"""
import json
import math
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List
from datetime import datetime
from zoneinfo import ZoneInfo

_CACHE: Dict[str, Any] = {}
_LOCK = threading.RLock()


def get_recent_bars(symbol: str, limit: int, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    source = cfg.get('market_data_source', 'yahoo')
    if source == 'alpaca':
        from atrader.alpaca import daily_bars
        import hashlib
        identity = hashlib.sha256(cfg.get('alpaca_api_key', '').encode()).hexdigest()[:12]
        key = 'alpaca:' + identity + ':' + symbol
        with _LOCK:
            cached = _CACHE.get(key)
            if cached and time.time()-cached['fetched_at'] < 60 and len(cached['bars']) >= limit:
                return [dict(b) for b in cached['bars'][-limit:]]
            bars = daily_bars(symbol, limit, cfg)
            _CACHE[key] = {'fetched_at':time.time(),'bars':bars}
            return [dict(b) for b in bars]
    if source == 'synthetic':
        from atrader.evaluator import generate_multi_regime_bars
        rows = generate_multi_regime_bars(symbol, max(limit, 120))[-limit:]
        if cfg.get('market_interval') == '1d':
            rows = [dict(b, timestamp=int(time.time())-(len(rows)-i)*86400) for i, b in enumerate(rows)]
        return [{**b, 'source': 'synthetic', 'market_open': True, 'complete': True,
                 'quote_price': b['close'], 'market_timestamp': b['timestamp']} for b in rows]
    if source != 'yahoo':
        raise ValueError(f'Unsupported market data source: {source}')
    ticker = symbol
    if '/' in ticker:
        base, quote = ticker.split('/')
        if quote not in ('USD', 'USDT', 'USDC'):
            raise ValueError(f'Unsupported quote currency: {quote}')
        ticker = base + '-USD'
    if ticker.endswith('.US'):
        ticker = ticker[:-3]
    if ticker.endswith('.HK'):
        ticker = ticker[:-3].zfill(4) + '.HK'
    interval = cfg.get("market_interval", "1h")
    cache_key = ticker + ":" + interval
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached['fetched_at'] < 60:
            bars = cached['bars']
        else:
            url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(ticker, safe='')
            url += '?interval=' + interval + '&range=' + ('5y' if interval == '1d' else '6mo') + '&includePrePost=false&events=div%2Csplits'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 ATrader/0.1'})
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.load(response)
            result = payload.get('chart', {}).get('result')
            if not result:
                raise RuntimeError(f'No market data for {symbol}')
            result = result[0]
            meta = result.get('meta', {})
            market_ts = int(meta.get('regularMarketTime', 0))
            session = meta.get('currentTradingPeriod', {}).get('regular', {})
            crypto = '/' in symbol
            market_open = crypto or int(session.get('start', 0)) <= now < int(session.get('end', 0))
            max_age = 7200 if market_open else 7 * 86400
            if market_ts <= 0 or now - market_ts > max_age or market_ts > now + 300:
                raise RuntimeError(f'Stale market quote for {symbol}: timestamp={market_ts}')
            quote = result['indicators']['quote'][0]
            bars = []
            zone = ZoneInfo('America/New_York')
            def day_of(ts):
                return datetime.fromtimestamp(int(ts), zone).date().isoformat()
            events = result.get('events', {})
            dividends = {day_of(v['date']): float(v['amount']) for v in events.get('dividends', {}).values()}
            splits = {day_of(v['date']): float(v['numerator']) / float(v['denominator']) for v in events.get('splits', {}).values()}
            today = day_of(now)
            for i, ts in enumerate(result.get('timestamp', [])):
                values = {k: quote[k][i] for k in ('open', 'high', 'low', 'close', 'volume')}
                if any(v is None or not math.isfinite(float(v)) for v in values.values()):
                    continue
                if any(float(values[k]) <= 0 for k in ('open', 'high', 'low', 'close')):
                    continue
                bars.append({
                    'timestamp': int(ts), **{k: float(v) for k, v in values.items()},
                    'source': 'yahoo', 'source_symbol': ticker, 'interval': interval,
                    'session_date': day_of(ts),
                    'complete': day_of(ts) < today or (not market_open and now >= int(session.get('end', 0))),
                    'dividend': dividends.get(day_of(ts), 0), 'split_ratio': splits.get(day_of(ts), 1),
                    'quote_price': float(meta.get('regularMarketPrice', values['close'])),
                    'price_adjustment': 'split_adjusted',
                    'fetched_at': int(now), 'market_timestamp': market_ts, 'market_open': market_open,
                })
            if not bars or now - bars[-1]['timestamp'] > max_age:
                raise RuntimeError(f'Stale or empty market bars for {symbol}')
            _CACHE[cache_key] = {'fetched_at': now, 'bars': bars}
        if len(bars) < limit:
            raise RuntimeError(f'Insufficient market bars for {symbol}: {len(bars)} < {limit}')
        return [dict(bar) for bar in bars[-limit:]]

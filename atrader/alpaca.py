"""Alpaca PAPER transport and read-only IEX data. No live endpoint is accepted."""
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from atrader.portfolio import session_date

PAPER_URL = 'https://paper-api.alpaca.markets'
DATA_URL = 'https://data.alpaca.markets'


def timestamp(value):
    value = re.sub(r'(\.\d{6})\d+', r'\1', value)
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


class PaperClient:
    def __init__(self, cfg):
        base = cfg.get('alpaca_base_url', PAPER_URL).rstrip('/')
        if base not in (PAPER_URL, PAPER_URL+'/v2'):
            raise ValueError('Only the official HTTPS Alpaca PAPER endpoint is allowed')
        if not cfg.get('alpaca_api_key') or not cfg.get('alpaca_secret_key') or cfg['alpaca_api_key'].startswith('your_'):
            raise ValueError('Alpaca paper credentials are missing; no local account fallback')
        self.headers = {'APCA-API-KEY-ID':cfg['alpaca_api_key'], 'APCA-API-SECRET-KEY':cfg['alpaca_secret_key'], 'Content-Type':'application/json'}

    def request(self, path, method='GET', payload=None, data=False, missing_ok=False):
        allowed = path.startswith('/v2/') or (data and path.startswith('/v1/corporate-actions?'))
        if not allowed or (data and method != 'GET'):
            raise ValueError('Unsupported Alpaca request')
        request = urllib.request.Request((DATA_URL if data else PAPER_URL)+path,
            headers=self.headers, method=method, data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if getattr(response, 'status', None) == 204:
                    return None
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            raise RuntimeError(f'Alpaca paper API HTTP {exc.code}') from None
        except (OSError, ValueError):
            raise RuntimeError('Alpaca paper API unavailable or invalid response') from None


def daily_bars(symbol, limit, cfg):
    if cfg.get('market_interval', '1d') != '1d':
        raise ValueError('Alpaca execution uses daily bars only')
    client = PaperClient(cfg)
    clock = client.request('/v2/clock')
    now = timestamp(clock['timestamp'])
    start = datetime.fromtimestamp(now, timezone.utc)-timedelta(days=max(500,limit*2))
    params = {'timeframe':'1Day','start':start.isoformat(),'limit':10000,'feed':'iex','adjustment':'split','sort':'asc'}
    bars = []
    while True:
        result = client.request('/v2/stocks/'+urllib.parse.quote(symbol, safe='')+'/bars?'+urllib.parse.urlencode(params), data=True)
        bars.extend(result.get('bars') or [])
        token = result.get('next_page_token')
        if not token:break
        params['page_token'] = token
        if len(bars)>10000:raise ValueError('Unexpected Alpaca data pagination')
    latest = client.request('/v2/stocks/'+urllib.parse.quote(symbol,safe='')+'/trades/latest?feed=iex', data=True)['trade']
    trade_time, price = timestamp(latest['t']),float(latest['p'])
    max_age = 900 if clock['is_open'] else 7*86400
    if not math.isfinite(price) or price<=0 or now-trade_time>max_age or trade_time>now+300:
        raise ValueError('Stale or invalid Alpaca IEX trade: '+symbol)
    result=[]
    today=session_date(now)
    cash_events = cash_dividends(client, symbol, session_date(timestamp(bars[-limit]['t'])), today) if len(bars)>=limit else {}
    for b in bars:
        ts=timestamp(b['t']);day=session_date(ts)
        values={k:float(b[v]) for k,v in (('open','o'),('high','h'),('low','l'),('close','c'),('volume','v'))}
        if any(not math.isfinite(v) for v in values.values()) or any(values[k]<=0 for k in ('open','high','low','close')):
            raise ValueError('Invalid Alpaca bar')
        result.append(dict(values,timestamp=int(ts),session_date=day,source='alpaca_iex',interval='1d',
            complete=day<today,quote_price=price,market_timestamp=int(trade_time),market_open=clock['is_open'],
            dividend=cash_events.get(day,0),split_ratio=1,price_adjustment='split_adjusted_cash_dividends',corporate_actions_owner='Alpaca',fetched_at=int(now)))
    if len(result)<limit:raise ValueError('Insufficient Alpaca daily bars: '+symbol)
    return result[-limit:]


def cash_dividends(client, symbol, first_day, today):
    """Cash distributions in the same split-adjusted share units as historical bars.

    The API filters process_date, which can be later than ex_date; include future
    payable dates, then restrict events to observed ex-dates. Never use future splits.
    """
    params={'symbols':symbol,'types':'cash_dividend,forward_split,reverse_split',
        'start':(datetime.fromisoformat(first_day)-timedelta(days=180)).date().isoformat(),
        'end':(datetime.fromisoformat(today)+timedelta(days=90)).date().isoformat(),'limit':1000}
    actions={};seen=set()
    while True:
        response=client.request('/v1/corporate-actions?'+urllib.parse.urlencode(params),data=True)
        for kind,events in response.get('corporate_actions',{}).items():
            actions.setdefault(kind,[]).extend(events)
        token=response.get('next_page_token')
        if not token:break
        if token in seen:raise ValueError('Repeated Alpaca corporate action page')
        seen.add(token);params['page_token']=token
    splits=[];split_ids=set()
    for kind in ('forward_splits','reverse_splits'):
        for event in actions.get(kind,[]):
            if event.get('symbol')!=symbol or not event.get('ex_date'):continue
            if first_day<=event['ex_date']<=today:
                identity=event.get('id') or (kind,event['ex_date'],event['new_rate'],event['old_rate'])
                if identity in split_ids:continue
                split_ids.add(identity)
                ratio=float(event['new_rate'])/float(event['old_rate'])
                if not math.isfinite(ratio) or ratio<=0:raise ValueError('Invalid Alpaca split')
                splits.append((event['ex_date'],ratio))
    result={};ids=set()
    for event in actions.get('cash_dividends',[]):
        day=event.get('ex_date')
        if event.get('symbol')!=symbol or not day or not first_day<=day<=today:continue
        if event['id'] in ids:continue
        ids.add(event['id'])
        rate=float(event['rate'])
        for ex_date,ratio in splits:
            if day<ex_date:rate/=ratio
        if not math.isfinite(rate) or rate<0:raise ValueError('Invalid Alpaca dividend')
        result[day]=result.get(day,0)+rate
    return result

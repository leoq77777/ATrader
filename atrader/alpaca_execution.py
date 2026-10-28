"""Durable daily paper execution. Broker fills, never modeled fills, change the account."""
import fcntl
import hashlib
import json
import math
import time
from decimal import Decimal, ROUND_DOWN
from urllib.parse import urlencode, quote
from atrader.alpaca import PaperClient, timestamp
from atrader.config import STATE_DIR, write_json_atomic
from atrader.portfolio import session_date

TERMINAL = {'filled','canceled','expired','rejected','done_for_day','replaced','suspended','stopped','calculated'}


def scoped_account(account,cfg):
    reserve=float(cfg.get('external_cash_reserve_usd',0))
    if not math.isfinite(reserve) or reserve<0:raise ValueError('Invalid external cash reserve')
    if reserve:
        account=dict(account,cash=float(account['cash'])-reserve,equity=float(account['equity'])-reserve)
        if account['cash'] < -.05 or account['equity']<=0:
            raise ValueError('External cash reserve no longer reconciles')
    return account


def quantity(value):
    return str(Decimal(str(max(0,value))).quantize(Decimal('.000000001'),rounding=ROUND_DOWN))


def order_view(order):
    qty=float(order.get('filled_qty') or 0);price=float(order.get('filled_avg_price') or 0)
    return {'symbol':order['symbol'],'broker':'alpaca','side':order['side'].upper(),'qty':qty,'price':price,
        'timestamp':int(timestamp(order.get('filled_at') or order.get('updated_at') or order['created_at'])),
        'order_id':order['id'],'client_order_id':order.get('client_order_id'),'fee_usd':0,
        'fee_status':'not_reported','market_data_source':'alpaca_iex',
        'reason':'Alpaca paper 成交回报；策略依据见同轮决策日志'}


def effective_weights(account,positions,weights,cfg):
    equity=float(account['equity'])
    cap,gross=float(cfg['max_position_pct']),float(cfg['max_gross_exposure'])
    if not 0<cap<=gross<=1:raise ValueError('Invalid fixed paper risk limits')
    if equity<=0 or not math.isfinite(equity) or set(weights)!=set(cfg['watchlist']):raise ValueError('Invalid broker account or universe')
    if any(not math.isfinite(w) or w<0 or w>cap+1e-9 for w in weights.values()) or sum(weights.values())>gross+1e-9:
        raise ValueError('Invalid paper targets')
    if any(p.get('side')!='long' or not math.isfinite(float(p['qty'])) or float(p['qty'])<0
           or not math.isfinite(float(p['market_value'])) or float(p['market_value'])<0 for p in positions):
        raise ValueError('Invalid or short positions require manual review')
    external=sum(float(p['market_value']) for p in positions if p['symbol'] not in weights)
    scale=min(1,max(0,equity*gross-external)/max(sum(weights.values())*equity,1e-12))
    return {s:w*scale*.999 for s,w in weights.items()}


def intents(account, positions, weights, prices, cfg, side):
    adjusted=effective_weights(account,positions,weights,cfg)
    equity,cash=float(account['equity']),float(account['cash'])
    if not math.isfinite(cash):raise ValueError('Invalid broker cash')
    cash=max(0,cash)
    cap,gross=float(cfg['max_position_pct']),float(cfg['max_gross_exposure'])
    held={p['symbol']:p for p in positions}
    # Cash only, with fixed reserve and a small fill-price buffer. Never use margin buying_power.
    spend=max(0,cash-equity*(1-gross))
    result=[]
    current_gross=sum(float(p['market_value']) for p in positions)/equity
    for symbol in sorted(weights,key=lambda s:(-weights[s],s)):
        price=float(prices[symbol])
        if not math.isfinite(price) or price<=0:raise ValueError('Invalid paper price')
        p=held.get(symbol,{})
        qty=float(p.get('qty',0));current=qty*price
        target=equity*adjusted[symbol]
        delta=target-current
        due=abs(delta)>=equity*.015 or (weights[symbol]==0 and qty>0) or current/equity>cap+1e-8 or current_gross>gross+1e-8
        if not due:continue
        if side=='sell' and delta<0:
            size=min(qty,float(p.get('qty_available',qty)),-delta/price)
        elif side=='buy' and delta>0:
            size=min(delta/price,spend/(price*1.005));spend-=size*price*1.005
        else:continue
        value=quantity(size)
        if float(value)*price>=1 or (side=='sell' and weights[symbol]==0 and float(value)>0):
            result.append({'symbol':symbol,'side':side,'qty':value,'type':'market','time_in_force':'day'})
    return result


def execute_daily(cfg, clock, plan, prices):
    """Persist intent before POST; retry by deterministic client id after ambiguous replies."""
    client=PaperClient(cfg);today=session_date(timestamp(clock['timestamp']))
    STATE_DIR.mkdir(parents=True,exist_ok=True)
    with (STATE_DIR/'alpaca_execution.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'execution_busy','executions':[]}
        path=STATE_DIR/'alpaca_execution.json'
        state=json.loads(path.read_text()) if path.exists() else {}
        account=scoped_account(client.request('/v2/account'),cfg)
        identity=hashlib.sha256(account['id'].encode()).hexdigest()[:12]
        if state.get('account') and state['account']!=identity:raise ValueError('Alpaca account changed; review execution state')
        if state.get('session')==today and state.get('status') in ('completed','failed'):
            return {**state,'status':'already_rebalanced' if state['status']=='completed' else 'execution_failed'}
        opens=client.request('/v2/orders?status=open&limit=500')
        own={x['client_order_id'] for x in state.get('intents',[])} if state.get('session')==today else set()
        if len(opens)>=500 or any(o.get('client_order_id') not in own for o in opens):return {'status':'open_orders_blocked','executions':[]}
        if not clock['is_open']:return {'status':'market_closed','executions':[]}
        if account.get('status')!='ACTIVE' or account.get('trading_blocked') or account.get('account_blocked') or account.get('currency')!='USD':
            raise ValueError('Alpaca paper account is not eligible for USD trading')
        if state.get('session')!=today:
            if state.get('session'):
                write_json_atomic(STATE_DIR/'alpaca_execution_history'/(state['session']+'.json'),state)
            plan=json.loads(json.dumps(plan))
            plan['effective_weights']=effective_weights(account,client.request('/v2/positions'),plan['weights'],cfg)
            state={'account':identity,'session':today,'status':'pending','phase':'sell','intents':[],
                'plan':plan,'prices':prices,'equity_before':float(account['equity']),'executions':[],'created_at':int(time.time())}
            write_json_atomic(path,state)
            if cfg.get('paper_observation_enabled'):
                write_json_atomic(STATE_DIR/'observation/decisions'/(today+'.json'),
                    {'plan':plan,'prices':prices,'account_equity':float(account['equity']),'snapshot':plan.get('decision_snapshot')})
        # At most two phases. Buy quantities use broker cash and positions AFTER confirmed sells.
        for _ in range(2):
            if not state['intents']:
                positions=client.request('/v2/positions')
                account=scoped_account(client.request('/v2/account'),cfg)
                orders=intents(account,positions,state['plan']['weights'],prices,cfg,state['phase'])
                for order in orders:
                    asset=client.request('/v2/assets/'+quote(order['symbol'],safe=''))
                    if not asset.get('tradable') or not asset.get('fractionable'):
                        raise ValueError('Asset is not fractionally tradable: '+order['symbol'])
                    order['client_order_id']='atr-'+identity+'-'+today.replace('-','')+'-'+state['phase']+'-'+order['symbol']
                state['intents']=orders
                write_json_atomic(path,state)
            pending=False;failed=False;executions=[]
            for order in state['intents']:
                existing=client.request('/v2/orders:by_client_order_id?'+urlencode({'client_order_id':order['client_order_id']}),missing_ok=True)
                if existing is None:
                    fresh_clock=client.request('/v2/clock')
                    if not fresh_clock['is_open'] or session_date(timestamp(fresh_clock['timestamp']))!=today:
                        return {**state,'status':'market_closed'}
                    # No POST retry here. A later tick must reconcile the same client id first.
                    existing=client.request('/v2/orders',method='POST',payload=order)
                pending |= existing['status'] not in TERMINAL
                failed |= existing['status'] in TERMINAL and existing['status']!='filled'
                executions.append({'symbol':order['symbol'],'routed_broker':'alpaca','reason':state['plan']['reasons'][order['symbol']],
                    'target_weight':state['plan']['weights'][order['symbol']], 'effective_target_weight':state['plan']['weights'][order['symbol']],
                    'order_result':{'status':existing['status'],'order':order_view(existing)}})
            if pending:
                state['pending_executions']=executions;write_json_atomic(path,state)
                return {**state,'status':'orders_pending'}
            state['executions'].extend(executions);state.pop('pending_executions',None)
            if failed:
                state['status']='failed';write_json_atomic(path,state);return state
            if state['phase']=='sell':
                state['phase']='buy';state['intents']=[];write_json_atomic(path,state)
            else:
                state['status']='completed';state['completed_at']=int(time.time());write_json_atomic(path,state);return state
        return {**state,'status':'orders_pending'}

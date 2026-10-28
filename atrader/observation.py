"""One frozen Paper execution/risk observation; no statistical acceptance or promotion."""
import hashlib
import json
import math
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo
from atrader.alpaca import timestamp
from atrader.config import write_json_atomic
from atrader.portfolio import session_date

NY = ZoneInfo('America/New_York')
TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'done_for_day', 'replaced', 'suspended', 'stopped', 'calculated'}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Non-finite broker value')
    return value


def positions_view(rows):
    result = {}
    for p in rows:
        qty, value = number(p['qty']), number(p['market_value'])
        if p['symbol'] in result or p.get('side') != 'long' or qty < 0 or value < 0:
            raise ValueError('Invalid or duplicate long position')
        if qty > 0:
            result[p['symbol']] = {'qty': qty, 'market_value': value}
    return result


def load_contract(state_dir):
    folder = state_dir/'observation'
    contract = json.loads((folder/'contract.json').read_text())
    state = json.loads((folder/'state.json').read_text())
    if fingerprint(contract) != state['contract_hash']:
        raise ValueError('Observation contract changed; execution blocked')
    return contract, state


def register(root, state_dir, cfg, client, sessions=60):
    """Explicit operator command, under observation.lock with daemon stopped. GETs only."""
    if sessions < 60 or sessions > 252:
        raise ValueError('Observation requires 60 to 252 future full sessions')
    folder = state_dir/'observation'
    if folder.exists():
        raise ValueError('Observation already registered; never reset its capital or high-water mark')
    clock = client.request('/v2/clock')
    now = timestamp(clock['timestamp']);today = session_date(now)
    account = client.request('/v2/account')
    if account.get('currency') != 'USD' or account.get('status') != 'ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):
        raise ValueError('Ineligible paper account')
    if client.request('/v2/orders?status=open&limit=500'):
        raise ValueError('Reconcile open orders before registration')
    positions = positions_view(client.request('/v2/positions'))
    identity = hashlib.sha256(account['id'].encode()).hexdigest()[:12]
    execution_path = state_dir/'alpaca_execution.json'
    execution = json.loads(execution_path.read_text()) if execution_path.exists() else {}
    scoped = {s:p for s,p in positions.items() if s in cfg['watchlist']}
    # Migration must demonstrate ownership, not adopt every matching symbol by guesswork.
    if scoped:
        if execution.get('account') != identity or execution.get('status') != 'completed':
            raise ValueError('Existing scope holdings require completed owned execution evidence')
        expected = dict(execution['plan'].get('positions_before', {}))
        for item in execution.get('executions', []):
            o = item['order_result']['order']
            if o['symbol'] not in cfg['watchlist']:continue
            expected[o['symbol']] = expected.get(o['symbol'], 0) + number(o['qty'])*(1 if o['side']=='BUY' else -1)
        if any(abs(p['qty']-expected.get(s,0))>1e-7 for s,p in scoped.items()):
            raise ValueError('Initial holdings do not match owned execution evidence')
    campaign_path = state_dir/'research/campaign.json'
    campaign = json.loads(campaign_path.read_text())
    raw = (campaign_path.parent/'sample.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != campaign['sample_hash']:
        raise ValueError('Research sample hash mismatch')
    sample = json.loads(raw)
    if any(session_date(b['timestamp']) >= today for bars in sample['market'].values() for b in bars):
        raise ValueError('Research sample includes the protected observation start session')
    calendar = client.request('/v2/calendar?'+urlencode({'start':(datetime.fromisoformat(today)+timedelta(days=1)).date().isoformat(),
        'end':(datetime.fromisoformat(today)+timedelta(days=550)).date().isoformat()}))
    future = [x for x in calendar if x['date']>today][:sessions]
    if len(future)!=sessions or len({x['date'] for x in future})!=sessions:
        raise ValueError('Insufficient or duplicate broker calendar sessions')
    expiry = datetime.fromisoformat(future[-1]['date']+'T'+future[-1]['close']).replace(tzinfo=NY).timestamp()
    cash = number(account['cash']);equity = cash+sum(p['market_value'] for p in scoped.values())
    if cash<0 or equity<=0:raise ValueError('Positive cash-only strategy capital required')
    contract = {'schema':1,'mode':'execution_risk_observation','task_id':'existing_sector_research',
        'registered_at':now,'account':identity,'symbols':list(cfg['watchlist']),
        'strategy_sha256':hashlib.sha256((root/'strategy.py').read_bytes()).hexdigest(),
        'strategy_snapshot':(root/'strategy.py').read_text(),
        'initial_cash':cash,'initial_positions':scoped,'initial_equity':equity,
        'external_positions':{s:p for s,p in positions.items() if s not in scoped},
        'research_campaign':campaign['id'],'research_sample_hash':campaign['sample_hash'],
        'research_cutoff':campaign['research_end'],'research_limits':campaign['limits'],
        'inherited_research_attempts':len(campaign['trials']),'inherited_research_cost_usd':campaign['cost_usd'],
        'formal_challenges':0,'significance_budget_reserved':0,
        'future_sessions':[x['date'] for x in future],'expires_at':expiry,
        'on_expiry':'cash','performance_acceptance':'disabled_uncalibrated','warning_drawdown':.15,'stop_drawdown':.25,
        'capital_flow_policy':'unknown activity blocks new risk; no automatic attribution or reset',
        'dividends':'broker raw equity only; no claim of total-return performance',
        'risk_coverage':'starts at registration; earlier high-water marks unknown'}
    # Commit a complete directory, so a crash cannot expose half a registration.
    pending = state_dir/'observation.pending'
    pending.mkdir()
    write_json_atomic(pending/'contract.json',contract)
    write_json_atomic(pending/'state.json',{'contract_hash':fingerprint(contract),'status':'observing',
        'high_water':equity,'equity':equity,'drawdown':0,'updated_at':now,'exit_orders':[],'events':[]})
    pending.rename(folder)
    return contract


def activities(client, registered_at):
    params = {'after':datetime.fromtimestamp(registered_at,NY).isoformat(),'direction':'asc','page_size':100}
    result=[];seen=set()
    for _ in range(100):
        page=client.request('/v2/account/activities?'+urlencode(params))
        if not isinstance(page,list):raise ValueError('Invalid activities response')
        for item in page:
            if item['id'] in seen:raise ValueError('Repeated activity pagination')
            seen.add(item['id']);result.append(item)
        if len(page)<100:return result
        params['page_token']=page[-1]['id']
    raise ValueError('Activity history exceeds observation reconciliation limit')


def load_cleanup(state_dir, contract):
    path=state_dir/'observation/nvda_cleanup.json'
    if not path.exists():return None
    cleanup=json.loads(path.read_text())
    if (cleanup['contract_hash']!=fingerprint(contract) or cleanup['symbol']!='NVDA'
        or cleanup['authorized_qty']!=contract['external_positions'].get('NVDA',{}).get('qty')):
        raise ValueError('Invalid external NVDA cleanup authorization')
    return cleanup


def authorize_nvda_cleanup(state_dir,client):
    contract,state=load_contract(state_dir)
    if load_cleanup(state_dir,contract):raise ValueError('NVDA cleanup already registered')
    _,positions,_,_=reconcile(contract,client)
    if 'NVDA' not in positions or 'NVDA' in contract['symbols']:
        raise ValueError('No external NVDA position to clean up')
    if any(o['symbol']=='NVDA' for o in client.request('/v2/orders?status=open&limit=500')):
        raise ValueError('Reconcile existing NVDA orders first')
    cleanup={'contract_hash':fingerprint(contract),'symbol':'NVDA',
        'authorized_qty':contract['external_positions']['NVDA']['qty'],'orders':[],
        'authorization':'User explicitly requested removing legacy NVDA from Paper holdings; proceeds remain external',
        'status':'waiting_market','proceeds_usd':0}
    write_json_atomic(state_dir/'observation/nvda_cleanup.json',cleanup)
    return cleanup


def cleanup_tick(contract,state_dir,client,clock,cleanup,positions):
    path=state_dir/'observation/nvda_cleanup.json'
    for intent in cleanup['orders']:
        order=client.request('/v2/orders:by_client_order_id?'+urlencode({'client_order_id':intent['client_order_id']}),missing_ok=True)
        if order is None:
            fresh=client.request('/v2/clock')
            if session_date(timestamp(fresh['timestamp']))!=intent['session']:continue
            if not fresh['is_open']:cleanup['status']='waiting_market';write_json_atomic(path,cleanup);return
            order=client.request('/v2/orders',method='POST',payload={k:v for k,v in intent.items() if k!='session'})
        if order['status'] not in TERMINAL:
            cleanup['status']='fills_pending';write_json_atomic(path,cleanup);return
    remaining=positions.get('NVDA',{}).get('qty',0)
    if remaining<=1e-9:
        cleanup['status']='completed';write_json_atomic(path,cleanup);return
    cleanup['status']='waiting_market'
    if clock['is_open']:
        today=session_date(timestamp(clock['timestamp']))
        if not any(o['session']==today for o in cleanup['orders']):
            opens=client.request('/v2/orders?status=open&limit=500')
            if len(opens)>=500 or any(o['symbol']=='NVDA' for o in opens):
                raise ValueError('Unrecognized NVDA order blocks cleanup')
            cleanup['orders'].append({'symbol':'NVDA','side':'sell','qty':format(remaining,'.9f'),
                'type':'market','time_in_force':'day','session':today,
                'client_order_id':'atrext-'+contract['account']+'-'+today.replace('-','')+'-NVDA'})
            cleanup['status']='intent_persisted'
    write_json_atomic(path,cleanup)


def reconcile(contract, client, order_cache=None, cleanup=None):
    # Read activities on both sides of the account snapshot; retry next tick if it moved.
    first=activities(client,contract['registered_at'])
    account=client.request('/v2/account');positions=positions_view(client.request('/v2/positions'))
    second=activities(client,contract['registered_at'])
    if fingerprint(first)!=fingerprint(second):raise ValueError('Broker snapshot moved; reconciliation pending')
    if hashlib.sha256(account['id'].encode()).hexdigest()[:12]!=contract['account']:
        raise ValueError('Paper account changed')
    if account.get('currency')!='USD' or account.get('status')!='ACTIVE' or account.get('trading_blocked') or account.get('account_blocked'):
        raise ValueError('Paper account is blocked')
    expected={s:p['qty'] for s,p in contract['initial_positions'].items()};cash=contract['initial_cash']
    order_cache = order_cache if order_cache is not None else {}
    reserve,sold=0.,0.
    cleanup_ids={o['client_order_id'] for o in cleanup['orders']} if cleanup else set()
    for event in first:
        if cleanup and event.get('activity_type')=='FILL' and event.get('symbol')=='NVDA' and event.get('side')=='sell':
            order=client.request('/v2/orders/'+quote(event['order_id'],safe=''))
            if order.get('client_order_id') not in cleanup_ids or order.get('side')!='sell' or order.get('symbol')!='NVDA':
                raise ValueError('Unauthorized external fill')
            qty,price=number(event['qty']),number(event['price'])
            if qty<=0 or price<=0:raise ValueError('Invalid external fill')
            reserve+=qty*price;sold+=qty;cash+=qty*price
            if sold>cleanup['authorized_qty']+1e-8:raise ValueError('External cleanup exceeded authorized quantity')
            continue
        if event.get('activity_type')!='FILL' or event.get('symbol') not in contract['symbols']:
            raise ValueError('Unattributed account activity; manual review required')
        key=event['order_id']
        if key not in order_cache:
            order=client.request('/v2/orders/'+quote(key,safe=''))
            if not owned(order,contract):raise ValueError('Unowned fill; manual review required')
            order_cache[key]=order['symbol']
        if order_cache[key]!=event['symbol']:raise ValueError('Order symbol mismatch')
        qty,price=number(event['qty']),number(event['price']);side=event['side']
        if qty<=0 or price<=0 or side not in ('buy','sell'):raise ValueError('Invalid fill')
        sign=1 if side=='buy' else -1
        cash-=sign*qty*price;expected[event['symbol']]=expected.get(event['symbol'],0)+sign*qty
    if abs(cash-number(account['cash']))>.05:
        raise ValueError('Broker cash does not reconcile; new risk blocked')
    for symbol in set(expected)|set(positions)|set(contract['external_positions']):
        actual=positions.get(symbol,{}).get('qty',0)
        target=expected.get(symbol,0) if symbol in contract['symbols'] else contract['external_positions'].get(symbol,{}).get('qty',0)
        if cleanup and symbol=='NVDA':target-=sold
        if abs(actual-target)>1e-7:raise ValueError('Broker position does not reconcile: '+symbol)
    account=dict(account,external_cash_reserve_usd=reserve)
    equity=number(account['cash'])-reserve+sum(p['market_value'] for s,p in positions.items() if s in contract['symbols'])
    return account,positions,equity,len(first)


def owned(order, contract):
    key=order.get('client_order_id','')
    return order.get('symbol') in contract['symbols'] and (key.startswith('atr-'+contract['account']+'-') or key.startswith('atrx-'+contract['account']+'-'))


def save(state_dir,state):
    write_json_atomic(state_dir/'observation/state.json',state)


def exit_to_cash(contract,state,state_dir,client,clock):
    """Persist intent, reconcile by client id, confirm positions; never touch outside symbols."""
    opens=client.request('/v2/orders?status=open&limit=500')
    if len(opens)>=500:raise ValueError('Open order pagination limit reached')
    own_exit={o['client_order_id'] for o in state['exit_orders']}
    cleanup=load_cleanup(state_dir,contract)
    external_ids={o['client_order_id'] for o in cleanup['orders']} if cleanup else set()
    for order in opens:
        if order.get('client_order_id') in external_ids and order['symbol']=='NVDA':continue
        if not owned(order,contract):raise ValueError('Unknown open order blocks exit; never cancel it')
        if order['client_order_id'] not in own_exit:
            try:client.request('/v2/orders/'+quote(order['id'],safe=''),method='DELETE')
            except RuntimeError:
                confirmed=client.request('/v2/orders/'+quote(order['id'],safe=''))
                if confirmed['status'] not in TERMINAL:raise
            state['exit_phase']='cancel_pending';save(state_dir,state)
            return
    pending=False
    for intent in state['exit_orders']:
        order=client.request('/v2/orders:by_client_order_id?'+urlencode({'client_order_id':intent['client_order_id']}),missing_ok=True)
        if order is None:
            fresh=client.request('/v2/clock')
            if session_date(timestamp(fresh['timestamp']))!=intent['session']:
                intent['broker_status']='not_submitted_expired'
                continue
            if not fresh['is_open']:
                state['exit_phase']='waiting_market';save(state_dir,state);return
            order=client.request('/v2/orders',method='POST',payload={k:v for k,v in intent.items() if k in ('symbol','side','qty','type','time_in_force','client_order_id')})
        intent['broker_status']=order['status']
        pending |= order['status'] not in TERMINAL
    save(state_dir,state)
    if pending:
        state['exit_phase']='fills_pending';return
    # All submitted orders are terminal, including partial cancellations/rejections.
    # Reconcile again before computing residuals, and never generate same-day rejection loops.
    _,positions,equity,_=reconcile(contract,client,state.setdefault('owned_order_ids',{}),cleanup)
    remaining={s:p for s,p in positions.items() if s in contract['symbols'] and p['qty']>1e-8}
    if not remaining:
        state.update(status='cash',exit_phase='confirmed',equity=equity);return
    if not clock['is_open']:
        state['exit_phase']='waiting_market';return
    today=session_date(timestamp(clock['timestamp']))
    for symbol,p in sorted(remaining.items()):
        if any(o['symbol']==symbol and o['session']==today for o in state['exit_orders']):continue
        intent={'symbol':symbol,'side':'sell','qty':format(p['qty'],'.9f'),'type':'market','time_in_force':'day',
            'client_order_id':'atrx-'+contract['account']+'-'+today.replace('-','')+'-'+symbol,'session':today}
        state['exit_orders'].append(intent)
    state['exit_phase']='intents_persisted';save(state_dir,state)
    # Submission happens on next tick, after the durable intent can be reconciled.


def tick(root,state_dir,cfg,client,clock):
    contract,state=load_contract(state_dir)
    now=timestamp(clock['timestamp']);state['updated_at']=now
    try:
        if contract['symbols']!=cfg['watchlist']:raise ValueError('Observation universe changed')
        if hashlib.sha256((root/'strategy.py').read_bytes()).hexdigest()!=contract['strategy_sha256']:
            raise ValueError('Frozen execution source changed')
        cleanup=load_cleanup(state_dir,contract)
        account,positions,equity,count=reconcile(contract,client,state.setdefault('owned_order_ids',{}),cleanup)
        state['external_cash_reserve_usd']=account['external_cash_reserve_usd']
        if cleanup:
            cleanup['proceeds_usd']=account['external_cash_reserve_usd']
            cleanup_tick(contract,state_dir,client,clock,cleanup,positions)
        state.update(equity=equity,reconciled_activities=count,reconciliation='matched',error=None)
        state['high_water']=max(state['high_water'],equity)
        state['drawdown']=max(0,1-equity/state['high_water'])
        if state['status'] not in ('exit_pending','cash'):
            if state['drawdown']>=contract['stop_drawdown']:
                state.update(status='exit_pending',outcome='risk_terminated',exit_reason='drawdown_25_percent')
            elif now>=contract['expires_at']:
                state.update(status='exit_pending',outcome='inconclusive',exit_reason='observation_expired')
            else:state['status']='warning' if state['drawdown']>=contract['warning_drawdown'] else 'observing'
        save(state_dir,state)
        if state['status']=='cash' and any(s in contract['symbols'] for s in positions):
            state['status']='exit_pending'
        if state['status']=='exit_pending':exit_to_cash(contract,state,state_dir,client,clock)
        save(state_dir,state)
        return {'allow_trade':state['status'] in ('observing','warning') and (not cleanup or cleanup['status']=='completed'),
            'status':'external_cleanup_pending' if cleanup and cleanup['status']!='completed' and state['status'] in ('observing','warning') else state['status'],'state':state}
    except Exception as exc:
        state.update(error=str(exc),reconciliation='blocked')
        save(state_dir,state)
        return {'allow_trade':False,'status':'observation_blocked','state':state}


def research_boundary(state_dir,campaign):
    contract,_=load_contract(state_dir)
    raw=(state_dir/'research/sample.json').read_bytes()
    if campaign['id']!=contract['research_campaign'] or hashlib.sha256(raw).hexdigest()!=contract['research_sample_hash']:
        raise ValueError('Observation research boundary changed')
    if campaign['limits']!=contract['research_limits']:
        raise ValueError('Observation research budget changed; review required')
    return {k:contract[k] for k in ('task_id','research_campaign','research_cutoff','research_sample_hash','mode')}


def public_state(state_dir):
    folder=state_dir/'observation'
    if not folder.exists():return {'enabled':False}
    contract,state=load_contract(state_dir)
    return {'enabled':True,**{k:contract[k] for k in ('mode','task_id','registered_at','expires_at','research_cutoff','strategy_sha256','performance_acceptance','risk_coverage')},
        'external_cleanup':load_cleanup(state_dir,contract),
        **{k:state.get(k) for k in ('status','equity','high_water','drawdown','updated_at','outcome','exit_phase','exit_reason','reconciliation','error','external_cash_reserve_usd')}}

import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch
from atrader import observation as obs,ratchet
from atrader.config import write_json_atomic
from test_portfolio_harness import settings,rows


class Paper:
    def __init__(self):
        self.cash=0.;self.qty={'XLK':100.,'NVDA':5.};self.price=100.;self.orders={};self.posts=[];self.events=[]
        self.now='2026-10-27T14:00:00Z';self.open=True;self.pending=False;self.timeout=False;self.reject=False
        self.account=hashlib.sha256(b'test-account').hexdigest()[:12]
    def request(self,path,method='GET',payload=None,**kwargs):
        if path=='/v2/clock':return {'timestamp':self.now,'is_open':self.open}
        if path=='/v2/account':return {'id':'test-account','cash':str(self.cash),'equity':str(self.cash+self.qty.get('XLK',0)*self.price+self.qty.get('NVDA',0)*100),'status':'ACTIVE','currency':'USD'}
        if path=='/v2/positions':return [{'symbol':s,'qty':str(q),'market_value':str(q*(self.price if s=='XLK' else 100)),'side':'long'} for s,q in self.qty.items() if q>0]
        if path.startswith('/v2/calendar?'):
            return [{'date':(datetime(2026,10,28)+timedelta(days=i)).date().isoformat(),'close':'16:00'} for i in range(120) if (datetime(2026,10,28)+timedelta(days=i)).weekday()<5]
        if path.startswith('/v2/account/activities?'):return self.events.copy()
        if path.startswith('/v2/orders?status=open'):return [o.copy() for o in self.orders.values() if o['status'] in ('new','partially_filled','pending_cancel')]
        if path.startswith('/v2/orders:by_client_order_id?'):
            return self.orders.get(parse_qs(urlsplit(path).query)['client_order_id'][0])
        if path.startswith('/v2/orders/'):
            order=next(o for o in self.orders.values() if o['id']==path.split('/')[-1])
            if method=='DELETE':order['status']='canceled';return None
            return order.copy()
        if path=='/v2/orders' and method=='POST':
            key=payload['client_order_id'];assert key not in self.orders
            self.posts.append(dict(payload));order=dict(payload,id=key,status='new',filled_qty='0');self.orders[key]=order
            if self.reject:order['status']='rejected'
            elif not self.pending:self.fill(order)
            if self.timeout:self.timeout=False;raise RuntimeError('timeout after acceptance')
            return order
        raise AssertionError((path,method))
    def fill(self,order,qty=None):
        qty=float(order['qty']) if qty is None else qty
        self.qty[order['symbol']]-=qty;self.cash+=qty*self.price
        order.update(status='filled',filled_qty=str(qty))
        self.events.append({'id':order['id']+'-fill-'+str(len(self.events)),'activity_type':'FILL','order_id':order['id'],'symbol':order['symbol'],'qty':str(qty),'price':str(self.price),'side':'sell'})


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.state=self.root/'state';self.state.mkdir()
        self.cfg=settings();self.cfg['paper_observation_enabled']=True;self.paper=Paper()
        (self.root/'strategy.py').write_text('class Strategy: pass\n')
        sample={'market':{'XLK':rows(5)}};raw=json.dumps(sample).encode()
        (self.state/'research').mkdir();(self.state/'research/sample.json').write_bytes(raw)
        self.campaign={'id':'campaign','sample_hash':hashlib.sha256(raw).hexdigest(),'research_end':'2024-01-01','limits':{'max_trials':120,'max_cost_usd':1},'trials':[{}],'cost_usd':.01}
        write_json_atomic(self.state/'research/campaign.json',self.campaign)
        write_json_atomic(self.state/'alpaca_execution.json',{'account':self.paper.account,'status':'completed','plan':{'positions_before':{'XLK':100}},'executions':[]})
        self.contract=obs.register(self.root,self.state,self.cfg,self.paper)
    def tick(self):return obs.tick(self.root,self.state,self.cfg,self.paper,self.paper.request('/v2/clock'))
    def test_registration_excludes_nvda_and_never_resets(self):
        self.assertEqual(self.contract['initial_equity'],10000)
        self.assertEqual(set(self.contract['initial_positions']),{'XLK'})
        self.assertEqual(self.contract['formal_challenges'],0)
        self.assertEqual(self.contract['inherited_research_attempts'],1)
        with self.assertRaisesRegex(ValueError,'already registered'):obs.register(self.root,self.state,self.cfg,self.paper)
        self.assertEqual(self.tick()['status'],'observing')
    def test_nvda_price_is_excluded_from_high_water_and_warning_latches_correctly(self):
        self.paper.price=80
        state=self.tick()['state'];self.assertEqual(state['status'],'warning');self.assertAlmostEqual(state['drawdown'],.2)
        self.assertEqual(state['high_water'],10000)
        self.paper.price=120;self.assertEqual(self.tick()['state']['high_water'],12000)
        self.paper.price=100;self.assertEqual(self.tick()['status'],'warning')
    def test_risk_exit_is_durable_and_confirmed_only_after_fills(self):
        self.paper.price=70;self.paper.pending=True
        self.assertFalse(self.tick()['allow_trade']);self.assertEqual(self.paper.posts,[])
        result=self.tick();self.assertEqual(result['status'],'exit_pending');self.assertEqual(len(self.paper.posts),1)
        self.assertEqual(self.tick()['status'],'exit_pending');self.assertEqual(len(self.paper.posts),1)
        self.paper.fill(next(iter(self.paper.orders.values())))
        self.assertEqual(self.tick()['status'],'cash');self.assertEqual(self.paper.qty['NVDA'],5)
        self.assertEqual(self.tick()['state']['outcome'],'risk_terminated')
    def test_timeout_after_broker_accepts_does_not_duplicate_exit(self):
        self.paper.price=70;self.tick();self.paper.timeout=True
        self.assertEqual(self.tick()['status'],'observation_blocked')
        self.assertEqual(self.tick()['status'],'cash');self.assertEqual(len(self.paper.posts),1)
    def test_closed_market_persists_exit_and_never_posts(self):
        self.paper.price=70;self.paper.open=False
        result=self.tick();self.assertEqual(result['status'],'exit_pending');self.assertEqual(result['state']['exit_phase'],'waiting_market')
        self.assertEqual(self.paper.posts,[])
        self.paper.open=True;self.tick();self.tick();self.assertEqual(self.tick()['status'],'cash')
    def test_unsent_intent_at_close_can_recover_on_next_session(self):
        self.paper.price=70;self.tick();self.paper.open=False;self.tick()
        self.paper.now='2026-10-28T14:00:00Z';self.paper.open=True
        self.tick();self.tick();self.assertEqual(self.tick()['status'],'cash');self.assertEqual(len(self.paper.posts),1)
    def test_rejected_exit_never_busy_loops_or_resumes_buys(self):
        self.paper.price=70;self.paper.reject=True;self.tick();self.tick();self.tick()
        self.assertEqual(len(self.paper.posts),1);self.assertFalse(self.tick()['allow_trade'])
        self.paper.now='2026-10-28T14:00:00Z';self.paper.reject=False
        self.tick();self.tick();self.assertEqual(self.tick()['status'],'cash')
    def test_external_flows_or_position_changes_block_new_risk(self):
        self.paper.cash+=100
        self.assertEqual(self.tick()['status'],'observation_blocked');self.assertEqual(self.paper.posts,[])
        self.paper.cash-=100;self.paper.qty['NVDA']=6
        self.assertEqual(self.tick()['status'],'observation_blocked');self.assertEqual(self.paper.posts,[])
    def test_partial_fill_and_cancel_reconcile_residual_on_next_day(self):
        self.paper.price=70;self.paper.pending=True;self.tick();self.tick()
        order=next(iter(self.paper.orders.values()));self.paper.fill(order,40);order['status']='canceled'
        self.assertEqual(self.tick()['status'],'exit_pending');self.assertEqual(len(self.paper.posts),1)
        self.paper.now='2026-10-28T14:00:00Z';self.paper.pending=False
        self.tick();self.tick();self.assertEqual(self.tick()['status'],'cash');self.assertEqual(self.paper.qty['XLK'],0)
    def test_expiry_without_acceptance_produces_inconclusive_and_cash(self):
        self.paper.now=datetime.fromtimestamp(self.contract['expires_at']+1,obs.NY).isoformat();self.paper.open=False
        state=self.tick()['state'];self.assertEqual(state['outcome'],'inconclusive');self.assertEqual(state['status'],'exit_pending')
        self.assertEqual(self.paper.posts,[])
    def test_source_and_contract_tampering_block_execution(self):
        (self.root/'strategy.py').write_text('changed')
        self.assertEqual(self.tick()['status'],'observation_blocked')
        (self.root/'strategy.py').write_text(self.contract['strategy_snapshot'])
        contract=dict(self.contract,expires_at=0);write_json_atomic(self.state/'observation/contract.json',contract)
        with self.assertRaisesRegex(ValueError,'contract changed'):self.tick()
    def test_owned_normal_order_is_cancelled_before_exit_intents(self):
        key='atr-'+self.paper.account+'-20261027-buy-XLK'
        self.paper.orders[key]={'id':key,'client_order_id':key,'symbol':'XLK','status':'new'}
        self.paper.price=70
        first=self.tick();self.assertEqual(first['state']['exit_phase'],'cancel_pending')
        self.assertEqual(self.paper.orders[key]['status'],'canceled');self.assertEqual(self.paper.posts,[])
        self.tick();self.tick();self.assertEqual(self.tick()['status'],'cash')

    def test_api_outage_never_falls_through_to_normal_trading(self):
        with patch.object(self.paper,'request',side_effect=RuntimeError('API unavailable')):
            result=obs.tick(self.root,self.state,self.cfg,self.paper,{'timestamp':self.paper.now,'is_open':True})
        self.assertFalse(result['allow_trade']);self.assertEqual(result['status'],'observation_blocked')
        self.assertEqual(self.paper.posts,[])

    def test_unknown_orders_are_never_cancelled(self):
        self.paper.orders['manual']={'id':'manual','symbol':'NVDA','client_order_id':'manual','status':'new'}
        self.paper.price=70
        self.assertEqual(self.tick()['status'],'observation_blocked');self.assertEqual(self.paper.orders['manual']['status'],'new')
        self.assertEqual(self.paper.posts,[])
    def test_boundary_contains_no_account_or_performance_and_rejects_new_sample(self):
        boundary=obs.research_boundary(self.state,self.campaign)
        self.assertNotIn('equity',boundary);self.assertNotIn('initial_positions',boundary)
        (self.state/'research/sample.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'boundary changed'):obs.research_boundary(self.state,self.campaign)
    def test_guard_runs_even_when_market_closed_and_blocks_normal_execution(self):
        self.paper.price=70;self.paper.open=False
        with patch.object(ratchet,'PROJECT_ROOT',self.root),patch.object(ratchet,'STATE_DIR',self.state),patch('atrader.alpaca.PaperClient',return_value=self.paper),patch.object(ratchet,'_run_alpaca_unmanaged') as normal:
            result=ratchet._run_alpaca_cycle(self.cfg)
            normal.assert_not_called();self.assertEqual(result['status'],'exit_pending')


class ExternalCleanupTest(unittest.TestCase):
    setUp = ObservationTest.setUp
    tick = ObservationTest.tick
    def test_cleanup_waits_for_open_and_does_not_modify_original_contract(self):
        before=(self.state/'observation/contract.json').read_bytes()
        obs.authorize_nvda_cleanup(self.state,self.paper)
        self.paper.open=False
        result=self.tick();self.assertEqual(result['status'],'external_cleanup_pending');self.assertFalse(result['allow_trade'])
        self.assertEqual(self.paper.posts,[])
        self.assertEqual((self.state/'observation/contract.json').read_bytes(),before)
    def test_sale_proceeds_and_profit_are_excluded_from_strategy(self):
        obs.authorize_nvda_cleanup(self.state,self.paper)
        self.tick();self.tick()
        # NVDA realizes an external gain; neither proceeds nor gain belongs to the strategy.
        self.paper.cash=600;self.paper.events[0]['price']='120'
        result=self.tick()
        self.assertEqual(self.paper.qty['NVDA'],0)
        self.assertEqual(result['state']['external_cash_reserve_usd'],600)
        self.assertEqual(result['state']['equity'],10000)
        self.assertEqual(result['state']['high_water'],10000)
        self.assertEqual(len(self.paper.posts),1)
        self.assertEqual(obs.load_cleanup(self.state,self.contract)['status'],'completed')
        self.assertEqual(result['state']['reconciliation'],'matched')
    def test_cleanup_timeout_after_acceptance_is_reconciled_without_duplicate(self):
        obs.authorize_nvda_cleanup(self.state,self.paper);self.tick();self.paper.timeout=True
        self.assertEqual(self.tick()['status'],'observation_blocked')
        self.assertTrue(self.tick()['allow_trade']);self.assertEqual(len(self.paper.posts),1)
    def test_partial_external_sale_reserves_only_actual_fills(self):
        obs.authorize_nvda_cleanup(self.state,self.paper);self.paper.pending=True
        self.tick();self.tick();order=next(iter(self.paper.orders.values()))
        self.paper.fill(order,2);order['status']='canceled'
        result=self.tick();self.assertEqual(result['state']['external_cash_reserve_usd'],200)
        self.assertEqual(result['state']['equity'],10000)
        self.assertFalse(result['allow_trade']);self.assertEqual(self.paper.qty['NVDA'],3)
        self.paper.now='2026-10-28T14:00:00Z';self.paper.pending=False
        self.tick();self.tick();self.tick()
        self.assertEqual(self.paper.qty['NVDA'],0);self.assertEqual(len(self.paper.posts),2)
        self.assertEqual(self.tick()['state']['external_cash_reserve_usd'],500)
    def test_unknown_external_sale_still_blocks_after_authorization(self):
        obs.authorize_nvda_cleanup(self.state,self.paper)
        self.paper.orders['manual']={'id':'manual','client_order_id':'manual','symbol':'NVDA','side':'sell','qty':'5','status':'new'}
        self.paper.fill(self.paper.orders['manual'])
        self.assertEqual(self.tick()['status'],'observation_blocked')
    def test_quarantined_cash_is_not_available_to_strategy_execution(self):
        from atrader.alpaca_execution import scoped_account
        raw={'cash':'1500','equity':'11500','id':'test'}
        adjusted=scoped_account(raw,{'external_cash_reserve_usd':1500})
        self.assertEqual(adjusted['cash'],0);self.assertEqual(adjusted['equity'],10000)
        self.assertEqual(raw['cash'],'1500')

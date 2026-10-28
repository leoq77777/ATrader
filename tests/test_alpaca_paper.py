import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from unittest.mock import patch
from atrader import alpaca, alpaca_execution as execution, brokers, ratchet, web
from test_portfolio_harness import settings


class FakePaper:
    def __init__(self):
        self.cash=7500;self.positions={'XLK':25};self.orders={};self.posts=[]
        self.pending=False;self.ambiguous=False;self.reject=False;self.opens=[];self.clock_open=True
    def __call__(self,cfg):return self
    def fill(self,order):
        if order['status']=='filled':return
        qty=float(order['qty']);sign=1 if order['side']=='buy' else -1
        self.positions[order['symbol']]=self.positions.get(order['symbol'],0)+sign*qty
        self.cash-=sign*qty*100
        order.update(status='filled',filled_qty=str(qty),filled_avg_price='100',filled_at='2026-10-27T14:00:00Z')
    def request(self,path,method='GET',payload=None,**kwargs):
        if path=='/v2/clock':return {'is_open':self.clock_open,'timestamp':'2026-10-27T14:00:00Z'}
        if path=='/v2/account':return {'id':'test-paper-account','cash':str(self.cash),'equity':str(self.cash+sum(self.positions.values())*100),'last_equity':'10000','status':'ACTIVE','currency':'USD','buying_power':'1000000'}
        if path=='/v2/positions':return [{'symbol':s,'qty':str(q),'qty_available':str(q),'side':'long','market_value':str(q*100)} for s,q in self.positions.items() if q>0]
        if path.startswith('/v2/orders?status=open'):return self.opens+[o for o in self.orders.values() if o['status']=='new']
        if path.startswith('/v2/assets/'):return {'tradable':True,'fractionable':True}
        if path.startswith('/v2/orders:by_client_order_id?'):
            key=parse_qs(urlsplit(path).query)['client_order_id'][0]
            return self.orders.get(key)
        if path=='/v2/orders' and method=='POST':
            self.posts.append(deepcopy(payload));key=payload['client_order_id']
            assert key not in self.orders,'Duplicate broker order'
            order=dict(payload,id=key,created_at='2026-10-27T14:00:00Z',status='new',filled_qty='0',filled_avg_price=None)
            self.orders[key]=order
            if self.reject:order['status']='rejected'
            elif not self.pending:self.fill(order)
            if self.ambiguous:
                self.ambiguous=False
                raise RuntimeError('Connection lost AFTER server accepted order')
            return order
        raise AssertionError(path)


class AlpacaExecutionTest(unittest.TestCase):
    clock={'is_open':True,'timestamp':'2026-10-27T14:00:00Z'}
    plan={'weights':{'XLK':0,'XLF':.25},'reasons':{'XLK':'exit','XLF':'enter'}}
    def run_cycle(self,fake,root):
        with patch.object(execution,'STATE_DIR',root),patch.object(execution,'PaperClient',fake):
            return execution.execute_daily(settings(),self.clock,self.plan,{'XLK':100,'XLF':100})
    def test_sell_before_buy_actual_cash_and_daily_restart_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=FakePaper()
            result=self.run_cycle(fake,root)
            self.assertEqual(result['status'],'completed')
            self.assertEqual([o['side'] for o in fake.posts],['sell','buy'])
            self.assertEqual(self.run_cycle(fake,root)['status'],'already_rebalanced')
            self.assertEqual(len(fake.posts),2)
            self.assertGreaterEqual(fake.cash,2500)
            self.assertTrue(all(o['type']=='market' and o['time_in_force']=='day' for o in fake.posts))
    def test_pending_sell_blocks_buys_and_reconciles_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=FakePaper();fake.pending=True
            self.assertEqual(self.run_cycle(fake,root)['status'],'orders_pending')
            self.assertEqual(len(fake.posts),1)
            self.assertEqual(self.run_cycle(fake,root)['status'],'orders_pending')
            self.assertEqual(len(fake.posts),1)
            fake.fill(next(iter(fake.orders.values())));fake.pending=False
            self.assertEqual(self.run_cycle(fake,root)['status'],'completed')
            self.assertEqual([o['side'] for o in fake.posts],['sell','buy'])
    def test_timeout_after_acceptance_does_not_duplicate_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=FakePaper();fake.ambiguous=True
            with self.assertRaises(RuntimeError):self.run_cycle(fake,root)
            self.assertEqual(self.run_cycle(fake,root)['status'],'completed')
            self.assertEqual(len(fake.posts),2)
            self.assertEqual(len({o['client_order_id'] for o in fake.posts}),2)
    def test_market_closing_during_planning_never_queues_next_day_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake=FakePaper();fake.clock_open=False
            self.assertEqual(self.run_cycle(fake,Path(tmp))['status'],'market_closed')
            self.assertEqual(fake.posts,[])
    def test_rejected_sell_halts_buys(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake=FakePaper();fake.reject=True
            self.assertEqual(self.run_cycle(fake,Path(tmp))['status'],'failed')
            self.assertEqual(len(fake.posts),1)
            self.assertEqual(self.run_cycle(fake,Path(tmp))['status'],'execution_failed')
    def test_partially_filled_sell_does_not_unlock_buys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=FakePaper();fake.pending=True
            self.run_cycle(fake,root)
            order=next(iter(fake.orders.values()));order.update(status='partially_filled',filled_qty='10',filled_avg_price='100')
            self.assertEqual(self.run_cycle(fake,root)['status'],'orders_pending')
            self.assertEqual([o['side'] for o in fake.posts],['sell'])
    def test_leveraged_risk_configuration_is_rejected(self):
        cfg=settings();cfg.update(max_position_pct=1.25,max_gross_exposure=1.5)
        with self.assertRaisesRegex(ValueError,'risk limits'):
            execution.intents({'equity':'10000','cash':'10000'},[],{'XLK':1.25,'XLF':0},{'XLK':100,'XLF':100},cfg,'buy')
    def test_gross_cap_forces_small_reductions_even_below_turnover_threshold(self):
        positions=[{'symbol':'NVDA','qty':'30','side':'long','market_value':'3000'},
                   {'symbol':'XLK','qty':'25','side':'long','market_value':'2500'},
                   {'symbol':'XLF','qty':'25','side':'long','market_value':'2500'}]
        orders=execution.intents({'equity':'10000','cash':'2000'},positions,{'XLK':.25,'XLF':.25},{'XLK':100,'XLF':100},settings(),'sell')
        self.assertEqual({o['symbol'] for o in orders},{'XLK','XLF'})
        self.assertGreaterEqual(sum(float(o['qty'])*100 for o in orders),500)
    def test_external_open_orders_block_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake=FakePaper();fake.opens=[{'client_order_id':'manual-order'}]
            self.assertEqual(self.run_cycle(fake,Path(tmp))['status'],'open_orders_blocked')
            self.assertEqual(fake.posts,[])
    def test_external_holding_preserved_and_margin_is_not_used(self):
        cfg=settings();cfg.update(max_holdings=2)
        account={'equity':'10000','cash':'1000','buying_power':'1000000'}
        positions=[{'symbol':'NVDA','qty':'90','side':'long','market_value':'9000'}]
        weights={'XLK':.25,'XLF':.25}
        self.assertEqual(execution.intents(account,positions,weights,{'XLK':100,'XLF':100},cfg,'buy'),[])
        self.assertEqual(execution.intents(account,positions,weights,{'XLK':100,'XLF':100},cfg,'sell'),[])
        positions[0].update(qty='50',market_value='5000');account['cash']='5000'
        buy=execution.intents(account,positions,weights,{'XLK':100,'XLF':100},cfg,'buy')
        self.assertLessEqual(sum(float(o['qty'])*100 for o in buy),2500)


class AlpacaBoundaryTest(unittest.TestCase):
    def test_live_insecure_or_lookalike_endpoint_is_rejected(self):
        for url in ('https://api.alpaca.markets','http://paper-api.alpaca.markets','https://paper-api.alpaca.markets.evil','https://user:secret@paper-api.alpaca.markets'):
            with self.subTest(url=url),self.assertRaises(ValueError):
                alpaca.PaperClient({'alpaca_base_url':url,'alpaca_api_key':'key','alpaca_secret_key':'secret'})
        with self.assertRaisesRegex(ValueError,'credentials'):
            brokers.MultiBrokerRouter({'execution_provider':'alpaca'})
    def test_closed_market_never_runs_strategy_or_posts(self):
        fake=FakePaper()
        with patch.object(ratchet,'load_config',return_value=dict(settings(),execution_provider='alpaca')),patch.object(alpaca,'PaperClient',return_value=fake),patch.object(fake,'request',return_value={'is_open':False}),patch.object(ratchet,'run_strategy') as worker:
            self.assertEqual(ratchet.run_live_trading_cycle()['status'],'market_closed')
            worker.assert_not_called();self.assertEqual(fake.posts,[])
    def test_nanosecond_timestamp_is_supported_on_ubuntu_python(self):
        self.assertEqual(alpaca.timestamp('2026-10-27T01:08:09.467766664-04:00'),alpaca.timestamp('2026-10-27T05:08:09.467766Z'))
    def test_broker_summary_uses_external_values_and_never_reads_local_ledger(self):
        account={'status':'ACTIVE','cash':'98750','equity':'100014.51','last_equity':'99985.46'}
        positions=[{'symbol':'NVDA','qty':'5','avg_entry_price':'200','current_price':'250','market_value':'1250','unrealized_pl':'250'}]
        with patch.object(alpaca.PaperClient,'__init__',return_value=None),patch.object(alpaca.PaperClient,'request',side_effect=[account,positions,[]]),patch.object(brokers.LocalPaperBroker,'_ensure_ledger',side_effect=AssertionError('Local ledger read')):
            summary=brokers.MultiBrokerRouter(dict(settings(),execution_provider='alpaca')).get_unified_portfolio()
            self.assertEqual(summary['virtual_master_ledger']['total_equity_usd'],100014.51)
            self.assertEqual(summary['accounts'][0]['broker'],'alpaca')
            self.assertEqual(summary['accounts'][0]['positions'][0]['symbol'],'NVDA')


class CancelTransportTest(unittest.TestCase):
    def test_no_content_cancel_response_does_not_parse_json(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.status=204;response.__enter__.return_value=response
        with patch.object(alpaca.urllib.request,'urlopen',return_value=response),patch.object(alpaca.json,'load',side_effect=AssertionError('204 is empty')):
            cfg=dict(settings(),alpaca_api_key='paper-key',alpaca_secret_key='paper-secret')
            self.assertIsNone(alpaca.PaperClient(cfg).request('/v2/orders/test-id',method='DELETE'))

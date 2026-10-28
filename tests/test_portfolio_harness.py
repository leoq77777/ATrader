import json
import math
import os
import os
import subprocess
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from atrader import brokers, config, evaluator, journal, ratchet
from atrader.backtest import simulate, evaluate
from atrader.portfolio import allocate, align_bars, rebalance, apply_dividends, mark_equity
from atrader.strategy_runner import validate_source, run_strategy

ROOT=Path(__file__).resolve().parents[1]


def settings():
    cfg=deepcopy(config.DEFAULT_CONFIG)
    cfg.update(market_data_source='yahoo',execution_provider='local_paper',execution_market_data_source='yahoo',watchlist=['XLK','XLF'],max_holdings=1,rank_buffer=1,trading_fee_bps=0,slippage_bps=0)
    return cfg


def rows(n,base=100,start=1577975400):
    return [{'timestamp':start+i*86400,'open':base+i*.05,'high':base+i*.05+1,'low':base+i*.05-1,'close':base+i*.05,'volume':1000,'dividend':0} for i in range(n)]


class AllocationTest(unittest.TestCase):
    def test_rank_buffer_and_hard_cash_limits(self):
        cfg=settings();signals={'XLK':{'score':2,'eligible':True,'exposure':1},'XLF':{'score':1,'eligible':True,'exposure':1}}
        plan=allocate(signals,{'XLF':1},cfg)
        self.assertEqual(plan['selected'],['XLF'])
        self.assertEqual(sum(plan['weights'].values()),.25)
        signals['XLF']['eligible']=False
        self.assertEqual(allocate(signals,{'XLF':1},cfg)['selected'],['XLK'])

    def test_malformed_and_out_of_universe_signals_rejected(self):
        valid={'XLK':{'score':1,'eligible':True},'XLF':{'score':0,'eligible':False}}
        for bad in ({**valid,'BTC':valid['XLK']},dict(valid,XLK={'score':math.nan,'eligible':True}),dict(valid,XLK={'score':1,'eligible':True,'exposure':-1})):
            with self.assertRaises(ValueError):allocate(bad,{},settings())

    def test_sells_release_shared_cash_before_buys(self):
        cfg=settings();cfg.update(max_position_pct=1,max_gross_exposure=1)
        book={'cash':0,'positions':{'XLK':{'qty':100,'avg_price':100}},'orders':[]}
        updated,trades=rebalance(book,{'XLK':0,'XLF':1},{'XLK':100,'XLF':100},cfg,1)
        self.assertEqual([t['side'] for t in trades],['SELL','BUY'])
        self.assertEqual(updated['positions']['XLF']['qty'],100)
        self.assertEqual(updated['cash'],0)
        self.assertEqual(book['positions']['XLK']['qty'],100)

    def test_fees_slippage_cash_reserve_and_full_small_exit(self):
        cfg=settings();cfg.update(trading_fee_bps=5,slippage_bps=3)
        book={'cash':10000,'positions':{},'orders':[]}
        updated,trades=rebalance(book,{'XLK':.25},{'XLK':100,'XLF':100},cfg,1)
        self.assertGreater(trades[0]['price'],100)
        self.assertAlmostEqual(mark_equity(updated,{'XLK':100})+trades[0]['fee_usd']+trades[0]['slippage_usd'],10000)
        self.assertGreaterEqual(updated['cash'],2500)
        self.assertLessEqual(updated['positions']['XLK']['qty']*100/mark_equity(updated,{'XLK':100}),.25+1e-9)
        updated['positions']={'XLK':{'qty':.001,'avg_price':100}}
        exited,trades=rebalance(updated,{'XLK':0},{'XLK':100},cfg,2)
        self.assertEqual(exited['positions'],{})
        self.assertEqual(trades[0]['side'],'SELL')

    def test_bad_weights_and_prices_cannot_mutate_account(self):
        book={'cash':10000,'positions':{},'orders':[]}
        for weights,prices in (({'BTC':.1},{'BTC':100}),({'XLK':-.1},{'XLK':100}),({'XLK':.26},{'XLK':100}),({'XLK':.2},{'XLK':0})):
            with self.assertRaises(ValueError):rebalance(book,weights,prices,settings(),1)
        self.assertEqual(book['cash'],10000)

    def test_dividends_credit_only_existing_holdings(self):
        book={'cash':0,'positions':{'XLK':{'qty':10,'avg_price':100}}}
        result,income=apply_dividends(book,{'XLK':{'dividend':.5},'XLF':{'dividend':10}})
        self.assertEqual(income,5);self.assertEqual(result['cash'],5);self.assertEqual(book['cash'],0)


class SimulationTest(unittest.TestCase):
    def test_no_future_data_next_open_fills_and_mutation_isolation(self):
        market={s:rows(4) for s in ('XLK','XLF','SPY')}
        market['XLK'][1]['open']=200;market['XLK'][1]['close']=200
        observed=[]
        class Strategy:
            def rank_assets(self,history,positions):
                observed.append(history['XLK'][-1]['timestamp'])
                history['XLK'][-1]['close']=999999
                return {'XLK':{'score':1,'eligible':True,'exposure':1},'XLF':{'score':0,'eligible':False,'exposure':0}}
        before=deepcopy(market)
        result=simulate(Strategy(),market,settings(),1,2)
        self.assertEqual(observed,[market['XLK'][0]['timestamp']])
        self.assertEqual(result['recent_trades'][0]['price'],200)
        self.assertEqual(result['recent_trades'][0]['qty'],12.5)
        self.assertEqual(market,before)

    def test_calendar_alignment_is_by_date_and_rejects_duplicates(self):
        data={'XLK':rows(4),'SPY':rows(4)[1:]}
        days,aligned=align_bars(data,['XLK','SPY'])
        self.assertEqual(len(days),3)
        data['SPY'].append(data['SPY'][-1])
        with self.assertRaises(ValueError):align_bars(data,['XLK','SPY'])

    def test_rolling_folds_exclude_reserved_holdout(self):
        data={s:rows(800) for s in ('XLK','XLF','SPY')}
        class Flat:
            def rank_assets(self,h,p):return {s:{'score':0,'eligible':False} for s in ('XLK','XLF')}
        result=evaluate(Flat,data,settings())
        self.assertEqual(result['holdout']['reserved_sessions'],160)
        self.assertFalse(result['holdout']['used_for_promotion'])
        altered=deepcopy(data)
        for bars in altered.values():
            for bar in bars[640:]:
                for key in ('open','high','low','close'):bar[key]*=10
        self.assertEqual(evaluate(Flat,altered,settings())['ratchet_score'],result['ratchet_score'])
        self.assertEqual(len(result['validation_folds']),2)


class WorkerTest(unittest.TestCase):
    def test_baseline_runs_in_restricted_process(self):
        data={s:rows(150,100+i*5) for i,s in enumerate(('XLK','XLF','SPY'))}
        result=run_strategy(ROOT/'strategy.py',{'operation':'decision','history':data,'positions':{},'config':evaluator.harness_config(settings())})
        self.assertEqual(set(result['weights']),{'XLK','XLF'})
        self.assertLessEqual(sum(result['weights'].values()),.75)

    def test_io_introspection_and_module_mutation_are_denied(self):
        for code in ('import os\nclass Strategy: pass','class Strategy:\n def rank_assets(self,h,p): return h.__class__', 'import math\nmath.sqrt=lambda x:0\nclass Strategy: pass'):
            with self.assertRaises(ValueError):validate_source(code)
        source="class Strategy:\n name='bad'\n def rank_assets(self,h,p):\n  return open('/etc/passwd').read()\n"
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'strategy.py';path.write_text(source)
            with self.assertRaisesRegex(ValueError,'open'):
                run_strategy(path,{'operation':'decision','history':{},'positions':{},'config':settings()})

    def test_worker_timeout_is_an_experiment_failure(self):
        with patch('atrader.strategy_runner.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',45)):
            with self.assertRaisesRegex(RuntimeError,'45 second'):
                run_strategy(ROOT/'strategy.py',{'operation':'decision'})


class ConfigurationTest(unittest.TestCase):
    def test_credentials_never_enable_real_broker_and_scope_is_fixed(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(config,'CONFIG_FILE',Path(tmp)/'none'),patch.object(config,'ensure_state_dir'),patch.object(config,'_parse_dotenv',return_value={'ALPACA_API_KEY':'test','ALPACA_SECRET_KEY':'test'}),patch.dict(os.environ,{},clear=True):
            cfg=config.load_config()
            self.assertEqual(cfg['active_brokers'],['alpaca'])
            self.assertEqual(cfg['market_interval'],'1d')
            self.assertNotIn('BTC/USDT',cfg['watchlist'])
            self.assertNotIn('SPY',cfg['watchlist'])


class ExecutionIntegrationTest(unittest.TestCase):
    def test_live_uses_shared_engine_liquidates_legacy_and_runs_once_per_session(self):
        now=int(time.time());cfg=settings()
        data={s:rows(260,100+i*2,start=now-261*86400) for i,s in enumerate(('XLK','XLF','SPY'))}
        for bars in data.values():
            for bar in bars:
                bar.update(complete=True,market_open=True,quote_price=150,source='synthetic',market_timestamp=now)
        def fetch(symbol,limit,cfg):
            if symbol=='NVDA':return [dict(data['SPY'][-1],close=100,quote_price=100)]
            return data[symbol][-limit:]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'strategy.py').write_text((ROOT/'strategy.py').read_text())
            book={'initial_cash':10000,'cash':9800,'positions':{'NVDA':{'qty':2,'avg_price':100}},'orders':[], 'corporate_actions_through':'9999-01-01'}
            ledger=root/'paper.json';ledger.write_text(json.dumps(book))
            with patch.object(ratchet,'PROJECT_ROOT',root),patch.object(ratchet,'load_config',return_value=cfg),patch.object(ratchet,'get_recent_bars',side_effect=fetch),patch.object(brokers,'get_recent_bars',side_effect=fetch),patch.object(brokers,'PAPER_LEDGER_FILE',ledger),patch.object(brokers,'ensure_state_dir'),patch.object(journal,'JOURNAL_FILE',root/'journal.sqlite3'):
                first=ratchet.run_live_trading_cycle()
                self.assertEqual(first['status'],'completed')
                self.assertNotIn('NVDA',json.loads(ledger.read_text())['positions'])
                self.assertLessEqual(len(first['selected']),1)
                trades=[e['order_result']['order'] for e in first['executions'] if e['order_result']['status']=='filled']
                self.assertEqual(trades[0]['side'],'SELL')
                self.assertEqual(ratchet.run_live_trading_cycle()['status'],'already_rebalanced')
                self.assertEqual(len(journal.read_cycles()),1)

    def test_closed_market_never_invokes_strategy_or_mutates_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Path(tmp)/'paper.json';ledger.write_text(json.dumps({'cash':10000,'positions':{},'orders':[]}));before=ledger.read_bytes()
            with patch.object(ratchet,'load_config',return_value=settings()),patch.object(ratchet,'get_recent_bars',return_value=[{'market_open':False}]),patch.object(brokers,'PAPER_LEDGER_FILE',ledger),patch.object(brokers,'ensure_state_dir'),patch.object(ratchet,'run_strategy') as worker:
                self.assertEqual(ratchet.run_live_trading_cycle()['status'],'market_closed')
                self.assertEqual(ledger.read_bytes(),before)
                worker.assert_not_called()


class EvaluatorIntegrationTest(unittest.TestCase):
    def test_research_runs_portfolio_worker_and_keeps_holdout_out_of_score(self):
        data={s:rows(800,100+i*2) for i,s in enumerate(('XLK','XLF','SPY'))}
        with tempfile.TemporaryDirectory() as tmp,patch.object(evaluator,'STATE_DIR',Path(tmp)),patch.object(evaluator,'load_config',return_value=settings()):
            result=evaluator.evaluate_strategy(ROOT/'strategy.py',market_bars=data)
            self.assertEqual(result['evaluation_policy'],'shared_portfolio_daily_v1')
            self.assertEqual(len(result['validation_folds']),2)
            self.assertEqual(result['market_interval'],'1d')
            self.assertFalse(result['holdout']['used_for_promotion'])
            self.assertEqual(len(list((Path(tmp)/'datasets').glob('*.json'))),1)
            audit=evaluator.evaluate_strategy(ROOT/'strategy.py',market_bars=data,audit=True)
            self.assertIn('audit',audit)
            self.assertEqual(json.loads((Path(tmp)/'last_evaluation.json').read_text())['dataset_id'],result['dataset_id'])


class RatchetTest(unittest.TestCase):
    def test_research_continues_but_only_explicit_unfreeze_can_promote(self):
        baseline={'ratchet_score':2,'strategy_name':'base','validation_score':2,'train_score':2,'avg_sharpe':1,'avg_return_pct':1,'avg_max_drawdown_pct':1,'dataset_id':'frozen','portfolio':{'weight_path':[[.25,0]],'behavior_dates':['2020-01-01']}}
        candidate=dict(baseline,ratchet_score=2.1,strategy_name='candidate')
        original=(ROOT/'strategy.py').read_text()
        source=original.replace("'momentum_short': 20", "'momentum_short': 31")
        for switch in ('missing', True, False, 'false', 0, None, 'observation'):
            with self.subTest(switch=switch), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'snapshots').mkdir();(root/'strategy.py').write_text(original)
                cfg=settings()
                if switch == 'observation':
                    cfg.update(paper_observation_enabled=True,paper_execution_frozen=False)
                elif switch == 'missing':
                    cfg.pop('paper_execution_frozen',None)
                else:
                    cfg['paper_execution_frozen']=switch
                promoted=switch is False
                history={'current_generation':1,'best_score':999,'generations':[],'total_llm_cost_usd':0}
                with patch('atrader.observation.research_boundary',return_value={'task_id':'test'}),patch.object(ratchet,'PROJECT_ROOT',root),patch.object(ratchet,'STATE_DIR',root),patch.object(ratchet,'SNAPSHOTS_DIR',root/'snapshots'),patch.object(ratchet,'RATCHET_HISTORY_FILE',root/'history.json'),patch.object(ratchet,'load_config',return_value=cfg),patch.object(ratchet,'get_ratchet_history',return_value=history),patch.object(ratchet,'build_market_snapshot',return_value={s:rows(800) for s in ('XLK','XLF','SPY')}),patch.object(ratchet,'evaluate_strategy',side_effect=[baseline,candidate]) as evaluate,patch.object(ratchet,'call_deepseek_llm',return_value=('HYPOTHESIS: test\n```python\n'+source+'\n```',{'cost_usd':.001,'provider':'test','prompt_tokens':10,'completion_tokens':20})),patch.object(ratchet,'preview_portfolio'):
                    result=ratchet.run_code_ratchet_step()
                self.assertEqual(result['best_score'],2.1 if promoted else 2)
                self.assertEqual((root/'strategy.py').read_text(),source.strip()+'\n' if promoted else original)
                record=result['steps_executed'][0]
                self.assertEqual(record['status'],'LOCKED (KEEP)' if promoted else 'CANDIDATE (PAPER FROZEN)')
                self.assertTrue(record['backtest_qualified'])
                self.assertEqual(record['paper_execution_frozen'],not promoted)
                self.assertEqual(record['baseline_score_before'],2)
                self.assertEqual(record['prompt_tokens'],10)
                self.assertEqual(record['ratchet_score'],2.1)
                self.assertAlmostEqual(result['total_llm_cost_usd'],.001)
                campaign=json.loads((root/'research/campaign.json').read_text())
                self.assertEqual(len(campaign['trials']),1)
                self.assertEqual(len(campaign['forward_epochs']),2 if promoted else 1)
                self.assertEqual(len(list((root/'snapshots').glob('strategy_gen_*.py'))),1 if promoted else 0)
                self.assertTrue((root/record['candidate_artifact']).exists())
                self.assertEqual(json.loads((root/'last_evaluation.json').read_text())['strategy_name'],'candidate' if promoted else 'base')
                samples=[call.kwargs['market_bars'] for call in evaluate.call_args_list]
                self.assertIs(samples[0],samples[1])


class DashboardStateTest(unittest.TestCase):
    def test_preview_and_legacy_holding_remain_distinct(self):
        from atrader import web
        from types import SimpleNamespace
        account={'broker':'local_paper','positions':[{'symbol':'NVDA','qty':1,'avg_price':100,'last_price':110,'market_value_usd':110}]}
        portfolio={'accounts':[account],'virtual_master_ledger':{'total_equity_usd':10000}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'strategy.py').write_text('preview')
            (root/'portfolio_preview.json').write_text(json.dumps({'status':'preview_only','timestamp':1,'signal_asof':'2026-10-14','selected':['XLK'],'executions':[{'symbol':'XLK','score':2,'rank':1,'effective_target_weight':.25}]}))
            with patch.object(web,'STATE_DIR',root),patch.object(web,'PROJECT_ROOT',root),patch.object(web,'load_config',return_value=settings()),patch.object(web,'MultiBrokerRouter',return_value=SimpleNamespace(get_unified_portfolio=lambda:portfolio)),patch.object(web,'read_cycles',return_value=[]):
                result=web.collect_dashboard_state()
            self.assertEqual(result['portfolio_plan']['status'],'preview_only')
            self.assertFalse(result['positions'][0]['in_scope'])
            self.assertEqual(result['positions'][0]['target_weight_pct'],0)
            self.assertTrue(result['watchlist'][0]['selected'])


class HelperMethodTest(unittest.TestCase):
    def test_private_pure_helpers_work_without_allowing_dunder_introspection(self):
        source="""class Strategy:
 name='helper_test'
 def __init__(self): self.params={}
 def _score(self,bars): return bars[-1]['close']
 def rank_assets(self,h,p):
  return {s:{'score':self._score(b),'eligible':True,'exposure':1} for s,b in h.items() if s!='SPY'}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'strategy.py';path.write_text(source)
            result=run_strategy(path,{'operation':'decision','history':{s:rows(2) for s in ('XLK','XLF','SPY')},'positions':{},'config':evaluator.harness_config(settings())})
            self.assertEqual(len(result['selected']),1)
        with self.assertRaises(ValueError):validate_source(source.replace("bars[-1]['close']","self.__class__"))

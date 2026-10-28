import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from atrader import alpaca, evaluator, research, rebaseline
from atrader.config import write_json_atomic
from test_portfolio_harness import settings, rows, ROOT


class CorporateActionTest(unittest.TestCase):
    def test_dividends_match_split_adjusted_units_without_using_future_splits(self):
        class Client:
            def request(self,path,**kwargs):
                return {'corporate_actions':{
                    'forward_splits':[{'symbol':'XLK','ex_date':'2024-12-01','new_rate':2,'old_rate':1},
                        {'symbol':'XLK','ex_date':'2027-01-01','new_rate':10,'old_rate':1}],
                    'cash_dividends':[{'id':'a','symbol':'XLK','ex_date':'2023-06-01','rate':4},
                        {'id':'a','symbol':'XLK','ex_date':'2023-06-01','rate':4},
                        {'id':'b','symbol':'XLK','ex_date':'2023-06-01','rate':1},
                        {'id':'c','symbol':'XLK','ex_date':'2025-06-01','rate':3},
                        {'id':'future','symbol':'XLK','ex_date':'2027-01-01','rate':99}]},'next_page_token':None}
        cash=alpaca.cash_dividends(Client(),'XLK','2023-01-01','2026-10-27')
        self.assertEqual(cash,{'2023-06-01':2.5,'2025-06-01':3})

    def test_corporate_action_pagination_and_failing_source_are_not_silently_ignored(self):
        class Client:
            def __init__(self):self.calls=0
            def request(self,path,**kwargs):
                self.calls+=1
                return {'corporate_actions':{'cash_dividends':[{'id':str(self.calls),'symbol':'XLF','ex_date':'2025-06-01','rate':1}]},'next_page_token':'next' if self.calls==1 else None}
        client=Client();self.assertEqual(alpaca.cash_dividends(client,'XLF','2025-01-01','2026-01-01'),{'2025-06-01':2})
        self.assertEqual(client.calls,2)
        with patch.object(client,'request',side_effect=RuntimeError('API unavailable')):
            with self.assertRaises(RuntimeError):alpaca.cash_dividends(client,'XLF','2025-01-01','2026-01-01')


class RebaselineTest(unittest.TestCase):
    def setup_run(self,root):
        cfg=settings();cfg['market_data_source']='yahoo'
        market={s:rows(1000,100+i*2) for i,s in enumerate(('XLK','XLF','SPY'))}
        (root/'strategy.py').write_text((ROOT/'strategy.py').read_text())
        state=root/'state';campaign,path=research.campaign_state(state,cfg,lambda _:market)
        evaluation=evaluator.evaluate_strategy(root/'strategy.py',market_bars=market,cfg_override=cfg,persist=False)
        research.archive_candidate(campaign,path.parent,7,(root/'strategy.py').read_text(),evaluation)
        campaign['trials']=[{'generation':i,'timestamp':1,'pathway':'independent','status':'reverted','cost_usd':.1} for i in range(1,4)]
        campaign['cost_usd']=.3
        write_json_atomic(path,campaign)
        history={'current_generation':10,'best_score':evaluation['ratchet_score'],'best_strategy_name':evaluation['strategy_name'],
            'generations':[{'generation':i,'timestamp':1,'llm_cost_usd':.1} for i in range(1,4)],'total_llm_cost_usd':.3}
        write_json_atomic(state/'ratchet_history.json',history)
        write_json_atomic(state/'last_evaluation.json',evaluation)
        legacy=state/'archives/old_local';legacy.mkdir(parents=True)
        write_json_atomic(state/'alpaca_migration.json',{'local_archive':str(legacy)})
        write_json_atomic(state/'paper_account.json',{'cash':10000,'positions':{},'orders':[]})
        write_json_atomic(state/'alpaca_execution.json',{'session':'preserve-broker-intents'})
        return cfg,market,campaign,evaluation

    def test_active_aliases_deduplicated_scores_updated_old_run_aborted_costs_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,market,old,previous=self.setup_run(root)
            new=deepcopy(market)
            for bars in new.values():bars[450]['dividend']=.5
            for bars in new.values():
                for bar in bars[800:]:
                    for key in ('open','high','low','close'):bar[key]*=10
            folder,manifest=rebaseline.prepare(root,cfg,new)
            self.assertEqual(manifest['unique_active_strategies'],1)
            self.assertEqual(manifest['results'][0]['aliases'],['incumbent','archive:7'])
            self.assertLessEqual(manifest['research_end'],old['research_end'])
            self.assertEqual(manifest['inherited_trial_count'],3)
            self.assertEqual(manifest['new_trial_count'],4)
            self.assertNotEqual(manifest['results'][0]['new_score'],previous['ratchet_score'])
            self.assertEqual(json.loads((root/'state/last_evaluation.json').read_text())['market_data_source'],'yahoo')
            broker=(root/'state/alpaca_execution.json').read_bytes()
            summary=rebaseline.commit(root,folder)
            updated=json.loads((root/'state/research/campaign.json').read_text())
            history=json.loads((root/'state/ratchet_history.json').read_text())
            self.assertEqual(updated['cost_usd'],.3)
            self.assertEqual(history['total_llm_cost_usd'],.3)
            self.assertEqual(len(updated['trials']),4)
            self.assertEqual(len(history['generations']),4)
            self.assertFalse(summary['strategy_changed'])
            self.assertEqual(json.loads((root/'state/last_evaluation.json').read_text())['market_data_source'],'alpaca')
            self.assertEqual((root/'state/alpaca_execution.json').read_bytes(),broker)
            self.assertFalse((root/'state/paper_account.json').exists())
            self.assertEqual(json.loads((root/'state/retired_paper_run.json').read_text())['status'],'ABORTED')
            self.assertEqual(json.loads((Path(summary['previous_research_archive'])/'research/campaign.json').read_text())['status'],'superseded')
            research.campaign_state(root/'state',dict(cfg,market_data_source='alpaca'),lambda _:self.fail('must remain frozen'))
            self.assertTrue((root/'state/datasets'/(manifest['results'][0]['evaluation']['dataset_id']+'.json')).exists())

    def test_failed_incumbent_keeps_published_scores_and_checkpoints_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,market,old,_=self.setup_run(root)
            before=(root/'state/last_evaluation.json').read_bytes()
            with patch.object(rebaseline,'evaluate_strategy',side_effect=ValueError('invalid evaluation')):
                with self.assertRaisesRegex(ValueError,'Incumbent failed'):rebaseline.prepare(root,cfg,market)
            self.assertEqual((root/'state/last_evaluation.json').read_bytes(),before)
            pending=json.loads((root/'state/research_rebase_pending/state/research/campaign.json').read_text())
            self.assertEqual(len(pending['trials']),4)
            self.assertEqual(pending['trials'][-1]['status'],'failed')
            self.assertEqual(json.loads((root/'state/research/campaign.json').read_text())['id'],old['id'])

    def test_manual_backtest_reuses_campaign_instead_of_overwriting_with_new_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,_,campaign,_=self.setup_run(root)
            with patch.object(evaluator,'STATE_DIR',root/'state'),patch.object(evaluator,'load_config',return_value=cfg),patch.object(evaluator,'build_market_snapshot',side_effect=AssertionError('must not refetch')):
                result=evaluator.evaluate_strategy(root/'strategy.py')
            self.assertEqual(result['data_range']['end'],campaign['research_end'])
            self.assertEqual(result['holdout']['reserved_sessions'],campaign['reserved_sessions'])
    def test_commit_rejects_a_racing_strategy_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg,market,_,_=self.setup_run(root)
            folder,_=rebaseline.prepare(root,cfg,market)
            (root/'strategy.py').write_text((root/'strategy.py').read_text()+'\n# changed\n')
            with self.assertRaisesRegex(ValueError,'changed during'):rebaseline.commit(root,folder)

    def test_final_exam_respects_pinned_boundary_when_reserve_is_not_exactly_twenty_percent(self):
        cfg=settings();market={s:rows(1000,100+i*2) for i,s in enumerate(('XLK','XLF','SPY'))}
        result=evaluator.evaluate_strategy(ROOT/'strategy.py',market_bars=market,cfg_override=cfg,persist=False,
            audit=True,audit_reserved_sessions=201)
        from atrader.portfolio import session_date
        self.assertEqual(result['audit']['start_date'],session_date(market['XLK'][799]['timestamp']))
        self.assertEqual(result['audit']['end_date'],session_date(market['XLK'][999]['timestamp']))
    def test_data_identity_includes_provider_even_when_prices_are_identical(self):
        cfg=settings();market={'x':[]}
        self.assertNotEqual(evaluator.dataset_identity(market,dict(cfg,market_data_source='yahoo')),
                            evaluator.dataset_identity(market,dict(cfg,market_data_source='alpaca')))

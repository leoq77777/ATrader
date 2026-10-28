import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from test_portfolio_harness import settings, rows, ROOT
from atrader import research, ratchet, evaluator, daemon


def evaluation(score=2, weights=None):
    return {'ratchet_score':score, 'strategy_name':'test', 'train_score':score, 'validation_score':score,
        'avg_sharpe':1, 'avg_return_pct':1, 'avg_max_drawdown_pct':1, 'dataset_id':'frozen',
        'portfolio':{'weight_path': weights or [[.25,0],[.25,0]], 'behavior_dates':['a','b']}}


class ResearchSearchTest(unittest.TestCase):
    def test_campaign_freezes_sample_limits_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            loader=lambda cfg:{s:rows(800) for s in ('XLK','XLF','SPY')}
            campaign,path=research.campaign_state(root,settings(),loader)
            market,layout=research.load_sample(path)
            self.assertEqual(len(market['XLK']),640)
            self.assertEqual(layout['reserved_sessions'],160)
            again,_=research.campaign_state(root,settings(),lambda _:self.fail('must not reload'))
            self.assertEqual(campaign['id'],again['id'])
            self.assertEqual((path.parent/'sealed_holdout.json').stat().st_mode & 0o777,0o600)
            with self.assertRaisesRegex(ValueError,'settings changed'):
                research.campaign_state(root,dict(settings(),min_score_improvement=.5),loader)
            (path.parent/'sample.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):research.load_sample(path)

    def test_independent_context_has_no_incumbent_scores_or_experiment_history(self):
        market={s:rows(800) for s in ('XLK','XLF','SPY')}
        text,parent=research.proposal_context('independent','INCUMBENT_SECRET',evaluation(12345),{'archive':[]},market,Path('/tmp'))
        self.assertNotIn('INCUMBENT_SECRET',text)
        self.assertNotIn('12345',text)
        self.assertNotIn('momentum',text.lower())
        self.assertIsNone(parent)

    def test_archive_branch_does_not_see_incumbent_and_empty_archive_falls_back(self):
        market={s:rows(800) for s in ('XLK','XLF','SPY')}
        source=(ROOT/'strategy.py').read_text()
        branch=source.replace("'momentum_short': 20","'momentum_short': 31")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);campaign={'archive':[],'limits':deepcopy(research.LIMITS)}
            research.archive_candidate(campaign,root,1,branch,evaluation())
            text,parent=research.proposal_context('archive',source,evaluation(),campaign,market,root)
            self.assertEqual(parent,1)
            self.assertIn(branch,text)
            self.assertNotIn('Incumbent strategy',text)
            text,parent=research.proposal_context('archive',branch,evaluation(),campaign,market,root)
            self.assertIsNone(parent)
            self.assertNotIn(branch,text)

    def test_archive_keeps_novel_quality_and_replaces_similar_inferior_member(self):
        source=(ROOT/'strategy.py').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);campaign={'archive':[],'limits':deepcopy(research.LIMITS)}
            self.assertEqual(research.archive_candidate(campaign,root,1,source,evaluation())[0],'accepted')
            self.assertEqual(research.archive_candidate(campaign,root,2,source,evaluation(1.99))[0],'similar_behavior')
            self.assertEqual(research.archive_candidate(campaign,root,3,source,evaluation(1.9,[[0,.25],[0,.25]]))[0],'accepted')
            self.assertEqual(len(campaign['archive']),2)
            self.assertEqual(research.archive_candidate(campaign,root,4,source,evaluation(.5,[[0,0],[0,0]]))[0],'below_quality_floor')
            research.archive_candidate(campaign,root,5,source,evaluation(2.01))
            self.assertEqual({x['generation'] for x in campaign['archive']},{3,5})
            self.assertAlmostEqual(research.behavior_distance(evaluation()['portfolio'],evaluation(weights=[[0,.25],[0,.25]])['portfolio']),.5)

    def test_ast_hash_ignores_comments_and_whitespace(self):
        self.assertEqual(research.structural_hash('x = 1 # note\n'),research.structural_hash('x=1\n'))

    def test_trial_failure_duplicates_costs_and_budget_stop_survive_restart(self):
        source=(ROOT/'strategy.py').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'snapshots').mkdir();(root/'strategy.py').write_text(source)
            history={'current_generation':8,'best_score':2,'generations':[],'total_llm_cost_usd':0}
            with patch.object(ratchet,'PROJECT_ROOT',root),patch.object(ratchet,'STATE_DIR',root),patch.object(ratchet,'SNAPSHOTS_DIR',root/'snapshots'),patch.object(ratchet,'RATCHET_HISTORY_FILE',root/'history.json'),patch.object(ratchet,'load_config',return_value=settings()),patch.object(ratchet,'get_ratchet_history',return_value=history),patch.object(ratchet,'build_market_snapshot',return_value={s:rows(800) for s in ('XLK','XLF','SPY')}) as loader,patch.object(ratchet,'evaluate_strategy',return_value=evaluation()),patch.object(ratchet,'call_deepseek_llm',return_value=('HYPOTHESIS: duplicate\n```python\n'+source+'\n```',{'provider':'test','cost_usd':.003})),patch.object(ratchet,'preview_portfolio'):
                first=ratchet.run_code_ratchet_step()
                second=ratchet.run_code_ratchet_step()
                self.assertEqual(loader.call_count,1)
                self.assertIn('Duplicate AST',first['steps_executed'][0]['error'])
                self.assertEqual(second['steps_executed'][0]['research_pathway'],'incumbent')
                path=root/'research/campaign.json';campaign=json.loads(path.read_text())
                self.assertEqual(len(campaign['trials']),2)
                self.assertAlmostEqual(campaign['cost_usd'],.006)
                campaign['limits']['max_trials']=2;path.write_text(json.dumps(campaign))
                with patch.object(ratchet,'call_deepseek_llm') as api:
                    stopped=ratchet.run_code_ratchet_step()
                    api.assert_not_called()
                self.assertEqual(stopped['steps_executed'],[])
                self.assertEqual(stopped['research_status'],'trial_budget_exhausted')
                self.assertEqual((root/'strategy.py').read_text(),source)

    def test_research_worker_never_receives_reserved_bars(self):
        market={s:rows(800) for s in ('XLK','XLF','SPY')}
        real=evaluator.run_strategy
        def capture(path,request):
            self.assertEqual(len(request['market']['XLK']),640)
            self.assertLess(request['market']['XLK'][-1]['timestamp'],market['XLK'][640]['timestamp'])
            return real(path,request)
        with tempfile.TemporaryDirectory() as tmp,patch.object(evaluator,'STATE_DIR',Path(tmp)),patch.object(evaluator,'load_config',return_value=settings()),patch.object(evaluator,'run_strategy',side_effect=capture):
            result=evaluator.evaluate_strategy(ROOT/'strategy.py',market_bars=market)
            self.assertEqual(len(result['portfolio']['weight_path']),320)

    def test_final_exam_is_explicit_after_budget_and_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            campaign,path=research.campaign_state(root,settings(),lambda cfg:{s:rows(800) for s in ('XLK','XLF','SPY')})
            with self.assertRaisesRegex(ValueError,'Finish'):
                research.audit_campaign(root,settings(),ROOT/'strategy.py')
            campaign['limits']['max_trials']=0;path.write_text(json.dumps(campaign))
            with patch.object(evaluator,'evaluate_strategy',return_value={'audit':'test'}) as audit:
                self.assertEqual(research.audit_campaign(root,settings(),ROOT/'strategy.py'),{'audit':'test'})
                self.assertEqual(len(audit.call_args.kwargs['market_bars']['XLK']),800)
                with self.assertRaisesRegex(ValueError,'already opened'):
                    research.audit_campaign(root,settings(),ROOT/'strategy.py')
                self.assertEqual(audit.call_count,1)

    def test_scheduler_budget_closed_campaign_does_not_index_empty_steps(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(daemon,'STATE_DIR',Path(tmp)),patch.object(daemon,'DAEMON_STATE_FILE',Path(tmp)/'daemon.json'),patch.object(daemon,'load_config',return_value={'ratchet_mode':'code_ratchet'}),patch.object(daemon,'run_live_trading_cycle',return_value={'status':'already_rebalanced'}),patch.object(daemon,'run_code_ratchet_step',return_value={'steps_executed':[],'research_status':'trial_budget_exhausted'}):
            instance=daemon.AutonomousDaemon();instance._tick(100000)
            state=instance.get_state()
            self.assertEqual(state['research_status'],'trial_budget_exhausted')
            self.assertFalse(any(log['type']=='ERROR' for log in state['recent_logs']))


class CostAccountingTest(unittest.TestCase):
    def test_new_york_day_and_pending_usage_count_without_double_counting(self):
        # 00:30 UTC is still the previous New York calendar day.
        timestamp=1793051400
        day=research.session_date(timestamp)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'research').mkdir()
            (root/'ratchet_history.json').write_text(json.dumps({'generations':[
                {'generation':1,'timestamp':timestamp,'llm_cost_usd':.1}]}))
            (root/'research/campaign.json').write_text(json.dumps({'trials':[
                {'generation':1,'timestamp':timestamp,'cost_usd':.1},
                {'generation':2,'timestamp':timestamp,'cost_usd':.2}]}))
            self.assertAlmostEqual(research.recorded_daily_cost(root,day),.3)
            self.assertEqual(research.recorded_daily_cost(root,'1900-01-01'),0)

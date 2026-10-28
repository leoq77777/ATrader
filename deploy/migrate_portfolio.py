"""Reviewed scope migration. Back up state, config and old strategy before deploying.

Existing market-backed cash/holdings are preserved; out-of-scope holdings become
liquidation-only at the next US-session portfolio rebalance. No real orders exist.
"""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atrader.config import STATE_DIR, ENV_FILE, CONFIG_FILE, load_config, write_json_atomic
from atrader.universe import SECTORS, BENCHMARK, UNIVERSE_VERSION
from atrader.portfolio import POLICY_VERSION, session_date
from atrader.evaluator import evaluate_strategy


def migrate(configure_only=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    saved = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    saved.update(watchlist=list(SECTORS), benchmark_symbol=BENCHMARK, investment_scope=UNIVERSE_VERSION,
                 market_interval='1d', research_bars=1000, execution_mode='paper', active_brokers=['local_paper'],
                 max_holdings=3, rank_buffer=1, max_position_pct=.25, max_gross_exposure=.75, ratchet_mode='code_ratchet')
    write_json_atomic(CONFIG_FILE, saved)
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    replace = {'ACTIVE_BROKERS':'local_paper','RATCHET_MODE':'code_ratchet','MARKET_DATA_SOURCE':'yahoo'}
    lines = [line for line in lines if line.partition('=')[0].strip() not in replace]
    ENV_FILE.write_text('\n'.join(lines+[k+'='+v for k,v in replace.items()])+'\n')
    ENV_FILE.chmod(0o600)
    if configure_only:
        return
    history_file=STATE_DIR/'ratchet_history.json'
    history=json.loads(history_file.read_text()) if history_file.exists() else {'current_generation':0,'generations':[],'total_llm_cost_usd':0}
    if history.get('evaluation_policy') == POLICY_VERSION:
        print('Portfolio harness already initialized; preserving ledger and evaluation history.')
        return
    evaluation=evaluate_strategy()
    history.update(best_score=evaluation['ratchet_score'],best_strategy_name=evaluation['strategy_name'],evaluation_policy=POLICY_VERSION)
    history.setdefault('generations',[]).append({'generation':history['current_generation'],'timestamp':int(time.time()),
        'status':'BASELINE (PORTFOLIO HARNESS)','strategy_name':evaluation['strategy_name'],
        'hypothesis':'Reviewed scope migration: shared-cash US-sector daily portfolio; prior scores are not comparable.',
        'ratchet_score':evaluation['ratchet_score'],'validation_score':evaluation['validation_score'],
        'avg_sharpe':evaluation['avg_sharpe'],'avg_return_pct':evaluation['avg_return_pct'],
        'avg_max_drawdown_pct':evaluation['avg_max_drawdown_pct'],'provider':'rebaseline','llm_cost_usd':0,
        'evaluation_policy':POLICY_VERSION,'market_data_source':'yahoo','investment_scope':UNIVERSE_VERSION,'dataset_id':evaluation['dataset_id']})
    write_json_atomic(history_file,history)
    ledger=STATE_DIR/'paper_account.json'
    if ledger.exists():
        book=json.loads(ledger.read_text())
        book['corporate_actions_through']=session_date(time.time())
        book.pop('last_rebalance_session',None)
        write_json_atomic(ledger,book)
        print('Preserved paper cash and holdings; liquidation-only legacy symbols:',[s for s in book.get('positions',{}) if s not in SECTORS])
    state_file=STATE_DIR/'daemon_state.json'
    if state_file.exists():
        state=json.loads(state_file.read_text());state.update(last_trade_ts=0,last_trade_attempt_ts=0,last_evolve_ts=0,last_evolve_attempt_ts=0)
        write_json_atomic(state_file,state)
    from atrader.ratchet import preview_portfolio
    preview_portfolio()
    print('Portfolio baseline:',evaluation['ratchet_score'],'dataset:',evaluation['dataset_id'][:12])


if __name__=='__main__':
    migrate('--configure-only' in sys.argv)

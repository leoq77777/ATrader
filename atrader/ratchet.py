"""Single-file strategy research with a fixed portfolio harness and paper execution."""
import fcntl
import hashlib
import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from uuid import uuid4

from atrader.config import PROJECT_ROOT, STATE_DIR, load_config, write_json_atomic
from atrader.evaluator import evaluate_strategy, build_market_snapshot, harness_config, dataset_identity
from atrader.market_data import get_recent_bars
from atrader.portfolio import POLICY_VERSION, align_bars, rebalance, mark_equity, session_date
from atrader.strategy_runner import run_strategy, validate_source
from atrader.brokers import LocalPaperBroker, MultiBrokerRouter
from atrader.journal import record_cycle
from atrader.universe import UNIVERSE_VERSION
from atrader.providers import model_configuration_id
from atrader.research import (SEARCH_POLICY, PATHS, campaign_state, load_sample, stop_reason,
    proposal_context, structural_hash, archive_candidate, recorded_daily_cost)

RATCHET_HISTORY_FILE = STATE_DIR / 'ratchet_history.json'
SIGNAL_REGIME_FILE = STATE_DIR / 'signal_regime.json'
SNAPSHOTS_DIR = STATE_DIR / 'snapshots'
def call_deepseek_llm(
    system_prompt: str,
    user_prompt: str,
    cfg: Dict[str, Any],
    temperature: float = 0.4,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """
    Calls DeepSeek (or any OpenAI-compatible API) using standard library urllib.
    Returns (response_text, usage_metadata_with_usd_cost).
    """
    api_key = cfg.get("llm_api_key", "").strip()
    if not api_key or api_key == "your_deepseek_api_key_here":
        return None, {
            "provider": "heuristic_fallback (no LLM_API_KEY set)",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        }

    base_url = cfg.get("llm_base_url", "https://api.deepseek.com").rstrip("/")
    endpoint = f"{base_url}/chat/completions" if not base_url.endswith("/chat/completions") else base_url
    model = cfg.get("llm_model", "deepseek-chat")

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    p_tok = int(usage.get("prompt_tokens", 600))
    c_tok = int(usage.get("completion_tokens", 500))

    in_rate = float(cfg.get("llm_cost_input_per_1m", 0.27))
    out_rate = float(cfg.get("llm_cost_output_per_1m", 1.10))
    cost_usd = (p_tok / 1_000_000.0) * in_rate + (c_tok / 1_000_000.0) * out_rate

    return content, {
        "provider": model,
        "model_configuration_id": model_configuration_id(cfg),
        "model_reported": data.get("model"),
        "model_version_reported": data.get("model_version"),
        "system_fingerprint": data.get("system_fingerprint"),
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "cost_usd": round(cost_usd, 6),
    }


def get_ratchet_history():
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    if RATCHET_HISTORY_FILE.exists():
        return json.loads(RATCHET_HISTORY_FILE.read_text())
    evaluation = evaluate_strategy()
    history = {'created_at': int(time.time()), 'current_generation': 0, 'best_score': evaluation['ratchet_score'],
        'best_strategy_name': evaluation['strategy_name'], 'total_llm_cost_usd': 0.0, 'generations': []}
    write_json_atomic(RATCHET_HISTORY_FILE, history)
    return history


def run_code_ratchet_step(steps=1, force_mock=False):
    # CLI and daemon cannot race on the same candidate, generation or campaign.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / 'research.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**get_ratchet_history(), 'steps_executed': [], 'research_status': 'busy'}
        return _run_research_steps(steps, force_mock)


def _run_research_steps(steps=1, force_mock=False):
    cfg = load_config()
    if force_mock:
        cfg['llm_api_key'] = ''
    history = get_ratchet_history()
    strategy_path = PROJECT_ROOT / 'strategy.py'
    outputs = []
    campaign, campaign_path = campaign_state(STATE_DIR, cfg, build_market_snapshot)
    campaign.setdefault('legacy_trials', len(history['generations']))
    market, layout = load_sample(campaign_path)
    boundary = {}
    if cfg.get('paper_observation_enabled'):
        from atrader.observation import research_boundary
        boundary = research_boundary(STATE_DIR, campaign)
        campaign['task_id'] = boundary['task_id']
    for _ in range(max(1, min(steps, 10))):
        reason = stop_reason(campaign)
        if reason:
            campaign['status'] = reason
            write_json_atomic(campaign_path, campaign)
            break
        daemon_path = STATE_DIR / 'daemon_state.json'
        daily_limit = json.loads(daemon_path.read_text()).get('daily_budget_usd', .5) if daemon_path.exists() else .5
        if recorded_daily_cost(STATE_DIR, session_date(time.time())) >= daily_limit:
            return {**history, 'steps_executed': outputs, 'research_status': 'daily_budget_exhausted'}
        code = strategy_path.read_text()
        baseline = evaluate_strategy(strategy_path, market_bars=market, research_layout=layout)
        generation = max([history['current_generation']] + [t['generation'] for t in campaign['trials']]) + 1
        if not campaign['archive']:
            archive_candidate(campaign, campaign_path.parent, generation-1, code, baseline)
            campaign.setdefault('forward_epochs', [{'generation': generation-1, 'started_at': int(time.time()),
                'source_hash': hashlib.sha256(code.encode()).hexdigest(), 'status': 'awaiting_forward_observations'}])
        pathway = PATHS[len(campaign['trials']) % len(PATHS)]
        user, parent_generation = proposal_context(pathway, code, baseline, campaign, market, campaign_path.parent)
        if pathway == 'archive' and parent_generation is None:
            pathway = 'independent'
        # Reserve the attempt before API/evaluation; crashes still consume the trial budget.
        trial = {'generation': generation, 'pathway': pathway, 'status': 'started', 'timestamp': int(time.time())}
        campaign['trials'].append(trial)
        write_json_atomic(campaign_path, campaign)
        prompt = (
            'Propose ONLY the Strategy class in strategy.py. Output HYPOTHESIS: one concise explanation, then complete code in a python fence.\n'
            'Interface: Strategy.name, description, params; rank_assets(history, positions) -> mapping of every tradable symbol to '
            '{score: finite float, eligible: bool, exposure: float in [0,1], reason: string, indicators: mapping of finite numbers}. '
            'history contains copies of CLOSED DAILY bars for all sectors and the SPY benchmark; positions contains quantities. '
            'Scoring must be stateless: each invocation receives a fresh strategy namespace. Reconstruct any state from history and positions. Only math may be imported. No filesystem/network/process access, introspection, magic methods except __init__, '
            'decorators, global/nonlocal or modifying modules. Pure helper methods such as self._momentum are allowed; only self.params may be assigned as an instance attribute. No order execution in strategy. '
            'The FIXED harness ranks industries, keeps up to 3 with an exit-rank buffer, caps each at 25% and gross at 75%. '
            'Shared cash, sells before buys, next-day open fills, costs and cash dividends are fixed. '
            'Two chronological validation folds determine promotion; a final holdout is never shown or used in promotion. '
            'Do not hardcode dates, prices, symbols beyond using SPY as benchmark, or return guessed performance. '
            'Use broadly applicable, causal rules. Respect the limited historical sample; do not claim the incumbent is proven.'
        )
        write_json_atomic(campaign_path.parent / f'prompt_{len(campaign["trials"])}.json',
            {'system': prompt, 'user': user, 'pathway': pathway, 'parent_generation': parent_generation,
             'research_boundary': boundary, 'sample_hash': campaign['sample_hash'], 'research_cutoff': campaign['research_end'],
             'request': {'model':cfg.get('llm_model','deepseek-chat'),'temperature':.4,
                 'messages':[{'role':'system','content':prompt},{'role':'user','content':user}]}})
        usage = {'provider': 'local_heuristic', 'cost_usd': 0.0, 'prompt_tokens': 0, 'completion_tokens': 0}
        response = None
        try:
            response, usage = call_deepseek_llm(prompt, user, cfg)
        except Exception as exc:
            usage['provider'] = 'fallback_after_error'
            usage['error'] = str(exc)
        if response:
            match = re.search(r'```python\s*(.*?)```', response, re.S)
            candidate = match.group(1).strip() if match else ''
            hypothesis = re.search(r'HYPOTHESIS:\s*(.+)', response)
            hypothesis = hypothesis.group(1) if hypothesis else 'Model-proposed industry scoring change'
        else:
            # Deterministic offline smoke experiment, not a replacement for model research.
            candidate = code.replace("'momentum_short': 20", "'momentum_short': 30", 1) if force_mock else ''
            hypothesis = 'Offline smoke experiment: test a slower short momentum window' if force_mock else 'Model request failed or returned no proposal'
        experiments = STATE_DIR / 'experiments'
        experiments.mkdir(parents=True, exist_ok=True)
        candidate_hash = hashlib.sha256(candidate.encode()).hexdigest()
        artifact = experiments / f'strategy_gen_{generation}_{candidate_hash[:12]}.py'
        artifact.write_text(candidate)
        temporary = STATE_DIR / 'candidate_strategy_tmp.py'
        cost = float(usage.get('cost_usd', 0))
        error, kept, qualified = usage.get('error'), False, False
        frozen = bool(cfg.get('paper_observation_enabled')) or cfg.get('paper_execution_frozen', True) is not False
        candidate_eval = baseline
        archive_status, distance = 'not_evaluated', None
        # Store paid usage immediately, even if a later worker or persistence step fails.
        campaign['cost_usd'] = round(campaign['cost_usd'] + cost, 6)
        trial.update(cost_usd=cost, provider=usage.get('provider'), prompt_tokens=usage.get('prompt_tokens'),
                     completion_tokens=usage.get('completion_tokens'))
        write_json_atomic(campaign_path, campaign)
        try:
            if not response and not force_mock:
                raise ValueError(error or 'No model proposal received; no synthetic research substituted')
            validate_source(candidate)
            normalized = structural_hash(candidate)
            duplicates = {t.get('structural_hash') for t in campaign['trials'][:-1]}
            duplicates.add(structural_hash(code))
            legacy_duplicate = candidate_hash in {g.get('candidate_source_hash') for g in history['generations']}
            trial['structural_hash'] = normalized
            if normalized in duplicates or legacy_duplicate:
                raise ValueError('Duplicate AST: identical proposal already seen in this campaign')
            temporary.write_text(candidate)
            candidate_eval = evaluate_strategy(temporary, llm_cost_usd=cost, market_bars=market, research_layout=layout)
            archive_status, distance = archive_candidate(campaign, campaign_path.parent, generation, candidate, candidate_eval)
            qualified = candidate_eval['ratchet_score'] >= baseline['ratchet_score'] + float(cfg.get('min_score_improvement', .015))
            kept = qualified and not frozen
        except Exception as exc:
            error = str(exc)
        finally:
            temporary.unlink(missing_ok=True)
        if kept:
            campaign.setdefault('forward_epochs', []).append({'generation': generation,
                'started_at': int(time.time()), 'source_hash': candidate_hash, 'status': 'awaiting_forward_observations'})
            staging = strategy_path.with_suffix('.next.py')
            staging.write_text(candidate+'\n')
            staging.replace(strategy_path)
            (SNAPSHOTS_DIR / f'strategy_gen_{generation}.py').write_text(candidate+'\n')
            chosen = candidate_eval
        else:
            chosen = baseline
        write_json_atomic(STATE_DIR / 'last_evaluation.json', chosen)
        score = candidate_eval['ratchet_score'] if error is None else -999.0
        status = 'LOCKED (KEEP)' if kept else 'CANDIDATE (PAPER FROZEN)' if qualified and error is None else 'REVERTED (DISCARD)'
        record = {'generation': generation, 'timestamp': int(time.time()), 'status': status,
            'paper_execution_frozen': frozen, 'backtest_qualified': qualified and error is None,
            'strategy_name': candidate_eval['strategy_name'], 'hypothesis': hypothesis, 'error': error,
            'ratchet_score': score, 'baseline_score_before': baseline['ratchet_score'], 'score_delta': score-baseline['ratchet_score'],
            'validation_score': candidate_eval['validation_score'], 'avg_sharpe': candidate_eval['avg_sharpe'],
            'avg_return_pct': candidate_eval['avg_return_pct'], 'avg_max_drawdown_pct': candidate_eval['avg_max_drawdown_pct'],
            'llm_cost_usd': cost, 'provider': usage.get('provider'), 'prompt_tokens': usage.get('prompt_tokens'),
            'completion_tokens': usage.get('completion_tokens'), 'dataset_id': baseline['dataset_id'],
            **{key: usage.get(key) for key in ('model_configuration_id', 'model_reported', 'model_version_reported', 'system_fingerprint')},
            'candidate_source_hash':candidate_hash, 'candidate_artifact':str(artifact.relative_to(STATE_DIR)),
            'search_policy': SEARCH_POLICY, 'research_pathway': pathway, 'parent_generation': parent_generation,
            'campaign_id': campaign['id'], 'campaign_trial': len(campaign['trials']),
            'archive_status': archive_status, 'behavior_distance': distance,
            'usage_error': usage.get('error'),
            'evaluation_policy': POLICY_VERSION, 'market_data_source': cfg['market_data_source'], 'investment_scope': UNIVERSE_VERSION}
        history.update(current_generation=generation, best_score=chosen['ratchet_score'], best_strategy_name=chosen['strategy_name'],
            total_llm_cost_usd=round(history.get('total_llm_cost_usd', 0)+cost, 6), evaluation_policy=POLICY_VERSION)
        write_json_atomic(artifact.with_suffix('.json'), record)
        history['generations'].append(record)
        write_json_atomic(RATCHET_HISTORY_FILE, history)
        trial.update(status=record['status'], error=error, archive_status=archive_status)
        campaign['status'] = stop_reason(campaign) or 'active'
        write_json_atomic(campaign_path, campaign)
        outputs.append(record)
        # Preview must use current closed data, never the frozen historical research end.
        try:
            preview_portfolio()
        except Exception as exc:
            write_json_atomic(STATE_DIR / 'preview_error.json', {'timestamp': int(time.time()), 'error': str(exc)})
    return {**history, 'steps_executed': outputs, 'research_status': campaign['status']}


def run_signal_ratchet_update(symbols=None):
    """Optional advisory snapshot only; it never changes portfolio allocations."""
    from atrader.indicators import calc_ema, calc_rsi
    cfg = load_config()
    watch = symbols or cfg['watchlist']
    if not set(watch) <= set(cfg['watchlist']):
        raise ValueError('Advisory symbols outside universe')
    regimes = {}
    for symbol in watch:
        bars = [b for b in get_recent_bars(symbol, 125, cfg) if b.get('complete', True)]
        closes = [b['close'] for b in bars]
        fast, slow = calc_ema(closes, 12), calc_ema(closes, 34)
        spread = (fast/slow-1)*100
        regimes[symbol] = {'symbol': symbol, 'trend_bias': max(-1,min(1,spread/2.5)), 'risk_scale': 1,
            'trust_weight': 0, 'provider': 'deterministic_advisory', 'advisory_only': True, 'llm_cost_usd': 0,
            'thesis': '仅供观察，不参与组合仓位', 'updated_at': int(time.time()),
            'snapshot': {'source': cfg['market_data_source'], 'price': closes[-1], 'rsi14': calc_rsi(closes,14),
                'ema12_vs_ema34_pct': spread, 'return_20bars_pct': (closes[-1]/closes[-21]-1)*100}}
    write_json_atomic(SIGNAL_REGIME_FILE, regimes)
    return {'regimes': regimes, 'provider': 'deterministic_advisory', 'llm_cost_usd': 0}


def run_live_trading_cycle(symbols=None):
    cfg = load_config()
    if cfg.get('execution_provider') != 'local_paper':
        return _run_alpaca_cycle(cfg, symbols)
    return _run_local_paper_cycle(symbols)


def _run_local_paper_cycle(symbols=None):
    """One daily portfolio decision, executed through the same fixed shared-cash engine."""
    cfg = load_config()
    if symbols is not None and list(symbols) != list(cfg['watchlist']):
        raise ValueError('Partial portfolio execution is forbidden')
    broker = LocalPaperBroker(cfg)
    book = broker._ensure_ledger()
    now = int(time.time())
    today = session_date(now)
    if book.get('last_rebalance_session') == today:
        return {'status': 'already_rebalanced', 'strategy_name': book.get('strategy_name'), 'executions': []}
    cycle = {'cycle_id': uuid4().hex, 'timestamp': now, 'evaluation_policy': POLICY_VERSION,
        'investment_scope': UNIVERSE_VERSION, 'strategy_name': 'pending', 'ratchet_mode': 'portfolio_rotation',
        'runtime_llm_tokens': 0, 'executions': [], 'status': 'completed'}
    try:
        rows = {s: get_recent_bars(s, 260, cfg) for s in list(cfg['watchlist'])+[cfg['benchmark_symbol']]}
        if not rows[cfg['benchmark_symbol']][-1].get('market_open', True):
            return {'status': 'market_closed', 'strategy_name': book.get('strategy_name'), 'executions': []}
        history = {s: [dict(b) for b in bars if b.get('complete', True)][-252:] for s,bars in rows.items()}
        days, history = align_bars(history, list(cfg['watchlist'])+[cfg['benchmark_symbol']])
        if len(days) < 120 or days[-1] >= today:
            raise ValueError('Insufficient past closed daily sessions')
        prices = {s: float(bars[-1].get('quote_price', bars[-1]['close'])) for s,bars in rows.items()}
        # Legacy holdings are liquidation-only, never eligible for selection.
        for symbol in book.get('positions', {}):
            if symbol not in prices:
                quote = get_recent_bars(symbol, 2, dict(cfg, market_interval='1h'))[-1]
                prices[symbol] = float(quote.get('quote_price', quote['close']))
        # Catch up cash dividends on held positions; do not repeatedly credit an ex-date.
        income = 0.0
        through = book.get('corporate_actions_through', book.get('last_rebalance_session', ''))
        for symbol, pos in book.get('positions', {}).items():
            for bar in rows.get(symbol, []):
                day = bar.get('session_date') or session_date(bar['timestamp'])
                if through < day <= today:
                    # Live holdings are actual shares, unlike split-adjusted research units.
                    ratio = float(bar.get('split_ratio', 1))
                    if ratio != 1:
                        pos['qty'] *= ratio
                        pos['avg_price'] /= ratio
                    income += pos['qty'] * float(bar.get('dividend', 0))
        book['cash'] += income
        book['corporate_actions_through'] = today
        quantities = {s:p['qty'] for s,p in book.get('positions', {}).items()}
        plan = run_strategy(PROJECT_ROOT/'strategy.py', {'operation': 'decision', 'history': history,
            'positions': quantities, 'config': harness_config(cfg)})
        equity = mark_equity(book, prices)
        updated, trades = rebalance(book, plan['weights'], prices, cfg, now, plan['reasons'])
        updated.update(last_rebalance_session=today, strategy_name=plan['strategy_name'], signal_asof=days[-1])
        cycle.update(strategy_source_hash=plan.get('strategy_source_hash'), strategy_name=plan['strategy_name'], strategy_params=plan['strategy_params'], signal_asof=days[-1],
            rankings=plan['ranks'], selected=plan['selected'], cash_dividend_usd=income)
        orders = {t['symbol']:t for t in trades}
        for symbol in sorted(set(cfg['watchlist'])|set(quantities)):
            signal = plan['signals'].get(symbol, {})
            order = orders.get(symbol)
            cycle['executions'].append({'symbol':symbol, 'routed_broker':'local_paper', 'price':prices[symbol],
                'position_qty_before': quantities.get(symbol,0), 'current_weight':quantities.get(symbol,0)*prices[symbol]/equity,
                'target_weight': plan['weights'].get(symbol,0), 'effective_target_weight':plan['weights'].get(symbol,0),
                'score':signal.get('score'), 'rank':plan['ranks'].get(symbol),
                'reason':plan['reasons'].get(symbol,'退出研究范围'), 'indicators':signal.get('indicators',{}),
                'regime_status':'fixed_portfolio_policy',
                'market_data':{'source':cfg['market_data_source'], 'market_timestamp':rows.get(symbol,[{}])[-1].get('market_timestamp',now), 'interval':'1d', 'signal_asof':days[-1]},
                'order_result':{'status':'filled','order':order} if order else {'status':'skipped_small_delta'}})
        broker._save_ledger(updated)
    except Exception as exc:
        cycle.update(status='failed', error=str(exc))
        record_cycle(cycle)
        raise
    record_cycle(cycle)
    return {**cycle, 'portfolio':MultiBrokerRouter(cfg).get_unified_portfolio()}


def preview_portfolio(market=None):
    """Read-only planning snapshot for the dashboard; never commits orders or cash."""
    cfg = load_config()
    market = market if market is not None else build_market_snapshot(dict(cfg, market_data_source=cfg.get('execution_market_data_source','yahoo'), research_bars=260))
    if cfg.get('execution_provider') != 'local_paper':
        account = MultiBrokerRouter(cfg).broker.get_account_summary()
        book = {'positions':{p['symbol']:{'qty':p['qty']} for p in account['positions']}}
    else:
        book_path = STATE_DIR/'paper_account.json'
        book = json.loads(book_path.read_text()) if book_path.exists() else {'positions':{}}
    history = {s:[dict(b) for b in bars[-252:]] for s,bars in market.items()}
    days,history = align_bars(history,list(cfg['watchlist'])+[cfg['benchmark_symbol']])
    plan = run_strategy(PROJECT_ROOT/'strategy.py',{'operation':'decision','history':history,
        'positions':{s:p['qty'] for s,p in book.get('positions',{}).items()},'config':harness_config(cfg)})
    preview={'timestamp':int(time.time()),'status':'preview_only','evaluation_policy':POLICY_VERSION,
        'strategy_source_hash':plan.get('strategy_source_hash'), 'strategy_name':plan['strategy_name'],'signal_asof':days[-1],'selected':plan['selected'],
        'executions':[{'symbol':s,'price':history[s][-1]['close'],'score':plan['signals'][s]['score'],
                      'rank':plan['ranks'].get(s),'effective_target_weight':plan['weights'][s],
                      'reason':'预览，未下单：'+plan['reasons'][s],'indicators':plan['signals'][s].get('indicators',{})} for s in cfg['watchlist']]}
    write_json_atomic(STATE_DIR/'portfolio_preview.json',preview)
    return preview


def _run_alpaca_cycle(cfg, symbols=None):
    if not cfg.get('paper_observation_enabled'):
        return _run_alpaca_unmanaged(cfg, symbols)
    from atrader.alpaca import PaperClient
    from atrader.observation import tick
    with (STATE_DIR/'observation.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'observation_busy','executions':[]}
        client=PaperClient(cfg)
        result=tick(PROJECT_ROOT,STATE_DIR,cfg,client,client.request('/v2/clock'))
        if not result['allow_trade']:
            return {'status':result['status'],'executions':[]}
        return _run_alpaca_unmanaged(dict(cfg,external_cash_reserve_usd=result['state'].get('external_cash_reserve_usd',0)), symbols)


def _run_alpaca_unmanaged(cfg, symbols=None):
    from atrader.alpaca import PaperClient, timestamp
    from atrader.alpaca_execution import execute_daily
    if symbols is not None and list(symbols)!=list(cfg['watchlist']):
        raise ValueError('Partial portfolio execution is forbidden')
    client=PaperClient(cfg)
    clock=client.request('/v2/clock')
    if not clock['is_open']:
        return {'status':'market_closed','executions':[]}
    execution_cfg=dict(cfg,market_data_source='alpaca',market_interval='1d')
    rows={s:get_recent_bars(s,260,execution_cfg) for s in list(cfg['watchlist'])+[cfg['benchmark_symbol']]}
    history={s:[dict(b) for b in bars if b['complete']][-252:] for s,bars in rows.items()}
    days,history=align_bars(history,list(cfg['watchlist'])+[cfg['benchmark_symbol']])
    if len(days)<120 or days[-1]>=session_date(timestamp(clock['timestamp'])):
        raise ValueError('Insufficient past Alpaca daily sessions')
    positions=client.request('/v2/positions')
    plan=run_strategy(PROJECT_ROOT/'strategy.py',{'operation':'decision','history':history,
        'positions':{p['symbol']:float(p['qty']) for p in positions},'config':harness_config(cfg)})
    plan['signal_asof']=days[-1]
    plan['positions_before']={p['symbol']:float(p['qty']) for p in positions}
    prices={s:float(bars[-1]['quote_price']) for s,bars in rows.items()}
    if cfg.get('paper_observation_enabled'):
        plan['decision_snapshot']={'history':history,'positions':plan['positions_before'],
            'config':harness_config(cfg),'prices':prices,'observed_at':clock['timestamp']}
    result=execute_daily(cfg,clock,plan,prices)
    if result['status'] in ('completed','orders_pending','failed'):
        frozen=result['plan']
        cycle={'cycle_id':'alpaca-'+result['account']+'-'+result['session'],
            'timestamp':result['created_at'],'status':result['status'],'broker':'alpaca',
            'evaluation_policy':POLICY_VERSION,'investment_scope':UNIVERSE_VERSION,
            'strategy_name':frozen['strategy_name'],'strategy_source_hash':frozen.get('strategy_source_hash'),
            'ratchet_mode':'portfolio_rotation_alpaca','signal_asof':frozen['signal_asof'],
            'rankings':frozen['ranks'],'selected':frozen['selected'],'runtime_llm_tokens':0,'executions':[],
            'broker_orders':result['executions']+result.get('pending_executions',[])}
        actual=result['executions']+result.get('pending_executions',[])
        for symbol in cfg['watchlist']:
            signal=frozen['signals'][symbol]
            execution=next((e for e in reversed(actual) if e['symbol']==symbol),None)
            cycle['executions'].append({'symbol':symbol,'routed_broker':'alpaca','price':result['prices'][symbol],
                'position_qty_before':frozen['positions_before'].get(symbol,0),
                'current_weight':frozen['positions_before'].get(symbol,0)*result['prices'][symbol]/result['equity_before'],
                'score':signal['score'],'rank':frozen['ranks'].get(symbol),'target_weight':frozen['weights'][symbol],
                'effective_target_weight':frozen['effective_weights'][symbol],'reason':frozen['reasons'][symbol],
                'indicators':signal['indicators'],'regime_status':'fixed_portfolio_policy',
                'market_data':{'source':'alpaca_iex','signal_asof':frozen['signal_asof']},
                'order_result':execution['order_result'] if execution else {'status':'skipped_small_delta'}})
        record_cycle(cycle)
        if result['status']=='completed':
            cycle['portfolio']=MultiBrokerRouter(cfg).get_unified_portfolio()
        return cycle
    return {key:value for key,value in result.items() if key not in ('account','plan','prices')}

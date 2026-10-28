"""Bounded, isolated research paths and a small behavior-diverse archive.

All thresholds are fixed when a campaign starts. Novelty never changes promotion.
"""
import ast
import hashlib
import json
import math
import time
from pathlib import Path
from atrader.config import write_json_atomic
from atrader.evaluator import dataset_identity, harness_config
from atrader.portfolio import align_bars, POLICY_VERSION, session_date

SEARCH_POLICY = 'isolated_search_v1'
PATHS = ('independent', 'incumbent', 'archive')
# One proposal per scheduled call; this does not triple the API budget.
LIMITS = {'max_trials': 120, 'max_cost_usd': 1.0, 'archive_size': 12,
          'min_distance': .04, 'quality_tolerance': .15}


def structural_hash(source):
    """Exact AST duplicate detection; not a claim of semantic equivalence."""
    return hashlib.sha256(ast.dump(ast.parse(source), include_attributes=False).encode()).hexdigest()


def campaign_state(state_dir, cfg, market_loader):
    folder = state_dir / 'research'
    path = folder / 'campaign.json'
    config_hash = hashlib.sha256(json.dumps({**harness_config(cfg),
        'min_score_improvement': cfg['min_score_improvement'], 'market_data_source': cfg['market_data_source']}, sort_keys=True).encode()).hexdigest()
    if path.exists():
        campaign = json.loads(path.read_text())
        if campaign['config_hash'] != config_hash or campaign['evaluation_policy'] != POLICY_VERSION:
            raise ValueError('Research campaign settings changed; explicit reviewed migration required')
        return campaign, path
    market = market_loader(cfg)
    days, series = align_bars(market, list(cfg['watchlist']) + [cfg['benchmark_symbol']])
    if len(days) < 800:
        raise ValueError('Research campaign requires at least 800 common sessions')
    cutoff=cfg.get("research_cutoff")
    reserve=sum(day>cutoff for day in days) if cutoff else len(days)//5
    if reserve<1 or len(days)-reserve<640:
        raise ValueError("Insufficient research or final holdout sessions")
    # The frozen final segment never enters normal research worker requests/prompts.
    private = folder / 'sealed_holdout.json'
    write_json_atomic(private, {'market': {s: bars[-reserve:] for s, bars in series.items()},
                               'dates': days[-reserve:], 'used_for_promotion': False})
    private.chmod(0o600)
    research = {s: bars[:-reserve] for s, bars in series.items()}
    write_json_atomic(folder / 'sample.json', {'market': research,
        'layout': {'common_sessions': len(days), 'reserved_sessions': reserve}})
    campaign = {'id': dataset_identity(market, cfg), 'search_policy': SEARCH_POLICY,
        'evaluation_policy': POLICY_VERSION, 'config_hash': config_hash,
        'created_at': int(time.time()), 'status': 'active', 'trials': [], 'cost_usd': 0,
        'limits': dict(LIMITS), 'research_start': days[0], 'research_end': days[-reserve-1],
        'reserved_sessions': reserve, 'archive': [],
        'sealed_hash': hashlib.sha256(private.read_bytes()).hexdigest(),
        'sample_hash': hashlib.sha256((folder / 'sample.json').read_bytes()).hexdigest()}
    write_json_atomic(path, campaign)
    return campaign, path


def load_sample(path):
    raw = (path.parent / 'sample.json').read_bytes()
    campaign = json.loads(path.read_text())
    if hashlib.sha256(raw).hexdigest() != campaign['sample_hash']:
        raise ValueError('Frozen research sample hash mismatch')
    sample = json.loads(raw)
    return sample['market'], sample['layout']


def stop_reason(campaign):
    if campaign.get('status') in ('migration_pending','aborted','superseded'):
        return campaign['status']
    if len(campaign['trials']) >= campaign['limits']['max_trials']:
        return 'trial_budget_exhausted'
    if campaign['cost_usd'] >= campaign['limits']['max_cost_usd']:
        return 'cost_budget_exhausted'
    return None


def evidence(evaluation):
    """Measured feedback only; never model-invented failure narratives."""
    return {key: evaluation.get(key) for key in
        ('ratchet_score', 'train_score', 'validation_score', 'avg_max_drawdown_pct',
         'avg_return_pct', 'total_trading_fees_usd')}


def observation_summary(market):
    result = {}
    for symbol, bars in market.items():
        closes = [bar['close'] for bar in bars]
        returns = [closes[i]/closes[i-1]-1 for i in range(1, len(closes))]
        mean = sum(returns)/len(returns)
        result[symbol] = {'sessions': len(bars), 'net_price_change_pct': round((closes[-1]/closes[0]-1)*100, 3),
            'daily_volatility_pct': round(math.sqrt(sum((r-mean)**2 for r in returns)/len(returns))*100, 3)}
    return result


def proposal_context(pathway, source, baseline, campaign, market, folder):
    common = 'Tradable symbols: ' + ', '.join(s for s in market if s != 'SPY')
    common += '\nObserve the available evidence, state a causal hypothesis, then implement it. No required strategy family.'
    parent = None
    if pathway == 'independent':
        body = common + '\nIndependent exploration: no incumbent, previous proposals or scores are supplied.\nObserved research-only data summary:\n'
        body += json.dumps(observation_summary(market), sort_keys=True)
    elif pathway == 'archive' and any(item['source_hash'] != structural_hash(source) for item in campaign['archive']):
        # Prefer a different parent; no incumbent code or other experiments are shown.
        parent = max((item for item in campaign['archive'] if item['source_hash'] != structural_hash(source)), key=lambda item: (behavior_distance(item['behavior'], baseline['portfolio']), -item['generation']))
        parent_source = (folder / parent['source']).read_text()
        if structural_hash(parent_source) != parent['source_hash']:
            raise ValueError('Archived strategy hash mismatch')
        body = common + '\nExplore this archived branch independently. Measured branch evidence:\n'
        body += json.dumps(parent['evidence']) + '\nBranch strategy:\n```python\n' + parent_source + '\n```'
    elif pathway == 'archive':
        # Empty archive must not silently turn into another champion-mutation path.
        return proposal_context('independent', source, baseline, campaign, market, folder)
    else:
        body = common + '\nImprove this incumbent. Measured evidence:\n' + json.dumps(evidence(baseline))
        body += '\nIncumbent strategy:\n```python\n' + source + '\n```'
    return body, parent['generation'] if parent else None


def behavior_distance(left, right):
    """Mean daily L1 target-weight distance over the identical validation dates."""
    a, b = left.get('weight_path', []), right.get('weight_path', [])
    if not a or len(a) != len(b) or left.get('behavior_dates') != right.get('behavior_dates'):
        return 0.0
    return sum(sum(abs(x-y) for x,y in zip(day_a, day_b)) for day_a,day_b in zip(a,b))/len(a)


def archive_candidate(campaign, folder, generation, source, evaluation):
    """Keep a bounded set of qualified behavioral niches, independent of execution."""
    archive = campaign['archive']
    candidate_score = evaluation['ratchet_score']
    best = max([candidate_score] + [item['score'] for item in archive])
    archive[:] = [item for item in archive if item['score'] >= best-campaign['limits']['quality_tolerance']]
    if candidate_score < best-campaign['limits']['quality_tolerance']:
        return 'below_quality_floor', None
    behavior = {key: evaluation['portfolio'].get(key, []) for key in ('weight_path', 'behavior_dates')}
    near = [(behavior_distance(item['behavior'], behavior), item) for item in archive]
    distance, neighbor = min(near, key=lambda pair: pair[0]) if near else (None, None)
    if neighbor and distance < campaign['limits']['min_distance']:
        if candidate_score <= neighbor['score']:
            return 'similar_behavior', distance
        archive.remove(neighbor)
    elif len(archive) >= campaign['limits']['archive_size']:
        # Only replace a lower-quality member; novelty never excuses poor quality.
        worst = min(archive, key=lambda item: item['score'])
        if candidate_score <= worst['score']:
            return 'archive_full', distance
        archive.remove(worst)
    name = f'archive/strategy_{generation}_{structural_hash(source)[:12]}.py'
    target = folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    archive.append({'generation': generation, 'source': name, 'score': candidate_score,
        'source_hash': structural_hash(source), 'behavior': behavior, 'evidence': evidence(evaluation)})
    return 'accepted', distance


def public_campaign(campaign):
    """Read-only dashboard metadata; no sealed data or large behavior vectors."""
    return {**{key: campaign.get(key) for key in ('id', 'status', 'search_policy', 'limits', 'cost_usd',
        'research_start', 'research_end', 'reserved_sessions', 'legacy_trials', 'lineage', 'data_source', 'holdout_independence')},
        'trial_count': len(campaign.get('trials', [])),
        'path_counts': {path: sum(t['pathway'] == path for t in campaign.get('trials', [])) for path in PATHS+('reevaluation',)},
        'archive': [{key: item[key] for key in ('generation', 'score', 'evidence')} for item in campaign.get('archive', [])]}


def audit_campaign(state_dir, cfg, strategy_path):
    """Explicit one-shot final examination of a completed campaign; never promotion."""
    import fcntl
    from atrader.evaluator import evaluate_strategy
    with (state_dir / 'research.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        campaign, path = campaign_state(state_dir, cfg, lambda _: None)
        if not stop_reason(campaign):
            raise ValueError('Finish the fixed campaign budget before opening its final exam')
        if campaign.get('audit_attempt'):
            raise ValueError('Final exam already opened; repeated adaptive inspection is prohibited')
        sample, layout = load_sample(path)
        sealed_raw = (path.parent / 'sealed_holdout.json').read_bytes()
        if hashlib.sha256(sealed_raw).hexdigest() != campaign['sealed_hash']:
            raise ValueError('Sealed final segment hash mismatch')
        sealed = json.loads(sealed_raw)['market']
        target = path.parent / 'exam_strategy.py'
        target.write_text(Path(strategy_path).read_text())
        campaign['audit_attempt'] = {'timestamp': int(time.time()), 'source_hash': structural_hash(target.read_text()), 'status': 'started'}
        write_json_atomic(path, campaign)
        result = evaluate_strategy(target, market_bars={s: bars+sealed[s] for s,bars in sample.items()}, audit=True, audit_reserved_sessions=layout['reserved_sessions'])
        campaign['audit_attempt']['status'] = 'completed'
        write_json_atomic(path, campaign)
        return result


def recorded_daily_cost(state_dir, day):
    history_file = state_dir / 'ratchet_history.json'
    history = json.loads(history_file.read_text()) if history_file.exists() else {}
    records = history.get('generations', [])
    cost = sum(item.get('llm_cost_usd', 0) for item in records if session_date(item.get('timestamp', 0)) == day)
    completed = {item['generation'] for item in records}
    campaign_file = state_dir / 'research' / 'campaign.json'
    campaign = json.loads(campaign_file.read_text()) if campaign_file.exists() else {}
    cost += sum(item.get('cost_usd', 0) for item in campaign.get('trials', [])
                if item['generation'] not in completed and session_date(item['timestamp']) == day)
    return round(cost, 6)

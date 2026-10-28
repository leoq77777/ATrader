"""Explicit migration of ACTIVE strategies to a new frozen data source.

No model requests, broker mutations, or access to final exam scores. Caller must
stop the daemon and hold research.lock while preparing and committing this plan.
"""
import hashlib
import json
import shutil
import time
from copy import deepcopy
from pathlib import Path
from atrader.config import write_json_atomic
from atrader.evaluator import evaluate_strategy, harness_config
from atrader.research import (campaign_state, load_sample, structural_hash, archive_candidate, stop_reason)


def active_sources(root, campaign):
    sources={}
    def add(path,label,generation,old_score):
        code=path.read_text();key=structural_hash(code)
        if key in sources:
            sources[key]['aliases'].append(label)
            return
        sources[key]={'source':code,'aliases':[label],'origin_generation':generation,'old_score':old_score}
    incumbent=(root/'strategy.py').read_text();key=structural_hash(incumbent)
    origin=next((a['generation'] for a in campaign['archive'] if a['source_hash']==key),None)
    add(root/'strategy.py','incumbent',origin,None)
    for member in campaign['archive']:
        path=root/'state/research'/member['source']
        if structural_hash(path.read_text())!=member['source_hash']:
            raise ValueError('Active archive source hash mismatch')
        add(path,'archive:'+str(member['generation']),member['generation'],member['score'])
    return sources,key


def prepare(root,cfg,market):
    state=root/'state'
    old=json.loads((state/'research/campaign.json').read_text())
    history=json.loads((state/'ratchet_history.json').read_text())
    sources,incumbent_key=active_sources(root,old)
    if len(old['trials'])+len(sources)>old['limits']['max_trials']:
        raise ValueError('Reevaluation exceeds remaining campaign trial budget')
    # Separate stage: old samples, archive and published scores remain intact until all pass.
    folder=state/'research_rebase_pending'
    if folder.exists():raise ValueError('Pending migration exists; inspect it before retrying')
    folder.mkdir()
    (folder/'state').mkdir()
    new_cfg=dict(cfg,market_data_source='alpaca',research_cutoff=old['research_end'])
    campaign,path=campaign_state(folder/'state',new_cfg,lambda _:market)
    if campaign['research_end']>old['research_end']:
        raise ValueError('Migration would expose old final holdout dates')
    campaign.update(trials=deepcopy(old['trials']),cost_usd=old['cost_usd'],limits=deepcopy(old['limits']),
        legacy_trials=old.get('legacy_trials',0),data_source='alpaca',
        lineage={'previous_campaign':old['id'],'previous_data_source':cfg['market_data_source'],
            'previous_trial_count':len(old['trials']),'previous_cost_usd':old['cost_usd']},
        holdout_independence='Previously reserved economic dates; changing feed does not create independent data')
    if old.get('audit_attempt'):campaign['audit_attempt']=deepcopy(old['audit_attempt'])
    write_json_atomic(path,campaign)
    market,layout=load_sample(path)
    generation=max([history['current_generation']]+[t['generation'] for t in old['trials']])
    results=[]
    for source_hash,source in sources.items():
        generation+=1
        target=folder/f'strategy_{generation}.py';target.write_text(source['source'])
        item={**source,'source_hash':source_hash,'generation':generation,'old_data_source':cfg['market_data_source']}
        if 'incumbent' in item['aliases']:item['old_score']=history['best_score']
        trial={'generation':generation,'timestamp':int(time.time()),'pathway':'reevaluation',
            'structural_hash':source_hash,'status':'started','cost_usd':0}
        campaign['trials'].append(trial);write_json_atomic(path,campaign)
        try:
            item['evaluation']=evaluate_strategy(target,market_bars=market,research_layout=layout,cfg_override=new_cfg,persist=False)
            item.update(status='valid',new_score=item['evaluation']['ratchet_score'],error=None)
        except Exception as exc:
            item.update(status='failed',new_score=None,error=str(exc))
        results.append(item)
        trial.update(status=item['status'],error=item['error']);write_json_atomic(path,campaign)
    incumbent=next(item for item in results if item['source_hash']==incumbent_key)
    if incumbent['status']!='valid':raise ValueError('Incumbent failed Alpaca evaluation: '+incumbent['error'])
    winner=max((item for item in results if item['status']=='valid'),key=lambda item:item['new_score'])
    if winner['new_score']<incumbent['new_score']+cfg['min_score_improvement']:
        winner=incumbent
    for item in sorted((x for x in results if x['status']=='valid'),key=lambda x:x['new_score'],reverse=True):
        item['archive_status'],item['behavior_distance']=archive_candidate(campaign,path.parent,item['generation'],item['source'],item['evaluation'])
    campaign['forward_epochs']=[{'generation':winner['generation'],'started_at':int(time.time()),
        'source_hash':hashlib.sha256(winner['source'].encode()).hexdigest(),'status':'awaiting_forward_observations'}]
    campaign['status']=stop_reason(campaign) or 'active'
    campaign['lineage']['previous_forward_epochs']=old.get('forward_epochs',[])
    write_json_atomic(path,campaign)
    manifest={'created_at':int(time.time()),'previous_campaign':old['id'],'new_campaign':campaign['id'],
        'public_config':harness_config(new_cfg),'data_source':'alpaca','data_policy':'split_adjusted_cash_dividends','research_end':campaign['research_end'],
        'reserved_sessions':campaign['reserved_sessions'],'unique_active_strategies':len(results),
        'new_trial_count':len(campaign['trials']),'inherited_trial_count':len(old['trials']),
        'winner_generation':winner['generation'],'strategy_changed':winner['source_hash']!=incumbent_key,
        'previous_history_sha256':hashlib.sha256((state/'ratchet_history.json').read_bytes()).hexdigest(),
        'previous_strategy_sha256':hashlib.sha256((root/'strategy.py').read_bytes()).hexdigest(),
        'results':results}
    write_json_atomic(folder/'manifest.json',manifest)
    return folder,manifest


def commit(root,folder):
    state=root/'state';manifest=json.loads((folder/'manifest.json').read_text())
    for path,key in ((state/'ratchet_history.json','previous_history_sha256'),(root/'strategy.py','previous_strategy_sha256')):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest[key]:raise ValueError('Research changed during rebaseline')
    history=json.loads((state/'ratchet_history.json').read_text())
    campaign=json.loads((folder/'state/research/campaign.json').read_text())
    retired=state/'archives'/('research_'+manifest['previous_campaign'][:12]+'_'+str(int(time.time())))
    retired.mkdir(parents=True)
    # Caller backs up full state and keeps the daemon stopped until commit completes.
    shutil.copytree(state/'research',retired/'research')
    old=json.loads((retired/'research/campaign.json').read_text());old.update(status='superseded',superseded_by=manifest['new_campaign'])
    write_json_atomic(retired/'research/campaign.json',old)
    shutil.copy2(state/'last_evaluation.json',retired/'last_evaluation.json')
    winner=next(item for item in manifest['results'] if item['generation']==manifest['winner_generation'])
    report_dir=state/'revaluations'/manifest['new_campaign']
    report_dir.mkdir(parents=True)
    for item in manifest['results']:
        target=report_dir/f'strategy_{item["generation"]}.py';target.write_text(item['source'])
        evaluation=item.get('evaluation') or {}
        write_json_atomic(target.with_suffix('.json'),{k:v for k,v in item.items() if k!='source'})
        record={'generation':item['generation'],'timestamp':int(time.time()),'status':'BASELINE (ALPACA REEVALUATION)' if item is winner else 'RESCORED (ARCHIVE)',
            'strategy_name':evaluation.get('strategy_name'),'hypothesis':'Active strategy reevaluated on frozen Alpaca IEX bars and cash dividends',
            'error':item['error'],'ratchet_score':item['new_score'],'baseline_score_before':item['old_score'],'score_delta':None,
            'train_score':evaluation.get('train_score'),'validation_score':evaluation.get('validation_score'),
            'avg_sharpe':evaluation.get('avg_sharpe'),'avg_return_pct':evaluation.get('avg_return_pct'),
            'avg_max_drawdown_pct':evaluation.get('avg_max_drawdown_pct'),'llm_cost_usd':0,'provider':'fixed_harness',
            'prompt_tokens':0,'completion_tokens':0,'research_pathway':'reevaluation','campaign_id':campaign['id'],
            'campaign_trial':next(i+1 for i,t in enumerate(campaign['trials']) if t['generation']==item['generation']),
            'archive_status':item.get('archive_status','failed'),'behavior_distance':item.get('behavior_distance'),
            'candidate_source_hash':hashlib.sha256(item['source'].encode()).hexdigest(),
            'candidate_artifact':str(target.relative_to(state)),'evaluation_policy':campaign['evaluation_policy'],
            'market_data_source':'alpaca','dataset_id':evaluation.get('dataset_id')}
        history['generations'].append(record)
    history.update(current_generation=max(item['generation'] for item in manifest['results']),
        best_score=winner['new_score'],best_strategy_name=winner['evaluation']['strategy_name'])
    shutil.rmtree(state/'research')
    shutil.copytree(folder/'state/research',state/'research')
    if manifest['strategy_changed']:
        stage=root/'strategy.next.py';stage.write_text(winner['source']+'\n');stage.replace(root/'strategy.py')
    sample=json.loads((state/'research/sample.json').read_text())
    write_json_atomic(state/'datasets'/(winner['evaluation']['dataset_id']+'.json'),
        {**sample,'config':manifest['public_config'],'market_data_source':'alpaca','policy':campaign['evaluation_policy']})
    write_json_atomic(state/'last_evaluation.json',winner['evaluation'])
    write_json_atomic(state/'ratchet_history.json',history)
    # Retired local-paper run is never resumed or mixed into broker returns.
    migrations=state/'alpaca_migration.json'
    local_archive=json.loads(migrations.read_text()).get('local_archive') if migrations.exists() else None
    if (state/'paper_account.json').exists():
        legacy=Path(local_archive) if local_archive else state/'archives'/('local_paper_aborted_'+str(int(time.time())))
        legacy.mkdir(parents=True,exist_ok=True)
        shutil.move(str(state/'paper_account.json'),str(legacy/('remaining_ledger_'+str(int(time.time()))+'.json')))
        local_archive=str(legacy)
    retired_run={'provider':'ATrader Local Paper','status':'ABORTED','ended_at':int(time.time()),
        'reason':'User terminated old local simulation after migrating active strategies to Alpaca data','archive':local_archive}
    if local_archive:write_json_atomic(Path(local_archive)/'run_status.json',retired_run)
    write_json_atomic(state/'retired_paper_run.json',retired_run)
    summary={k:v for k,v in manifest.items() if k not in ('results','previous_history_sha256','previous_strategy_sha256')}
    summary.update(status='completed',previous_research_archive=str(retired),retired_paper_status='ABORTED',
        results=[{k:v for k,v in item.items() if k not in ('source','evaluation')} | {
            'name':item.get('evaluation',{}).get('strategy_name'),
            'sharpe':item.get('evaluation',{}).get('avg_sharpe'),
            'return_pct':item.get('evaluation',{}).get('avg_return_pct'),
            'drawdown_pct':item.get('evaluation',{}).get('avg_max_drawdown_pct')} for item in manifest['results']])
    write_json_atomic(state/'research_revaluation.json',summary)
    shutil.move(str(folder/'manifest.json'),str(report_dir/'manifest.json'))
    shutil.rmtree(folder)
    return summary

"""Fixed shared-portfolio evaluation harness; candidates cannot edit scoring or fills."""
import hashlib
import json
import math
from pathlib import Path
from typing import Dict, Any, List, Optional
from atrader.config import PROJECT_ROOT, STATE_DIR, load_config, write_json_atomic
from atrader.market_data import get_recent_bars
from atrader.portfolio import POLICY_VERSION, align_bars
from atrader.strategy_runner import run_strategy
from atrader.universe import UNIVERSE_VERSION

def generate_multi_regime_bars(symbol: str = "BTC/USDT", n_bars: int = 480) -> List[Dict[str, Any]]:
    """
    Generates deterministic, realistic multi-regime OHLCV market data
    (Bull Trend -> High-Vol Consolidation -> Drawdown Shock -> Recovery Breakout)
    when offline or when initializing the baseline ratchet benchmark.
    """
    seed = sum(ord(c) * (idx + 1) for idx, c in enumerate(symbol))
    price = 60000.0 if "BTC" in symbol else (3000.0 if "ETH" in symbol else (125.0 if "NVDA" in symbol else 520.0))
    bars: List[Dict[str, Any]] = []
    base_ts = 1720000000

    for i in range(n_bars):
        # Deterministic pseudo-random cycle
        phase = i / float(n_bars)
        if phase < 0.30:
            drift = 0.0016  # Regime 1: Steady uptrend
            vol = 0.011
        elif phase < 0.55:
            drift = -0.0002  # Regime 2: Range-bound mean reversion
            vol = 0.015
        elif phase < 0.72:
            drift = -0.0024  # Regime 3: Sharp correction / stress test
            vol = 0.022
        else:
            drift = 0.0021  # Regime 4: Strong breakout recovery
            vol = 0.014

        wave1 = math.sin(i * 0.27 + seed * 0.01) * 0.55
        wave2 = math.cos(i * 0.11 - seed * 0.02) * 0.35
        wave3 = math.sin(i * 0.73 + seed * 0.05) * 0.25
        shock = drift + vol * (wave1 + wave2 + wave3)

        open_p = price
        close_p = max(price * 0.1, open_p * (1.0 + shock))
        wick_up = abs(math.sin(i * 0.41 + seed)) * vol * 0.85
        wick_dn = abs(math.cos(i * 0.53 + seed)) * vol * 0.85
        high_p = max(open_p, close_p) * (1.0 + wick_up)
        low_p = min(open_p, close_p) * (1.0 - wick_dn)
        volume = round(1000.0 * (1.0 + abs(shock) * 45.0), 2)

        bars.append({
            "timestamp": base_ts + i * 14400,  # 4-hour bars
            "open": round(open_p, 4),
            "high": round(high_p, 4),
            "low": round(low_p, 4),
            "close": round(close_p, 4),
            "volume": volume,
        })
        price = close_p

    return bars


def harness_config(cfg):
    """Only public experiment settings cross the worker boundary; never credentials."""
    return {key: cfg[key] for key in ('watchlist', 'benchmark_symbol', 'max_holdings', 'rank_buffer',
        'max_position_pct', 'max_gross_exposure', 'initial_paper_cash', 'trading_fee_bps', 'slippage_bps')}


def build_market_snapshot(cfg):
    result = {}
    for symbol in list(cfg['watchlist']) + [cfg['benchmark_symbol']]:
        rows = get_recent_bars(symbol, int(cfg.get('research_bars', 1000))+5, cfg)
        complete = [b for b in rows if b.get('complete', True)]
        if len(complete) < int(cfg.get('research_bars', 1000)):
            raise ValueError('Insufficient closed daily bars for ' + symbol)
        result[symbol] = [{key: b.get(key, 0) for key in ('timestamp', 'open', 'high', 'low', 'close', 'volume', 'dividend')} for b in complete[-int(cfg.get('research_bars', 1000)):]]
    return result


def dataset_identity(market, cfg):
    payload = {'market': market, 'config': harness_config(cfg), 'policy': POLICY_VERSION, 'scope': UNIVERSE_VERSION, 'market_data_source': cfg.get('market_data_source')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evaluate_strategy(strategy_path=None, symbols=None, llm_cost_usd=0.0, market_bars=None, audit=False, research_layout=None, cfg_override=None, persist=True, audit_reserved_sessions=None):
    cfg = cfg_override or load_config()
    if symbols is not None and list(symbols) != list(cfg['watchlist']):
        raise ValueError('Evaluation universe is fixed; rebaseline after a reviewed scope change')
    if market_bars is None and not audit and (STATE_DIR/'research/campaign.json').exists():
        from atrader.research import campaign_state,load_sample
        _,path=campaign_state(STATE_DIR,cfg,lambda _:None)
        market_bars,research_layout=load_sample(path)
    market = market_bars if market_bars is not None else build_market_snapshot(cfg)
    identity = dataset_identity(market, cfg)
    layout = research_layout
    if not audit and layout is None:
        days, series = align_bars(market, list(cfg['watchlist']) + [cfg['benchmark_symbol']])
        reserve = len(days)//5
        layout = {'common_sessions': len(days), 'reserved_sessions': reserve}
        market = {s: bars[:-reserve] for s, bars in series.items()}
    if audit and layout:
        raise ValueError('Research-only data cannot be used for final audit')
    if audit and audit_reserved_sessions is not None:
        days,_ = align_bars(market, list(cfg['watchlist']) + [cfg['benchmark_symbol']])
        layout={'common_sessions':len(days),'reserved_sessions':audit_reserved_sessions,'audit':True}
    dataset = STATE_DIR / 'datasets' / (identity + '.json')
    if persist and not audit and not dataset.exists():
        write_json_atomic(dataset, {'market': market, 'config': harness_config(cfg), 'policy': POLICY_VERSION, 'layout': layout})
    target = Path(strategy_path) if strategy_path else PROJECT_ROOT / 'strategy.py'
    result = run_strategy(target, {'operation': 'evaluate', 'market': market, 'config': harness_config(cfg), 'audit': audit, 'layout': layout})
    result.update(data_policy='split_adjusted_cash_dividends', dataset_id=identity, evaluation_policy=POLICY_VERSION, market_data_source=cfg['market_data_source'], market_interval='1d', investment_scope=UNIVERSE_VERSION)
    if audit:
        if persist:write_json_atomic(STATE_DIR / 'last_holdout_audit.json', result)
        return result
    portfolio = result['portfolio']
    result['ratchet_score'] = round(result['ratchet_score'] - llm_cost_usd / max(cfg['initial_paper_cash']*.01, 1)*.5, 6)
    result.update(avg_sharpe=portfolio['sharpe'], avg_return_pct=portfolio['net_return_pct'],
        avg_max_drawdown_pct=portfolio['max_drawdown_pct'], net_profit_usd=portfolio['net_profit_usd'],
        net_profit_after_llm_usd=portfolio['net_profit_usd']-llm_cost_usd, llm_cost_usd=llm_cost_usd,
        total_trading_fees_usd=portfolio['total_fees_usd'], equity_curve=portfolio['equity_curve'],
        profit_to_model_cost_ratio=portfolio['net_profit_usd']/llm_cost_usd if llm_cost_usd>0 else None,
        data_range={'start': portfolio['start_date'], 'end': portfolio['end_date']}, symbols={})
    if persist:write_json_atomic(STATE_DIR / 'last_evaluation.json', result)
    return result

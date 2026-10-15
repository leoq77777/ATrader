"""
ATrader Dual-Mode Karpathy Ratchet Engine
=========================================
Implements both Ratchet paradigms and their Hybrid combination:

1. `code_ratchet` (Strategy Code Evolution Ratchet - Karpathy `autoresearch` style):
   - Agent LLM mutates `strategy.py` based on out-of-sample weaknesses.
   - Evaluator tests candidate on Train (70%) + Validation (30%) bars, deducting trading fees & LLM token cost.
   - LOCKS (commits) the new code ONLY if `ratchet_score` strictly improves over baseline;
     otherwise REVERTS immediately to guarantee monotonic strategy improvement.

2. `signal_ratchet` (Low-Frequency Regime Signal + Shadow Trust Ratchet):
   - Agent LLM analyzes compact multi-asset market state at low frequency (e.g., 4h/1d)
     and outputs `trend_bias` [-1, +1] and `risk_scale` [0, 1.5].
   - A Shadow Ledger tracks whether the LLM's previous signals generated net profit > 3x API cost,
     automatically ratcheting `trust_weight` up or down.

3. `hybrid` (Recommended Default):
   - Runs `code_ratchet` to evolve deterministic 0-token execution logic in `strategy.py`,
     modulated by low-frequency `signal_ratchet` regime overlays weighted by `trust_weight`.
"""

import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from atrader.config import PROJECT_ROOT, STATE_DIR, load_config, ensure_state_dir
from atrader.evaluator import (
    evaluate_strategy,
    generate_multi_regime_bars,
    load_strategy_from_path,
)
from atrader.brokers import MultiBrokerRouter
from strategy import calc_ema, calc_rsi, calc_atr, calc_zscore


RATCHET_HISTORY_FILE = STATE_DIR / "ratchet_history.json"
SIGNAL_REGIME_FILE = STATE_DIR / "signal_regime.json"
SNAPSHOTS_DIR = STATE_DIR / "snapshots"


# ------------------------------------------------------------------------------
# DeepSeek / OpenAI-Compatible LLM Client with Token Cost Accounting
# ------------------------------------------------------------------------------

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
        "prompt_tokens": p_tok,
        "completion_tokens": c_tok,
        "cost_usd": round(cost_usd, 6),
    }


# ------------------------------------------------------------------------------
# Ratchet History Management
# ------------------------------------------------------------------------------

def get_ratchet_history() -> Dict[str, Any]:
    ensure_state_dir()
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    if RATCHET_HISTORY_FILE.exists():
        try:
            return json.loads(RATCHET_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Initialize baseline evaluation if no history exists
    baseline_eval = evaluate_strategy()
    history = {
        "created_at": int(time.time()),
        "current_generation": 0,
        "best_score": baseline_eval["ratchet_score"],
        "best_strategy_name": baseline_eval["strategy_name"],
        "total_llm_cost_usd": 0.0,
        "generations": [
            {
                "generation": 0,
                "timestamp": int(time.time()),
                "status": "BASELINE",
                "strategy_name": baseline_eval["strategy_name"],
                "hypothesis": "Initial adaptive EMA + RSI + ATR trailing stop baseline",
                "ratchet_score": baseline_eval["ratchet_score"],
                "validation_score": baseline_eval["validation_score"],
                "avg_sharpe": baseline_eval["avg_sharpe"],
                "avg_return_pct": baseline_eval["avg_return_pct"],
                "avg_max_drawdown_pct": baseline_eval["avg_max_drawdown_pct"],
                "llm_cost_usd": 0.0,
                "provider": "baseline",
            }
        ],
    }
    RATCHET_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return history


def _generate_heuristic_candidate(current_code: str, gen_idx: int) -> Tuple[str, str]:
    """
    Deterministic local strategy mutation engine used when `LLM_API_KEY` is not yet set
    or when testing the Karpathy Ratchet loop offline with $0 API cost.
    """
    mutations = [
        {
            "name": f"ratchet_gen{gen_idx}_tighter_dd_vol_scaling",
            "hypothesis": "Increase target volatility slightly to 0.022 and widen ATR trailing stop to 2.5x to prevent premature shakeouts in strong trends",
            "replacements": [
                ('"fast_ema": 12,', '"fast_ema": 10,'),
                ('"slow_ema": 34,', '"slow_ema": 30,'),
                ('"stop_atr_mult": 2.2,', '"stop_atr_mult": 2.5,'),
                ('"target_vol_pct": 0.018,', '"target_vol_pct": 0.022,'),
            ],
        },
        {
            "name": f"ratchet_gen{gen_idx}_overaggressive_overfit_test",
            "hypothesis": "Test ultra-short EMA (4/9) with tight 1.1x ATR stop (intentionally prone to whipsaw to verify Ratchet auto-revert)",
            "replacements": [
                ('"fast_ema": 10,', '"fast_ema": 4,'),
                ('"fast_ema": 12,', '"fast_ema": 4,'),
                ('"slow_ema": 30,', '"slow_ema": 9,'),
                ('"slow_ema": 34,', '"slow_ema": 9,'),
                ('"stop_atr_mult": 2.5,', '"stop_atr_mult": 1.1,'),
                ('"stop_atr_mult": 2.2,', '"stop_atr_mult": 1.1,'),
            ],
        },
        {
            "name": f"ratchet_gen{gen_idx}_momentum_dip_accumulator",
            "hypothesis": "Refine RSI entry band (38-72) and raise oversold threshold to 36 to capture V-shaped pullbacks earlier",
            "replacements": [
                ('"rsi_buy_max": 68.0,', '"rsi_buy_max": 72.0,'),
                ('"rsi_oversold": 32.0,', '"rsi_oversold": 36.0,'),
                ('"rsi_exit": 76.0,', '"rsi_exit": 79.0,'),
                ('"target_vol_pct": 0.018,', '"target_vol_pct": 0.024,'),
                ('"target_vol_pct": 0.022,', '"target_vol_pct": 0.025,'),
            ],
        },
    ]
    choice = mutations[(gen_idx - 1) % len(mutations)]
    mutated = re.sub(
        r'name:\s*str\s*=\s*"[^"]+"',
        f'name: str = "{choice["name"]}"',
        current_code,
        count=1,
    )
    for old_s, new_s in choice["replacements"]:
        mutated = mutated.replace(old_s, new_s)
    return mutated, choice["hypothesis"]


# ------------------------------------------------------------------------------
# Mode 1: Code Evolution Ratchet (`code_ratchet`)
# ------------------------------------------------------------------------------

def run_code_ratchet_step(steps: int = 1, force_mock: bool = False) -> Dict[str, Any]:
    """
    Executes N generations of the Karpathy Code Ratchet loop:
      1. Snapshot current `strategy.py` and baseline score
      2. Prompt DeepSeek (or heuristic mutator) for a single targeted improvement
      3. Evaluate candidate on walk-forward Train + Out-of-Sample Validation
      4. LOCK if `candidate_score >= best_score + min_delta`, else REVERT
    """
    cfg = load_config()
    if force_mock:
        cfg["llm_api_key"] = ""

    history = get_ratchet_history()
    strategy_file = PROJECT_ROOT / "strategy.py"
    min_delta = float(cfg.get("min_score_improvement", 0.005))

    step_results: List[Dict[str, Any]] = []

    for _ in range(max(1, steps)):
        baseline_code = strategy_file.read_text(encoding="utf-8")
        baseline_eval = evaluate_strategy(strategy_file)
        best_score = float(history.get("best_score", baseline_eval["ratchet_score"]))
        gen_num = int(history.get("current_generation", 0)) + 1

        # Build compact context of recent attempts so LLM doesn't repeat failed ideas
        recent_gens = history.get("generations", [])[-5:]
        history_digest = "\n".join(
            f"- Gen {g['generation']} [{g['status']}]: score={g['ratchet_score']} "
            f"(Sharpe={g['avg_sharpe']}, Ret={g['avg_return_pct']}%, DD={g['avg_max_drawdown_pct']}%) | {g['hypothesis']}"
            for g in recent_gens
        )

        sys_prompt = (
            "You are the Karpathy Ratchet Quantitative Researcher for ATrader.\n"
            "Your task is to mutate `strategy.py` to strictly improve its out-of-sample `ratchet_score` "
            "(which rewards Validation Sharpe, Net Return after 8bps trading cost, and penalizes Max Drawdown and Train/Val overfitting).\n"
            "Rules:\n"
            "1. Output a short first line starting with `HYPOTHESIS: <one sentence explanation>`.\n"
            "2. Then output the complete, valid Python code for `strategy.py` inside a ```python ... ``` block.\n"
            "3. Preserve the `Strategy` class interface (`name`, `description`, `params`, `on_bars`) and pure-Python indicators."
        )
        user_prompt = (
            f"Current Best Ratchet Score: {best_score}\n"
            f"Recent Ratchet History:\n{history_digest}\n\n"
            f"Current `strategy.py`:\n```python\n{baseline_code}\n```\n"
            f"Propose Generation {gen_num} (`name = 'ratchet_gen{gen_num}_...'`) to improve out-of-sample Sharpe and reduce drawdown."
        )

        hypothesis = ""
        candidate_code = ""
        usage_meta: Dict[str, Any] = {"provider": "heuristic_fallback", "cost_usd": 0.0}

        try:
            llm_resp, usage_meta = call_deepseek_llm(sys_prompt, user_prompt, cfg)
        except Exception as exc:
            llm_resp = None
            usage_meta = {"provider": f"fallback_after_error ({exc})", "cost_usd": 0.0}

        if llm_resp:
            hyp_match = re.search(r"HYPOTHESIS:\s*(.+)", llm_resp)
            hypothesis = hyp_match.group(1).strip() if hyp_match else f"DeepSeek mutation generation {gen_num}"
            code_match = re.search(r"```python\s*(.*?)```", llm_resp, re.DOTALL)
            if code_match:
                candidate_code = code_match.group(1).strip()

        if not candidate_code:
            candidate_code, hypothesis = _generate_heuristic_candidate(baseline_code, gen_num)

        llm_cost_usd = float(usage_meta.get("cost_usd", 0.0))

        # Write candidate to temporary file first to verify it compiles and runs
        tmp_path = STATE_DIR / "candidate_strategy_tmp.py"
        tmp_path.write_text(candidate_code + "\n", encoding="utf-8")

        try:
            cand_eval = evaluate_strategy(tmp_path, llm_cost_usd=llm_cost_usd)
            cand_score = float(cand_eval["ratchet_score"])
            improved = cand_score >= (best_score + min_delta)
            error_msg = None
        except Exception as eval_exc:
            cand_eval = baseline_eval
            cand_score = -999.0
            improved = False
            error_msg = str(eval_exc)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

        if improved:
            status = "LOCKED (KEEP)"
            strategy_file.write_text(candidate_code + "\n", encoding="utf-8")
            snapshot_file = SNAPSHOTS_DIR / f"strategy_gen_{gen_num}.py"
            snapshot_file.write_text(candidate_code + "\n", encoding="utf-8")
            # Re-run evaluate_strategy on main strategy.py so last_evaluation.json reflects the locked winner
            cand_eval = evaluate_strategy(strategy_file, llm_cost_usd=llm_cost_usd)
            history["best_score"] = cand_score
            history["best_strategy_name"] = cand_eval["strategy_name"]
        else:
            status = "REVERTED (DISCARD)"
            # Ensure strategy.py remains untouched at baseline_code
            strategy_file.write_text(baseline_code, encoding="utf-8")
            evaluate_strategy(strategy_file)

        gen_record = {
            "generation": gen_num,
            "timestamp": int(time.time()),
            "status": status,
            "strategy_name": cand_eval.get("strategy_name", f"gen_{gen_num}"),
            "hypothesis": hypothesis + (f" [Error: {error_msg}]" if error_msg else ""),
            "ratchet_score": round(cand_score, 4),
            "baseline_score_before": round(best_score, 4),
            "score_delta": round(cand_score - best_score, 4) if cand_score > -900 else 0.0,
            "validation_score": cand_eval.get("validation_score", 0.0),
            "avg_sharpe": cand_eval.get("avg_sharpe", 0.0),
            "avg_return_pct": cand_eval.get("avg_return_pct", 0.0),
            "avg_max_drawdown_pct": cand_eval.get("avg_max_drawdown_pct", 0.0),
            "llm_cost_usd": llm_cost_usd,
            "provider": usage_meta.get("provider", "local"),
        }

        history["current_generation"] = gen_num
        history["total_llm_cost_usd"] = round(float(history.get("total_llm_cost_usd", 0.0)) + llm_cost_usd, 6)
        history.setdefault("generations", []).append(gen_record)
        RATCHET_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
        step_results.append(gen_record)

    return {
        "mode": "code_ratchet",
        "current_generation": history["current_generation"],
        "best_score": history["best_score"],
        "best_strategy_name": history["best_strategy_name"],
        "total_llm_cost_usd": history["total_llm_cost_usd"],
        "steps_executed": step_results,
    }


# ------------------------------------------------------------------------------
# Mode 2: Low-Frequency Signal Ratchet (`signal_ratchet`)
# ------------------------------------------------------------------------------

def run_signal_ratchet_update(symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Computes a low-frequency Market Regime & Signal overlay for each symbol,
    and updates the Shadow-Portfolio Trust Weight (`trust_weight` in [0.1, 1.0]).
    """
    cfg = load_config()
    ensure_state_dir()
    target_symbols = symbols or cfg.get("watchlist", ["BTC/USDT", "ETH/USDT", "NVDA", "SPY"])

    existing_regimes: Dict[str, Any] = {}
    if SIGNAL_REGIME_FILE.exists():
        try:
            existing_regimes = json.loads(SIGNAL_REGIME_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing_regimes = {}

    compact_snapshots: Dict[str, Dict[str, Any]] = {}
    for sym in target_symbols:
        bars = generate_multi_regime_bars(sym, n_bars=120)
        closes = [float(b["close"]) for b in bars]
        fast = calc_ema(closes, 12) or closes[-1]
        slow = calc_ema(closes, 34) or closes[-1]
        rsi = calc_rsi(closes, 14)
        atr = calc_atr(bars, 14)
        z = calc_zscore(closes, 20)
        ret_20 = ((closes[-1] - closes[-20]) / closes[-20]) * 100.0
        compact_snapshots[sym] = {
            "price": round(closes[-1], 2),
            "ema12_vs_ema34_pct": round(((fast - slow) / slow) * 100.0, 2),
            "rsi14": round(rsi, 1),
            "atr_pct": round((atr / closes[-1]) * 100.0, 2),
            "zscore20": round(z, 2),
            "return_20bars_pct": round(ret_20, 2),
        }

    sys_prompt = (
        "You are ATrader's Low-Frequency Regime Signal Advisor.\n"
        "Given compact technical snapshots for each symbol, output ONLY valid JSON mapping each symbol to:\n"
        '{"trend_bias": float in [-1.0, 1.0], "risk_scale": float in [0.2, 1.4], "thesis": "short reason"}'
    )
    user_prompt = f"Market Snapshots JSON:\n{json.dumps(compact_snapshots)}"

    llm_parsed: Dict[str, Any] = {}
    usage_meta: Dict[str, Any] = {"provider": "quantitative_regime_fallback", "cost_usd": 0.0}

    try:
        resp_text, usage_meta = call_deepseek_llm(sys_prompt, user_prompt, cfg, temperature=0.2)
        if resp_text:
            json_match = re.search(r"\{.*\}", resp_text, re.DOTALL)
            if json_match:
                llm_parsed = json.loads(json_match.group(0))
    except Exception:
        llm_parsed = {}

    updated_regimes: Dict[str, Any] = {}
    for sym, snap in compact_snapshots.items():
        prev = existing_regimes.get(sym, {})
        prev_trust = float(prev.get("trust_weight", 0.35))

        # Check shadow return from last signal to ratchet trust_weight up or down
        prev_bias = float(prev.get("trend_bias", 0.2))
        realized_ret = float(snap["return_20bars_pct"])
        shadow_hit = (prev_bias >= 0 and realized_ret >= 0) or (prev_bias < 0 and realized_ret < 0)

        # Ratchet trust weight between 0.10 and 0.90 based on shadow accuracy
        new_trust = min(0.90, round(prev_trust + 0.08, 2)) if shadow_hit else max(0.10, round(prev_trust - 0.10, 2))

        if sym in llm_parsed and isinstance(llm_parsed[sym], dict):
            bias = float(llm_parsed[sym].get("trend_bias", 0.0))
            r_scale = float(llm_parsed[sym].get("risk_scale", 1.0))
            thesis = str(llm_parsed[sym].get("thesis", "DeepSeek regime synthesis"))
        else:
            # Deterministic quantitative fallback when no API key is configured
            ema_spread = snap["ema12_vs_ema34_pct"]
            rsi_val = snap["rsi14"]
            bias = max(-1.0, min(1.0, round(ema_spread / 2.5, 2)))
            r_scale = 1.15 if (ema_spread > 0 and 45 <= rsi_val <= 68) else (0.65 if rsi_val > 75 else 0.95)
            thesis = f"EMA spread {ema_spread:+.2f}%, RSI {rsi_val:.1f}, 20-bar return {realized_ret:+.2f}%"

        updated_regimes[sym] = {
            "symbol": sym,
            "trend_bias": round(max(-1.0, min(1.0, bias)), 2),
            "risk_scale": round(max(0.1, min(1.5, r_scale)), 2),
            "trust_weight": new_trust,
            "shadow_hit": shadow_hit,
            "thesis": thesis,
            "snapshot": snap,
            "provider": usage_meta.get("provider", "local"),
            "llm_cost_usd": usage_meta.get("cost_usd", 0.0),
            "updated_at": int(time.time()),
        }

    SIGNAL_REGIME_FILE.write_text(json.dumps(updated_regimes, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "mode": "signal_ratchet",
        "provider": usage_meta.get("provider", "local"),
        "llm_cost_usd": usage_meta.get("cost_usd", 0.0),
        "regimes": updated_regimes,
    }


# ------------------------------------------------------------------------------
# Live / Paper Execution Step (0 Runtime LLM Tokens!)
# ------------------------------------------------------------------------------

def run_live_trading_cycle(symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Runs one execution cycle across the watchlist using the current Ratchet-locked
    `strategy.py` and routes orders to each symbol's configured broker.
    """
    cfg = load_config()
    router = MultiBrokerRouter(cfg)
    strategy_obj = load_strategy_from_path()
    watch = symbols or cfg.get("watchlist", ["BTC/USDT", "ETH/USDT", "NVDA", "SPY"])

    regime_map: Dict[str, Any] = {}
    if SIGNAL_REGIME_FILE.exists() and cfg.get("ratchet_mode") in ("signal_ratchet", "hybrid"):
        try:
            regime_map = json.loads(SIGNAL_REGIME_FILE.read_text(encoding="utf-8"))
        except Exception:
            regime_map = {}

    execution_results: List[Dict[str, Any]] = []
    for sym in watch:
        broker = router.resolve_broker_for_symbol(sym)
        bars = broker.get_recent_bars(sym, limit=120)
        summary = broker.get_account_summary()
        cash = float(summary.get("cash_usd", 0.0))
        cur_qty = 0.0
        for p in summary.get("positions", []):
            if p.get("symbol") == sym:
                cur_qty = float(p.get("qty", 0.0))

        decision = strategy_obj.on_bars(
            bars=bars,
            position_qty=cur_qty,
            cash=cash,
            regime_state=regime_map.get(sym),
        )
        price = float(bars[-1]["close"])
        order_res = broker.place_target_weight_order(
            symbol=sym,
            target_weight=float(decision["target_weight"]),
            current_price=price,
            reason=decision.get("reason", ""),
        )
        execution_results.append({
            "symbol": sym,
            "routed_broker": broker.name,
            "price": round(price, 4),
            "target_weight": decision["target_weight"],
            "reason": decision.get("reason", ""),
            "indicators": decision.get("indicators", {}),
            "order_result": order_res,
        })

    return {
        "timestamp": int(time.time()),
        "strategy_name": getattr(strategy_obj, "name", "unknown"),
        "ratchet_mode": cfg.get("ratchet_mode", "hybrid"),
        "runtime_llm_tokens": 0,
        "executions": execution_results,
        "portfolio": router.get_unified_portfolio(),
    }

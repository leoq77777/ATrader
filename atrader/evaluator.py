"""
ATrader Cost-Aware Strategy Evaluator & Backtest Harness (The Immutable Referee)
================================================================================
In the Karpathy Ratchet architecture, `evaluator.py` is the immutable ground-truth
harness. It evaluates any candidate `Strategy` class across multi-regime OHLCV bars
(In-Sample Train + Out-of-Sample Validation), deducts trading fees, slippage, and
amortized LLM API token cost, and produces a single scalar `ratchet_score`.
"""

import importlib.util
import math
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from atrader.config import PROJECT_ROOT, STATE_DIR, load_config


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


def load_strategy_from_path(strategy_path: Optional[Path] = None) -> Any:
    """Dynamically loads the `Strategy` class from a Python file."""
    target = strategy_path or (PROJECT_ROOT / "strategy.py")
    spec = importlib.util.spec_from_file_location("candidate_strategy", str(target))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load strategy spec from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "Strategy"):
        raise AttributeError(f"{target} does not define a `Strategy` class")
    return module.Strategy()


def run_backtest_segment(
    strategy_instance: Any,
    bars: List[Dict[str, Any]],
    initial_cash: float = 10000.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 3.0,
    regime_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Runs a deterministic bar-by-bar simulation on a slice of OHLCV bars.
    """
    cash = float(initial_cash)
    position_qty = 0.0
    cost_rate = (fee_bps + slippage_bps) / 10000.0

    equity_curve: List[float] = []
    returns: List[float] = []
    trades: List[Dict[str, Any]] = []
    total_fees_usd = 0.0
    peak_equity = initial_cash
    max_drawdown = 0.0

    for idx in range(len(bars)):
        window = bars[: idx + 1]
        bar = bars[idx]
        price = float(bar["close"])

        decision = strategy_instance.on_bars(
            bars=window,
            position_qty=position_qty,
            cash=cash,
            regime_state=regime_state,
        )
        target_weight = float(decision.get("target_weight", 0.0))
        target_weight = max(0.0, min(1.0, target_weight))

        current_equity = cash + position_qty * price
        desired_notional = current_equity * target_weight
        current_notional = position_qty * price
        delta_notional = desired_notional - current_notional

        # Rebalance threshold: avoid micro-churn if delta is < 2.5% of equity
        if abs(delta_notional) > current_equity * 0.025 and price > 0:
            fee_usd = abs(delta_notional) * cost_rate
            total_fees_usd += fee_usd
            if delta_notional > 0:
                buy_usd = min(cash - fee_usd, delta_notional)
                if buy_usd > 0:
                    qty_bought = buy_usd / price
                    position_qty += qty_bought
                    cash -= (buy_usd + fee_usd)
                    trades.append({
                        "bar_idx": idx,
                        "timestamp": bar["timestamp"],
                        "side": "BUY",
                        "price": round(price, 4),
                        "qty": round(qty_bought, 6),
                        "fee_usd": round(fee_usd, 4),
                        "reason": decision.get("reason", ""),
                    })
            else:
                sell_usd = min(current_notional, abs(delta_notional))
                if sell_usd > 0:
                    qty_sold = sell_usd / price
                    position_qty = max(0.0, position_qty - qty_sold)
                    cash += (sell_usd - fee_usd)
                    trades.append({
                        "bar_idx": idx,
                        "timestamp": bar["timestamp"],
                        "side": "SELL",
                        "price": round(price, 4),
                        "qty": round(qty_sold, 6),
                        "fee_usd": round(fee_usd, 4),
                        "reason": decision.get("reason", ""),
                    })

        end_equity = cash + position_qty * price
        if equity_curve:
            prev_eq = equity_curve[-1]
            ret = (end_equity - prev_eq) / prev_eq if prev_eq > 0 else 0.0
            returns.append(ret)
        equity_curve.append(round(end_equity, 2))

        if end_equity > peak_equity:
            peak_equity = end_equity
        dd = (peak_equity - end_equity) / peak_equity if peak_equity > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    final_equity = equity_curve[-1] if equity_curve else initial_cash
    net_profit_usd = final_equity - initial_cash
    net_return_pct = (net_profit_usd / initial_cash) * 100.0

    # Annualized Sharpe (assuming 4h bars -> 6 * 365 = 2190 bars/year)
    if len(returns) > 2:
        mean_ret = sum(returns) / len(returns)
        var_ret = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_ret = math.sqrt(var_ret)
        sharpe = (mean_ret / std_ret) * math.sqrt(2190.0) if std_ret > 1e-9 else 0.0
    else:
        sharpe = 0.0

    # Benchmark Buy & Hold return
    bh_return_pct = (
        ((float(bars[-1]["close"]) - float(bars[0]["close"])) / float(bars[0]["close"])) * 100.0
        if len(bars) >= 2
        else 0.0
    )

    return {
        "initial_cash": initial_cash,
        "final_equity": round(final_equity, 2),
        "net_profit_usd": round(net_profit_usd, 2),
        "net_return_pct": round(net_return_pct, 2),
        "benchmark_return_pct": round(bh_return_pct, 2),
        "alpha_pct": round(net_return_pct - bh_return_pct, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "trade_count": len(trades),
        "total_fees_usd": round(total_fees_usd, 2),
        "equity_curve": equity_curve,
        "recent_trades": trades[-8:],
    }


def evaluate_strategy(
    strategy_path: Optional[Path] = None,
    symbols: Optional[List[str]] = None,
    llm_cost_usd: float = 0.0,
) -> Dict[str, Any]:
    """
    Evaluates a strategy across multiple symbols using a Walk-Forward split:
      - 70% In-Sample (Train)
      - 30% Out-of-Sample (Validation)
    Computes the unified Cost-Aware Ratchet Score (`ratchet_score`).
    """
    cfg = load_config()
    eval_symbols = symbols or cfg.get("watchlist", ["BTC/USDT", "ETH/USDT", "NVDA", "SPY"])
    initial_cash = float(cfg.get("initial_paper_cash", 10000.0))
    fee_bps = float(cfg.get("trading_fee_bps", 5.0))
    slippage_bps = float(cfg.get("slippage_bps", 3.0))

    # Check if there is an active signal_ratchet regime overlay
    regime_file = STATE_DIR / "signal_regime.json"
    regime_map: Dict[str, Any] = {}
    if regime_file.exists() and cfg.get("ratchet_mode") in ("signal_ratchet", "hybrid"):
        try:
            regime_map = json.loads(regime_file.read_text(encoding="utf-8"))
        except Exception:
            regime_map = {}

    strategy_obj = load_strategy_from_path(strategy_path)

    train_scores: List[float] = []
    val_scores: List[float] = []
    per_symbol_results: Dict[str, Any] = {}
    total_net_profit_usd = 0.0
    total_fees_usd = 0.0
    combined_equity_curve: List[float] = []

    for sym in eval_symbols:
        bars = generate_multi_regime_bars(sym, n_bars=480)
        split_idx = int(len(bars) * 0.70)
        train_bars = bars[:split_idx]
        val_bars = bars[split_idx - 36 :]  # include 36-bar indicator warmup

        sym_regime = regime_map.get(sym)
        train_res = run_backtest_segment(
            strategy_obj, train_bars, initial_cash=initial_cash,
            fee_bps=fee_bps, slippage_bps=slippage_bps, regime_state=sym_regime
        )
        val_res = run_backtest_segment(
            strategy_obj, val_bars, initial_cash=initial_cash,
            fee_bps=fee_bps, slippage_bps=slippage_bps, regime_state=sym_regime
        )
        full_res = run_backtest_segment(
            strategy_obj, bars, initial_cash=initial_cash,
            fee_bps=fee_bps, slippage_bps=slippage_bps, regime_state=sym_regime
        )

        # Score component per symbol: reward Sharpe + Return + Alpha, penalize Drawdown
        t_score = (
            train_res["sharpe"] * 0.45
            + (train_res["net_return_pct"] / 15.0) * 0.35
            + (train_res["alpha_pct"] / 15.0) * 0.20
            - (train_res["max_drawdown_pct"] / 12.0) * 0.35
        )
        v_score = (
            val_res["sharpe"] * 0.50
            + (val_res["net_return_pct"] / 10.0) * 0.35
            + (val_res["alpha_pct"] / 10.0) * 0.15
            - (val_res["max_drawdown_pct"] / 10.0) * 0.40
        )
        train_scores.append(t_score)
        val_scores.append(v_score)

        total_net_profit_usd += full_res["net_profit_usd"] / len(eval_symbols)
        total_fees_usd += full_res["total_fees_usd"] / len(eval_symbols)
        if not combined_equity_curve:
            combined_equity_curve = [eq / len(eval_symbols) for eq in full_res["equity_curve"]]
        else:
            combined_equity_curve = [
                round(a + b / len(eval_symbols), 2)
                for a, b in zip(combined_equity_curve, full_res["equity_curve"])
            ]

        per_symbol_results[sym] = {
            "train": {k: v for k, v in train_res.items() if k != "equity_curve"},
            "validation": {k: v for k, v in val_res.items() if k != "equity_curve"},
            "full": {k: v for k, v in full_res.items() if k != "equity_curve"},
        }

    avg_train = sum(train_scores) / len(train_scores) if train_scores else 0.0
    avg_val = sum(val_scores) / len(val_scores) if val_scores else 0.0

    # Overfitting penalty if train >> validation
    overfit_gap = max(0.0, avg_train - avg_val - 0.35) * 0.25

    # Model Cost vs Strategy Profit Penalty:
    # Deduct direct LLM token cost impact scaled against strategy profit
    net_profit_after_llm = total_net_profit_usd - llm_cost_usd
    cost_efficiency_ratio = (
        round(total_net_profit_usd / max(llm_cost_usd, 0.0001), 1)
        if llm_cost_usd > 0
        else 9999.0
    )
    llm_cost_penalty = (llm_cost_usd / max(initial_cash * 0.01, 1.0)) * 0.5

    # Final scalar Karpathy Ratchet Score (Weight validation 65%, train 35%)
    ratchet_score = round((0.35 * avg_train + 0.65 * avg_val) - overfit_gap - llm_cost_penalty, 4)

    avg_sharpe = round(
        sum(per_symbol_results[s]["full"]["sharpe"] for s in eval_symbols) / len(eval_symbols), 3
    )
    avg_dd = round(
        sum(per_symbol_results[s]["full"]["max_drawdown_pct"] for s in eval_symbols) / len(eval_symbols), 2
    )
    avg_return_pct = round((total_net_profit_usd / initial_cash) * 100.0, 2)

    summary = {
        "strategy_name": getattr(strategy_obj, "name", "unknown"),
        "strategy_description": getattr(strategy_obj, "description", ""),
        "strategy_params": getattr(strategy_obj, "params", {}),
        "ratchet_score": ratchet_score,
        "train_score": round(avg_train, 4),
        "validation_score": round(avg_val, 4),
        "avg_sharpe": avg_sharpe,
        "avg_return_pct": avg_return_pct,
        "avg_max_drawdown_pct": avg_dd,
        "net_profit_usd": round(total_net_profit_usd, 2),
        "net_profit_after_llm_usd": round(net_profit_after_llm, 2),
        "total_trading_fees_usd": round(total_fees_usd, 2),
        "llm_cost_usd": round(llm_cost_usd, 6),
        "profit_to_model_cost_ratio": cost_efficiency_ratio,
        "equity_curve": combined_equity_curve[::4],  # Downsampled for fast UI rendering
        "symbols": per_symbol_results,
    }

    # Persist latest evaluation snapshot
    (STATE_DIR / "last_evaluation.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary

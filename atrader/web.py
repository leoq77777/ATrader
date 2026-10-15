"""
ATrader Interactive Single-Page Web Dashboard & REST Server
===========================================================
Trader-centric interface modeled after modern trading terminals (TradingView,
Robinhood, Binance, Longbridge). Provides instant visibility into:
  1. Overall Portfolio Equity & PnL (Hero Bar & Asset Allocation Bar)
  2. Active Strategy & Agent Brain (Strategy logic, parameters, recent trade rationale)
  3. Live Open Positions (Holdings, cost basis, market value, unrealized PnL, target weight)
  4. Asset Watchlist & Live Dynamics (Prices, recent change %, range, agent sentiment)
  5. Dual-Agent Arena (Head-to-head comparison between ATrader and Agent B)
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Any, List
from urllib.parse import urlparse

from atrader.config import PROJECT_ROOT, STATE_DIR, load_config, save_config
from atrader.evaluator import evaluate_strategy
from atrader.ratchet import (
    get_ratchet_history,
    run_code_ratchet_step,
    run_signal_ratchet_update,
    run_live_trading_cycle,
    SIGNAL_REGIME_FILE,
)
from atrader.brokers import MultiBrokerRouter
from atrader.arena import run_agent_comparison


def collect_dashboard_state() -> Dict[str, Any]:
    cfg = load_config()
    history = get_ratchet_history()
    last_eval_file = STATE_DIR / "last_evaluation.json"
    if last_eval_file.exists():
        try:
            evaluation = json.loads(last_eval_file.read_text(encoding="utf-8"))
        except Exception:
            evaluation = evaluate_strategy()
    else:
        evaluation = evaluate_strategy()

    if SIGNAL_REGIME_FILE.exists():
        try:
            regimes = json.loads(SIGNAL_REGIME_FILE.read_text(encoding="utf-8"))
        except Exception:
            regimes = {}
    else:
        regimes = run_signal_ratchet_update().get("regimes", {})

    router = MultiBrokerRouter(cfg)
    portfolio = router.get_unified_portfolio()
    strategy_code = (PROJECT_ROOT / "strategy.py").read_text(encoding="utf-8")

    # Aggregate all positions across active accounts into a clean, flat list
    master = portfolio.get("virtual_master_ledger", {})
    total_eq = max(float(master.get("total_equity_usd", 10000.0)), 1.0)
    flat_positions: List[Dict[str, Any]] = []

    for acct in portfolio.get("accounts", []):
        b_name = acct.get("broker", "unknown")
        for p in acct.get("positions", []):
            sym = p.get("symbol", "")
            qty = float(p.get("qty", 0.0))
            if qty <= 1e-9:
                continue
            avg_p = float(p.get("avg_price", 0.0))
            last_p = float(p.get("last_price", avg_p))
            val = float(p.get("market_value_usd", qty * last_p))
            pnl_usd = float(p.get("unrealized_pnl_usd", (last_p - avg_p) * qty))
            pnl_pct = round(((last_p - avg_p) / avg_p * 100.0), 2) if avg_p > 0 else 0.0
            weight_pct = round((val / total_eq) * 100.0, 1)

            reg = regimes.get(sym, {})
            target_pct = round(float(reg.get("risk_scale", 1.0)) * 25.0, 1)

            flat_positions.append({
                "symbol": sym,
                "broker": b_name,
                "qty": qty,
                "avg_price": round(avg_p, 4),
                "last_price": round(last_p, 4),
                "market_value_usd": round(val, 2),
                "unrealized_pnl_usd": round(pnl_usd, 2),
                "unrealized_pnl_pct": pnl_pct,
                "weight_pct": weight_pct,
                "target_weight_pct": min(25.0, target_pct),
            })

    # Build market dynamics watchlist
    watchlist_items: List[Dict[str, Any]] = []
    symbol_routing = portfolio.get("symbol_routing", {})
    for sym, reg in regimes.items():
        snap = reg.get("snapshot", {})
        price = float(snap.get("price", 0.0))
        ret_20 = float(snap.get("return_20bars_pct", 0.0))
        bias = float(reg.get("trend_bias", 0.0))
        sentiment = "BULLISH" if bias > 0.2 else ("BEARISH" if bias < -0.2 else "NEUTRAL")
        watchlist_items.append({
            "symbol": sym,
            "routed_broker": symbol_routing.get(sym, "local_paper"),
            "price": round(price, 2),
            "change_pct": round(ret_20, 2),
            "trend_bias": bias,
            "sentiment": sentiment,
            "trust_weight": round(float(reg.get("trust_weight", 0.5)) * 100),
            "risk_scale": reg.get("risk_scale", 1.0),
            "thesis": reg.get("thesis", ""),
        })

    # Extract active strategy meta
    active_strat = {
        "name": evaluation.get("strategy_name", "baseline"),
        "generation": history.get("current_generation", 0),
        "description": evaluation.get("strategy_description", "Adaptive EMA Trend + RSI Filter + ATR Trailing Stop"),
        "params": evaluation.get("strategy_params", {}),
        "ratchet_score": evaluation.get("ratchet_score", 0.0),
        "total_llm_cost_usd": history.get("total_llm_cost_usd", 0.0),
        "model_roi": evaluation.get("profit_to_model_cost_ratio", "∞"),
    }

    # Run quick Arena benchmark for the comparison tab
    try:
        arena_res = run_agent_comparison()
    except Exception:
        arena_res = None

    return {
        "config": {
            "llm_model": cfg.get("llm_model", "deepseek-chat"),
            "llm_base_url": cfg.get("llm_base_url", "https://api.deepseek.com"),
            "llm_key_configured": bool(
                cfg.get("llm_api_key") and cfg.get("llm_api_key") != "your_deepseek_api_key_here"
            ),
            "ratchet_mode": cfg.get("ratchet_mode", "hybrid"),
            "active_brokers": cfg.get("active_brokers", ["local_paper"]),
        },
        "portfolio": portfolio,
        "positions": flat_positions,
        "watchlist": watchlist_items,
        "active_strategy": active_strat,
        "evaluation": evaluation,
        "ratchet_history": history,
        "arena": arena_res,
        "strategy_code": strategy_code,
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>ATrader · 极简量化交易终端 (The Karpathy Ratchet)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-base: #070a0f;
    --bg-surface: #0e141f;
    --bg-card: rgba(16, 23, 36, 0.78);
    --bg-card-hover: rgba(22, 33, 52, 0.92);
    --border: rgba(255, 255, 255, 0.08);
    --border-accent: rgba(56, 189, 248, 0.35);
    --text-primary: #f8fafc;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --cyan: #38bdf8;
    --cyan-glow: rgba(56, 189, 248, 0.25);
    --emerald: #10b981;
    --emerald-glow: rgba(16, 185, 129, 0.22);
    --rose: #f43f5e;
    --rose-glow: rgba(244, 63, 94, 0.22);
    --amber: #f59e0b;
    --purple: #a855f7;
    --font-sans: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, Menlo, Monaco, monospace;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: var(--font-sans);
    background: var(--bg-base);
    background-image:
      radial-gradient(ellipse 90% 45% at 50% -10%, rgba(56, 189, 248, 0.08), transparent 70%),
      radial-gradient(ellipse 50% 30% at 85% 15%, rgba(168, 85, 247, 0.06), transparent 60%);
    color: var(--text-primary);
    line-height: 1.5;
    padding: 20px 28px 48px;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }

  /* Header Navbar */
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 14px 20px;
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 20px;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo {
    width: 34px; height: 34px; border-radius: 9px;
    background: linear-gradient(135deg, #0284c7, #38bdf8);
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 18px; color: #fff;
    box-shadow: 0 0 16px var(--cyan-glow);
  }
  .brand-name { font-size: 17px; font-weight: 800; letter-spacing: -0.3px; display: flex; align-items: center; gap: 8px; }
  .brand-tag { font-size: 11px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
  .pulse-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--emerald); box-shadow: 0 0 10px var(--emerald); animation: pulse 2s infinite ease-in-out; }
  @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.85); } }

  .toolbar { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
  .mode-switch { display: flex; background: rgba(0, 0, 0, 0.4); padding: 3px; border-radius: 8px; border: 1px solid var(--border); }
  .mode-btn { background: transparent; border: none; color: var(--text-secondary); padding: 5px 11px; border-radius: 6px; font-size: 11px; font-weight: 600; cursor: pointer; transition: all 0.15s; }
  .mode-btn.active { background: rgba(56, 189, 248, 0.16); color: var(--cyan); border: 1px solid rgba(56, 189, 248, 0.35); }

  .btn {
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(30, 41, 59, 0.7); color: var(--text-primary);
    border: 1px solid var(--border); padding: 7px 13px; border-radius: 8px;
    font-size: 12px; font-weight: 600; cursor: pointer; transition: all 0.16s;
  }
  .btn:hover { border-color: rgba(255, 255, 255, 0.22); background: rgba(45, 62, 90, 0.85); transform: translateY(-1px); }
  .btn.primary { background: linear-gradient(135deg, #0284c7, #0ea5e9); border-color: #38bdf8; color: #fff; box-shadow: 0 0 14px rgba(14, 165, 233, 0.3); }
  .btn.success { background: linear-gradient(135deg, #059669, #10b981); border-color: #34d399; color: #fff; box-shadow: 0 0 14px rgba(16, 185, 129, 0.3); }
  .btn.loading { opacity: 0.65; pointer-events: none; }

  /* Section 1: Hero Portfolio Overview (Robinhood / Binance style) */
  .hero-portfolio {
    background: var(--bg-card);
    backdrop-filter: blur(14px);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 22px 24px;
    margin-bottom: 20px;
    display: grid;
    grid-template-columns: 1.4fr 2.6fr;
    gap: 24px;
    align-items: center;
  }
  @media (max-width: 960px) { .hero-portfolio { grid-template-columns: 1fr; } }

  .equity-hero-left {
    border-right: 1px solid var(--border);
    padding-right: 20px;
  }
  @media (max-width: 960px) { .equity-hero-left { border-right: none; padding-right: 0; border-bottom: 1px solid var(--border); padding-bottom: 18px; } }

  .equity-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--text-secondary); margin-bottom: 4px; }
  .equity-big { font-family: var(--font-mono); font-size: 36px; font-weight: 800; letter-spacing: -1px; margin-bottom: 6px; }
  .pnl-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 6px; font-family: var(--font-mono); font-size: 13px; font-weight: 700;
  }
  .pnl-chip.pos { background: rgba(16, 185, 129, 0.14); color: var(--emerald); border: 1px solid rgba(16, 185, 129, 0.3); }
  .pnl-chip.neg { background: rgba(244, 63, 94, 0.14); color: var(--rose); border: 1px solid rgba(244, 63, 94, 0.3); }

  .equity-hero-right {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .stat-triplet {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
  }
  .stat-box {
    background: rgba(8, 12, 18, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 9px;
    padding: 10px 14px;
  }
  .stat-box-title { font-size: 11px; color: var(--text-muted); margin-bottom: 3px; }
  .stat-box-val { font-family: var(--font-mono); font-size: 16px; font-weight: 700; color: var(--text-primary); }

  /* Allocation Bar */
  .alloc-wrapper { display: flex; flex-direction: column; gap: 6px; }
  .alloc-header { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-secondary); }
  .alloc-bar {
    height: 10px; width: 100%; border-radius: 5px;
    background: rgba(255, 255, 255, 0.05);
    display: flex; overflow: hidden;
  }
  .alloc-seg { height: 100%; transition: width 0.3s; }
  .alloc-legend { display: flex; gap: 14px; font-size: 11px; flex-wrap: wrap; margin-top: 4px; }
  .legend-item { display: flex; align-items: center; gap: 5px; color: var(--text-secondary); }
  .legend-dot { width: 8px; height: 8px; border-radius: 2px; }

  /* 2-Column Core: Strategy Brain + Market Dynamics */
  .grid-2 {
    display: grid;
    grid-template-columns: 1.15fr 1fr;
    gap: 18px;
    margin-bottom: 20px;
  }
  @media (max-width: 1080px) { .grid-2 { grid-template-columns: 1fr; } }

  .card {
    background: var(--bg-card);
    backdrop-filter: blur(14px);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
  }
  .card-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 14px; padding-bottom: 10px; border-bottom: 1px solid var(--border);
  }
  .card-title { font-size: 13.5px; font-weight: 700; display: flex; align-items: center; gap: 8px; letter-spacing: -0.2px; }

  /* Strategy Brain Card Specifics */
  .strat-name-box {
    background: rgba(56, 189, 248, 0.08);
    border: 1px solid rgba(56, 189, 248, 0.22);
    border-radius: 9px;
    padding: 10px 14px;
    margin-bottom: 12px;
  }
  .strat-name { font-family: var(--font-mono); font-size: 14px; font-weight: 700; color: var(--cyan); margin-bottom: 3px; }
  .strat-desc { font-size: 12px; color: var(--text-secondary); line-height: 1.45; }

  .params-pill-grid {
    display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px;
  }
  .param-pill {
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 11px;
    font-family: var(--font-mono);
    color: var(--text-secondary);
  }
  .param-pill b { color: var(--text-primary); }

  .decision-blotter {
    background: rgba(8, 12, 18, 0.6);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 11.5px;
  }
  .decision-title { font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin-bottom: 6px; }
  .decision-item { display: flex; justify-content: space-between; gap: 8px; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.03); }
  .decision-item:last-child { border-bottom: none; }

  /* Positions & Tables */
  .table-box {
    background: var(--bg-card);
    backdrop-filter: blur(14px);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 20px;
    margin-bottom: 20px;
  }
  .table-wrap { overflow-x: auto; max-height: 380px; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    position: sticky; top: 0; background: #0c121d; color: var(--text-muted);
    font-weight: 600; text-align: left; padding: 9px 12px; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid var(--border); z-index: 2;
  }
  td { padding: 11px 12px; border-bottom: 1px solid rgba(255, 255, 255, 0.04); color: var(--text-primary); }
  tr:hover td { background: rgba(56, 189, 248, 0.03); }
  .mono { font-family: var(--font-mono); }
  .pos { color: var(--emerald); font-weight: 600; }
  .neg { color: var(--rose); font-weight: 600; }

  /* Tabs Section */
  .tabs-nav {
    display: flex; gap: 4px; background: rgba(0, 0, 0, 0.35); padding: 4px; border-radius: 9px;
    margin-bottom: 16px; border: 1px solid var(--border);
  }
  .tab-item {
    flex: 1; text-align: center; padding: 7px 12px; font-size: 12px; font-weight: 600;
    color: var(--text-secondary); border-radius: 7px; cursor: pointer; transition: all 0.15s; user-select: none;
  }
  .tab-item:hover { color: var(--text-primary); }
  .tab-item.active { background: rgba(56, 189, 248, 0.15); color: var(--cyan); font-weight: 700; border: 1px solid rgba(56, 189, 248, 0.3); }

  /* Arena Scorecard Styles */
  .arena-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 14px;
  }
  @media (max-width: 800px) { .arena-grid { grid-template-columns: 1fr; } }
  .arena-card {
    background: rgba(8, 12, 18, 0.6);
    border: 1px solid var(--border); border-radius: 10px; padding: 16px;
  }
  .arena-winner { border-color: var(--emerald); box-shadow: 0 0 20px rgba(16, 185, 129, 0.12); }
  .arena-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
  .arena-title { font-size: 14px; font-weight: 800; }
  .arena-stat-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.03); font-size: 12px; }
  .verdict-banner {
    padding: 12px 16px; border-radius: 9px; background: rgba(16, 185, 129, 0.1);
    border: 1px solid rgba(16, 185, 129, 0.3); color: var(--emerald); font-size: 13px; font-weight: 700;
    margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
  }

  /* SVG Chart */
  .chart-box {
    position: relative; width: 100%; height: 230px; background: rgba(6, 9, 14, 0.5);
    border-radius: 9px; border: 1px solid rgba(255, 255, 255, 0.04); overflow: hidden;
  }
  .chart-tooltip {
    position: absolute; display: none; padding: 6px 10px; background: rgba(15, 23, 42, 0.95);
    border: 1px solid var(--cyan); border-radius: 6px; font-family: var(--font-mono); font-size: 11px;
    color: #fff; pointer-events: none; z-index: 10;
  }

  /* Toast Notification */
  #toast {
    position: fixed; bottom: 24px; right: 28px; background: rgba(15, 23, 42, 0.95);
    border: 1px solid var(--cyan); color: #fff; padding: 11px 18px; border-radius: 9px;
    font-size: 12.5px; font-weight: 600; display: flex; align-items: center; gap: 9px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6); transform: translateY(100px); opacity: 0;
    transition: all 0.22s cubic-bezier(0.16, 1, 0.3, 1); z-index: 999;
  }
  #toast.show { transform: translateY(0); opacity: 1; }
</style>
</head>
<body>

<!-- Header -->
<header>
  <div class="brand">
    <div class="logo">⚡</div>
    <div>
      <div class="brand-name">
        ATrader
        <span class="mode-btn active" id="mode-badge" style="font-size:10px; padding:2px 7px;">HYBRID</span>
      </div>
      <div class="brand-tag">
        <div class="pulse-dot"></div>
        <span>The Karpathy Ratchet Quantitative Terminal</span>
        <span>·</span>
        <span id="model-status" style="font-family:var(--font-mono); color:var(--cyan)">DeepSeek Connected</span>
      </div>
    </div>
  </div>

  <div class="toolbar">
    <div class="mode-switch">
      <button class="mode-btn" id="m-hybrid" onclick="changeMode('hybrid')">Hybrid (双模)</button>
      <button class="mode-btn" id="m-code" onclick="changeMode('code_ratchet')">Code Only (0-Token)</button>
      <button class="mode-btn" id="m-signal" onclick="changeMode('signal_ratchet')">Signal Only</button>
    </div>

    <button class="btn primary" onclick="triggerAction('/api/evolve', '⚡ 正在执行策略代码棘轮演化 (Evolve)...', this)">
      <span>⚡</span> 演化新策略 (Evolve)
    </button>
    <button class="btn" onclick="triggerAction('/api/signal', '📡 正在更新低频行情状态...', this)">
      <span>📡</span> 更新信号 (Signal)
    </button>
    <button class="btn success" onclick="triggerAction('/api/trade', '🚀 正在执行 0-Token 调仓...', this)">
      <span>🚀</span> 立即调仓 (Trade)
    </button>
    <button class="btn" onclick="triggerAction('/api/backtest', '🔄 正在运行样本外回测...', this)">
      <span>🔄</span> 回测 (Backtest)
    </button>
  </div>
</header>

<!-- Hero Section: Total Equity, PnL & Asset Allocation Bar -->
<section class="hero-portfolio">
  <div class="equity-hero-left">
    <div class="equity-label">账户总净资产 (Total Net Worth)</div>
    <div class="equity-big" id="hero-equity">$0.00</div>
    <div style="display:flex; align-items:center; gap:8px;">
      <span class="pnl-chip pos" id="hero-pnl-chip">+0.00 (+0.0%)</span>
      <span style="font-size:11.5px; color:var(--text-muted);">今日浮动盈亏</span>
    </div>
  </div>

  <div class="equity-hero-right">
    <div class="stat-triplet">
      <div class="stat-box">
        <div class="stat-box-title">可用现金 (Cash)</div>
        <div class="stat-box-val" id="hero-cash">$0.00</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-title">持仓市值 (Positions)</div>
        <div class="stat-box-val" id="hero-pos-val">$0.00</div>
      </div>
      <div class="stat-box">
        <div class="stat-box-title">大模型成本 / 算力 ROI</div>
        <div class="stat-box-val" id="hero-cost-roi" style="color:var(--cyan)">$0.00 / 0x</div>
      </div>
    </div>

    <!-- Allocation Bar -->
    <div class="alloc-wrapper">
      <div class="alloc-header">
        <span>资产配置分布 (Asset Allocation)</span>
        <span id="alloc-summary">现金 100% · 证券 0%</span>
      </div>
      <div class="alloc-bar" id="alloc-bar">
        <div class="alloc-seg" id="seg-cash" style="width: 100%; background: #0284c7;"></div>
      </div>
      <div class="alloc-legend" id="alloc-legend">
        <div class="legend-item"><div class="legend-dot" style="background:#0284c7"></div>可用现金</div>
      </div>
    </div>
  </div>
</section>

<!-- 2-Column: Strategy Brain + Market Dynamics -->
<div class="grid-2">
  <!-- Active Strategy & Agent Brain -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <span>🎯</span>
        <span>当前运行策略与大脑状态 (Active Strategy & Brain)</span>
      </div>
      <span class="mode-btn active" id="strat-gen-badge">Gen 0</span>
    </div>

    <div class="strat-name-box">
      <div class="strat-name" id="strat-name">Loading strategy...</div>
      <div class="strat-desc" id="strat-desc">--</div>
    </div>

    <div style="font-size:11px; font-weight:600; color:var(--text-muted); text-transform:uppercase; margin-bottom:6px;">关键指标参数 (Indicator Parameters)</div>
    <div class="params-pill-grid" id="params-pills"></div>

    <div class="decision-blotter">
      <div class="decision-title">最近一次决策记录 (Latest Decision Rationale)</div>
      <div id="decisions-list">
        <div class="decision-item" style="color:var(--text-muted)">正在等待最近一轮调仓执行数据...</div>
      </div>
    </div>
  </div>

  <!-- Asset Watchlist & Live Dynamics -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">
        <span>📊</span>
        <span>监控标的与近期行情涨跌 (Watchlist Dynamics)</span>
      </div>
      <span style="font-size:11px; color:var(--text-muted)">更新于 4h 收盘</span>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>标的代码</th>
            <th>通道</th>
            <th>最新现价</th>
            <th>近期涨跌</th>
            <th>Agent 倾向</th>
            <th>目标仓位</th>
          </tr>
        </thead>
        <tbody id="watchlist-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Section: Live Open Positions Table -->
<div class="table-box">
  <div class="card-header">
    <div class="card-title">
      <span>💼</span>
      <span>当前持仓明细 (Open Positions)</span>
    </div>
    <span style="font-size:11px; color:var(--text-muted)" id="pos-count">0 项持仓</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>标的代码</th>
          <th>交易账户</th>
          <th>持仓数量</th>
          <th>成本均价</th>
          <th>最新市价</th>
          <th>持仓市值 (USD)</th>
          <th>仓位占比</th>
          <th>浮动盈亏 (PnL)</th>
          <th>目标仓位</th>
        </tr>
      </thead>
      <tbody id="positions-tbody">
        <tr><td colspan="9" style="text-align:center; color:var(--text-muted)">暂无持仓，点击上方“立即调仓”或“演化新策略”开始建仓</td></tr>
      </tbody>
    </table>
  </div>
</div>

<!-- Bottom Section: Deep-Dive Tabs (Arena, Backtest Curve, Ratchet Timeline, Multi-Broker Custody, Code) -->
<div class="card">
  <div class="tabs-nav">
    <div class="tab-item active" onclick="switchTab('tab-arena', this)">⚔️ 双 Agent 竞技场对决 (Arena)</div>
    <div class="tab-item" onclick="switchTab('tab-curve', this)">📈 净值回测曲线 (Equity Curve)</div>
    <div class="tab-item" onclick="switchTab('tab-timeline', this)">🧬 棘轮演化历史 (Ratchet Timeline)</div>
    <div class="tab-item" onclick="switchTab('tab-brokers', this)">🏦 多账户存管总账 (Master Ledger)</div>
    <div class="tab-item" onclick="switchTab('tab-code', this)">💻 策略源码 (strategy.py)</div>
  </div>

  <!-- Tab 1: Arena -->
  <div id="tab-arena" class="tab-pane">
    <div class="verdict-banner" id="arena-verdict">
      <span>🏆</span> <span id="arena-verdict-text">正在加载双 Agent 对决数据...</span>
    </div>
    <div class="arena-grid">
      <div class="arena-card arena-winner" id="arena-card-a">
        <div class="arena-head">
          <div class="arena-title" style="color:var(--cyan)">Agent A: ATrader (Karpathy 棘轮)</div>
          <span class="mode-btn active" style="font-size:10px;">PROTAGONIST</span>
        </div>
        <div class="arena-stat-row"><span>Ratchet 评分</span><b class="mono" id="ar-score-a">--</b></div>
        <div class="arena-stat-row"><span>年化夏普比率 (Sharpe)</span><b class="mono pos" id="ar-sharpe-a">--</b></div>
        <div class="arena-stat-row"><span>样本外净收益率</span><b class="mono pos" id="ar-ret-a">--</b></div>
        <div class="arena-stat-row"><span>最大回撤 (MaxDD)</span><b class="mono neg" id="ar-dd-a">--</b></div>
        <div class="arena-stat-row"><span>LLM Token API 成本</span><b class="mono" id="ar-cost-a">--</b></div>
        <div class="arena-stat-row"><span>扣除成本后净利润</span><b class="mono pos" id="ar-profit-a">--</b></div>
        <div class="arena-stat-row"><span>算力 ROI (利润/Token 成本)</span><b class="mono" style="color:var(--purple)" id="ar-roi-a">--</b></div>
      </div>

      <div class="arena-card" id="arena-card-b">
        <div class="arena-head">
          <div class="arena-title" style="color:var(--text-secondary)">Agent B: 对比系统 / 基准</div>
          <span style="font-size:11px; color:var(--text-muted);">COMPETITOR</span>
        </div>
        <div class="arena-stat-row"><span>Ratchet 评分</span><b class="mono" id="ar-score-b">--</b></div>
        <div class="arena-stat-row"><span>年化夏普比率 (Sharpe)</span><b class="mono" id="ar-sharpe-b">--</b></div>
        <div class="arena-stat-row"><span>样本外净收益率</span><b class="mono" id="ar-ret-b">--</b></div>
        <div class="arena-stat-row"><span>最大回撤 (MaxDD)</span><b class="mono neg" id="ar-dd-b">--</b></div>
        <div class="arena-stat-row"><span>LLM Token API 成本</span><b class="mono" id="ar-cost-b">--</b></div>
        <div class="arena-stat-row"><span>扣除成本后净利润</span><b class="mono" id="ar-profit-b">--</b></div>
        <div class="arena-stat-row"><span>算力 ROI (利润/Token 成本)</span><b class="mono" id="ar-roi-b">--</b></div>
      </div>
    </div>
  </div>

  <!-- Tab 2: Equity Curve -->
  <div id="tab-curve" class="tab-pane" style="display:none;">
    <div class="chart-box" id="chart-box">
      <div class="chart-tooltip" id="chart-tooltip"></div>
      <svg id="equity-svg" width="100%" height="100%"></svg>
    </div>
  </div>

  <!-- Tab 3: Ratchet Timeline -->
  <div id="tab-timeline" class="tab-pane" style="display:none;">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>代数</th>
            <th>棘轮决策</th>
            <th>棘轮评分</th>
            <th>变化幅度</th>
            <th>Sharpe</th>
            <th>净收益 / 回撤</th>
            <th>LLM 成本</th>
            <th>变异假设与优化理由</th>
          </tr>
        </thead>
        <tbody id="timeline-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Tab 4: Multi-Broker Custody -->
  <div id="tab-brokers" class="tab-pane" style="display:none;">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>交易通道</th>
            <th>连通状态</th>
            <th>实际资金存管位置 (Custody Institution)</th>
            <th>账户总资产</th>
            <th>可用现金</th>
            <th>风险上限</th>
          </tr>
        </thead>
        <tbody id="broker-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Tab 5: Code Viewer -->
  <div id="tab-code" class="tab-pane" style="display:none;">
    <pre style="background:#070b12; padding:14px; border-radius:8px; overflow-x:auto; font-size:12px; font-family:var(--font-mono);"><code id="code-viewer">Loading strategy.py...</code></pre>
  </div>
</div>

<div id="toast">
  <span id="toast-icon">✓</span>
  <span id="toast-msg">操作成功</span>
</div>

<script>
function showToast(msg, icon = '✓') {
  const t = document.getElementById('toast');
  document.getElementById('toast-msg').textContent = msg;
  document.getElementById('toast-icon').textContent = icon;
  t.classList.add('show');
  setTimeout(() => { t.classList.remove('show'); }, 3200);
}

function switchTab(tabId, el) {
  document.querySelectorAll('.tab-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = 'none');
  el.classList.add('active');
  document.getElementById(tabId).style.display = 'block';
  if (tabId === 'tab-curve') {
    fetch('/api/state').then(r => r.json()).then(d => renderEquityChart(d.evaluation?.equity_curve || []));
  }
}

function renderEquityChart(curve) {
  const svg = document.getElementById('equity-svg');
  const box = document.getElementById('chart-box');
  const tooltip = document.getElementById('chart-tooltip');
  if (!curve || curve.length < 2) {
    svg.innerHTML = '<text x="20" y="40" fill="#64748b" font-size="12">暂无曲线数据</text>';
    return;
  }

  const w = box.clientWidth || 600;
  const h = box.clientHeight || 230;
  const padL = 20, padR = 20, padT = 24, padB = 26;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const minV = Math.min(...curve) * 0.992;
  const maxV = Math.max(...curve) * 1.008;

  const points = curve.map((v, i) => {
    const x = padL + (i / (curve.length - 1)) * plotW;
    const y = padT + plotH - ((v - minV) / Math.max(maxV - minV, 1)) * plotH;
    return { x, y, v, i };
  });

  const polylinePts = points.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
  const areaPts = `${points[0].x.toFixed(1)},${(padT + plotH).toFixed(1)} ${polylinePts} ${points[points.length-1].x.toFixed(1)},${(padT + plotH).toFixed(1)}`;

  const splitIdx = Math.floor(curve.length * 0.70);
  const splitX = points[splitIdx] ? points[splitIdx].x : (padL + plotW * 0.7);

  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = `
    <defs>
      <linearGradient id="curveGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.32"/>
        <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0"/>
      </linearGradient>
    </defs>
    <line x1="${splitX}" y1="${padT}" x2="${splitX}" y2="${padT + plotH}" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4,4" />
    <text x="${splitX + 8}" y="${padT + 14}" fill="#f59e0b" font-size="11" font-family="JetBrains Mono, monospace" font-weight="600">30% 样本外验证 →</text>
    <text x="${padL + 6}" y="${padT + 14}" fill="#94a3b8" font-size="11" font-family="JetBrains Mono, monospace">← 70% 训练集</text>
    <polygon points="${areaPts}" fill="url(#curveGrad)" />
    <polyline fill="none" stroke="#38bdf8" stroke-width="2.5" points="${polylinePts}" />
  `;

  box.onmousemove = (e) => {
    const rect = box.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const ratio = Math.max(0, Math.min(1, (mx - padL) / plotW));
    const idx = Math.min(points.length - 1, Math.round(ratio * (points.length - 1)));
    const pt = points[idx];
    if (pt) {
      tooltip.style.display = 'block';
      tooltip.style.left = `${Math.min(w - 110, Math.max(10, pt.x - 45))}px`;
      tooltip.style.top = `${Math.max(10, pt.y - 36)}px`;
      tooltip.innerHTML = `Bar #${idx}<br/><b>$${pt.v.toLocaleString()}</b>`;
    }
  };
  box.onmouseleave = () => { tooltip.style.display = 'none'; };
}

async function refreshDashboard() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    const port = data.portfolio?.virtual_master_ledger || {};
    const cfg = data.config || {};
    const strat = data.active_strategy || {};
    const hist = data.ratchet_history || {};
    const posList = data.positions || [];
    const watchList = data.watchlist || [];
    const arena = data.arena || {};

    // 1. Hero Portfolio
    const totalEq = port.total_equity_usd || 10000;
    const totalCash = port.total_cash_usd || 0;
    const totalPos = port.total_positions_usd || 0;
    const netPnl = port.total_net_pnl_usd || 0;
    const pnlPct = totalEq > 0 ? ((netPnl / totalEq) * 100).toFixed(2) : '0.00';

    document.getElementById('hero-equity').textContent = `$${totalEq.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    const pnlChip = document.getElementById('hero-pnl-chip');
    pnlChip.className = `pnl-chip ${netPnl >= 0 ? 'pos' : 'neg'}`;
    pnlChip.textContent = `${netPnl >= 0 ? '+' : ''}$${netPnl.toFixed(2)} (${netPnl >= 0 ? '+' : ''}${pnlPct}%)`;

    document.getElementById('hero-cash').textContent = `$${totalCash.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
    document.getElementById('hero-pos-val').textContent = `$${totalPos.toLocaleString(undefined, {minimumFractionDigits: 2})}`;
    document.getElementById('hero-cost-roi').textContent = `$${strat.total_llm_cost_usd?.toFixed(4) || '0.0000'} / ${strat.model_roi || '0'}x`;

    // Allocation bar
    const cashPct = Math.max(0, Math.min(100, Math.round((totalCash / Math.max(totalEq, 1)) * 100)));
    const posPct = 100 - cashPct;
    document.getElementById('alloc-bar').innerHTML = `
      <div class="alloc-seg" style="width: ${cashPct}%; background: #0284c7;" title="现金: ${cashPct}%"></div>
      <div class="alloc-seg" style="width: ${posPct}%; background: #10b981;" title="持仓资产: ${posPct}%"></div>
    `;
    document.getElementById('alloc-summary').textContent = `现金 ${cashPct}% · 证券/代币 ${posPct}%`;
    document.getElementById('alloc-legend').innerHTML = `
      <div class="legend-item"><div class="legend-dot" style="background:#0284c7"></div>可用现金 (${cashPct}%)</div>
      <div class="legend-item"><div class="legend-dot" style="background:#10b981"></div>股票/加密持仓 (${posPct}%)</div>
    `;

    // 2. Active Strategy Brain
    document.getElementById('strat-name').textContent = strat.name || 'baseline';
    document.getElementById('strat-gen-badge').textContent = `Gen #${strat.generation || 0}`;
    document.getElementById('strat-desc').textContent = strat.description || '自适应双周期 EMA 顺势跟踪 + RSI 超卖抄底 + 动态 ATR 移动止损';

    const params = strat.params || {};
    document.getElementById('params-pills').innerHTML = Object.entries(params).map(([k, v]) => `
      <div class="param-pill">${k}: <b>${v}</b></div>
    `).join('');

    // Decision Blotter
    const decisionsHtml = watchList.slice(0, 4).map(w => {
      const biasText = w.trend_bias > 0 ? '顺势偏多' : (w.trend_bias < 0 ? '防御偏空' : '中性观望');
      return `
        <div class="decision-item">
          <span><b>${w.symbol}</b>: ${biasText} (风险缩放 ${w.risk_scale}x)</span>
          <span class="mono" style="color:var(--cyan)">目标仓位: ${Math.round(w.risk_scale * 25)}%</span>
        </div>
      `;
    }).join('');
    document.getElementById('decisions-list').innerHTML = decisionsHtml || '<div class="decision-item">运行平稳，监控中...</div>';

    // 3. Watchlist Dynamics
    document.getElementById('watchlist-tbody').innerHTML = watchList.map(w => {
      const chg = w.change_pct || 0;
      const isPos = chg >= 0;
      const badgeCls = w.sentiment === 'BULLISH' ? 'pos' : (w.sentiment === 'BEARISH' ? 'neg' : '');
      return `
        <tr>
          <td class="mono"><b>${w.symbol}</b></td>
          <td><span class="mode-btn active" style="font-size:10px; padding:2px 6px;">${w.routed_broker}</span></td>
          <td class="mono">$${w.price?.toLocaleString()}</td>
          <td class="mono ${isPos ? 'pos' : 'neg'}"><b>${isPos ? '+' : ''}${chg}%</b></td>
          <td><span class="${badgeCls}">${w.sentiment}</span></td>
          <td class="mono">${Math.round(w.risk_scale * 25)}%</td>
        </tr>
      `;
    }).join('');

    // 4. Positions Table
    document.getElementById('pos-count').textContent = `${posList.length} 项持仓`;
    if (posList.length === 0) {
      document.getElementById('positions-tbody').innerHTML = `
        <tr><td colspan="9" style="text-align:center; color:var(--text-muted); padding:20px;">当前空仓。点击上方“立即调仓”或“演化新策略”即可自动建仓</td></tr>
      `;
    } else {
      document.getElementById('positions-tbody').innerHTML = posList.map(p => {
        const isPos = p.unrealized_pnl_usd >= 0;
        return `
          <tr>
            <td class="mono"><b>${p.symbol}</b></td>
            <td><span class="mode-btn active" style="font-size:10px; padding:2px 6px;">${p.broker}</span></td>
            <td class="mono">${p.qty}</td>
            <td class="mono">$${p.avg_price?.toLocaleString()}</td>
            <td class="mono"><b>$${p.last_price?.toLocaleString()}</b></td>
            <td class="mono" style="font-weight:700;">$${p.market_value_usd?.toLocaleString()}</td>
            <td class="mono">${p.weight_pct}%</td>
            <td class="mono ${isPos ? 'pos' : 'neg'}"><b>${isPos ? '+' : ''}$${p.unrealized_pnl_usd} (${isPos ? '+' : ''}${p.unrealized_pnl_pct}%)</b></td>
            <td class="mono" style="color:var(--cyan)">${p.target_weight_pct}%</td>
          </tr>
        `;
      }).join('');
    }

    // 5. Arena Comparison
    if (arena && arena.agent_a && arena.agent_b) {
      const a = arena.agent_a;
      const b = arena.agent_b;
      document.getElementById('arena-verdict-text').textContent = arena.verdict_reason || arena.verdict;
      document.getElementById('ar-score-a').textContent = a.score;
      document.getElementById('ar-sharpe-a').textContent = a.sharpe;
      document.getElementById('ar-ret-a').textContent = `+${a.net_return_pct}%`;
      document.getElementById('ar-dd-a').textContent = `-${a.max_drawdown_pct}%`;
      document.getElementById('ar-cost-a').textContent = `$${a.llm_cost_usd?.toFixed(5)}`;
      document.getElementById('ar-profit-a').textContent = `$${a.profit_after_llm_usd?.toLocaleString()}`;
      document.getElementById('ar-roi-a').textContent = `${a.model_roi}x`;

      document.getElementById('ar-score-b').textContent = b.score;
      document.getElementById('ar-sharpe-b').textContent = b.sharpe;
      document.getElementById('ar-ret-b').textContent = `+${b.net_return_pct}%`;
      document.getElementById('ar-dd-b').textContent = `-${b.max_drawdown_pct}%`;
      document.getElementById('ar-cost-b').textContent = `$${b.llm_cost_usd?.toFixed(5)}`;
      document.getElementById('ar-profit-b').textContent = `$${b.profit_after_llm_usd?.toLocaleString()}`;
      document.getElementById('ar-roi-b').textContent = `${b.model_roi}x`;
    }

    // 6. Ratchet Timeline
    const gens = (hist.generations || []).slice().reverse();
    document.getElementById('timeline-tbody').innerHTML = gens.map(g => {
      const isKeep = (g.status || '').includes('LOCKED') || (g.status || '').includes('BASELINE');
      return `
        <tr>
          <td class="mono"><b>#${g.generation}</b></td>
          <td><span class="mode-btn active" style="font-size:10px; color:${isKeep ? 'var(--emerald)' : 'var(--rose)'}">${g.status}</span></td>
          <td class="mono" style="font-weight:700; color:${isKeep ? 'var(--emerald)' : 'var(--text-secondary)'}">${g.ratchet_score}</td>
          <td class="mono">${g.score_delta > 0 ? '+' + g.score_delta : g.score_delta}</td>
          <td class="mono">${g.avg_sharpe}</td>
          <td class="mono">+${g.avg_return_pct}% / -${g.avg_max_drawdown_pct}%</td>
          <td class="mono">$${g.llm_cost_usd?.toFixed(5)}</td>
          <td style="color:#cbd5e1; max-width:320px;">${g.hypothesis || ''}</td>
        </tr>
      `;
    }).join('');

    // 7. Brokers
    const accounts = data.portfolio?.accounts || [];
    document.getElementById('broker-tbody').innerHTML = accounts.map(a => {
      const ok = (a.status || '').startsWith('connected');
      return `
        <tr>
          <td class="mono"><b>${a.broker}</b></td>
          <td><span class="mode-btn active" style="font-size:10px; color:${ok ? 'var(--emerald)' : 'var(--amber)'}">${a.status}</span></td>
          <td style="color:#94a3b8; font-size:12px;">${a.custody_location || ''}</td>
          <td class="mono" style="font-weight:600;">$${(a.equity_usd || 0).toLocaleString()}</td>
          <td class="mono">$${(a.cash_usd || 0).toLocaleString()}</td>
          <td class="mono" style="color:var(--text-muted)">$${(a.capital_cap_usd || 0).toLocaleString()}</td>
        </tr>
      `;
    }).join('');

    // 8. Code
    document.getElementById('code-viewer').textContent = data.strategy_code || '';
  } catch (err) {
    console.error('Refresh error:', err);
  }
}

async function triggerAction(endpoint, msg, btn) {
  if (btn) btn.classList.add('loading');
  showToast(msg, '⏳');
  try {
    await fetch(endpoint, { method: 'POST' });
    await refreshDashboard();
    showToast('操作执行成功并已更新看板！', '✓');
  } catch (err) {
    showToast('执行异常，请查看终端输出', '⚠');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function changeMode(mode) {
  showToast(`正在切换模式为: ${mode}...`, '⚙');
  await fetch('/api/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ratchet_mode: mode })
  });
  await refreshDashboard();
  showToast(`已切换为: ${mode.toUpperCase()}`, '✓');
}

window.addEventListener('load', () => {
  refreshDashboard();
  window.addEventListener('resize', () => {
    fetch('/api/state').then(r => r.json()).then(d => renderEquityChart(d.evaluation?.equity_curve || []));
  });
});
</script>
</body>
</html>
"""


class ATraderDashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            self._send_json(collect_dashboard_state())
            return
        raw_html = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw_html)))
        self.end_headers()
        self.wfile.write(raw_html)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body_bytes = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body = {}

        if parsed.path == "/api/evolve":
            res = run_code_ratchet_step(steps=int(body.get("steps", 1)))
            self._send_json(res)
        elif parsed.path == "/api/signal":
            res = run_signal_ratchet_update()
            self._send_json(res)
        elif parsed.path == "/api/trade":
            res = run_live_trading_cycle()
            self._send_json(res)
        elif parsed.path == "/api/backtest":
            res = evaluate_strategy()
            self._send_json(res)
        elif parsed.path == "/api/mode":
            mode = body.get("ratchet_mode", "hybrid")
            if mode in ("hybrid", "code_ratchet", "signal_ratchet"):
                save_config({"ratchet_mode": mode})
            self._send_json({"status": "ok", "ratchet_mode": mode})
        else:
            self._send_json({"error": "not_found"}, status=404)


def run_web_server(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = HTTPServer((host, port), ATraderDashboardHandler)
    print(f"🚀 ATrader Web Dashboard running at: http://{host}:{port}")
    print("   Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down ATrader Web Dashboard.")
        server.server_close()

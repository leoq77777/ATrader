"""
ATrader Interactive Single-Page Web Dashboard & REST Server
===========================================================
Ultra-modern quantitative terminal UI inspired by Linear & TradingView.
Zero-dependency HTTP server (`http.server`) providing:
  - Interactive Web UI at `http://127.0.0.1:8787`
  - JSON REST endpoints:
      GET  `/api/state`      -> Full dashboard state
      POST `/api/evolve`     -> Trigger Code Ratchet mutation & validation
      POST `/api/signal`     -> Update low-frequency regime signal & shadow trust
      POST `/api/trade`      -> Execute 0-token live/paper rebalance
      POST `/api/backtest`   -> Run walk-forward backtest
      POST `/api/mode`       -> Switch ratchet mode
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Any
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

    return {
        "config": {
            "llm_model": cfg.get("llm_model", "deepseek-chat"),
            "llm_base_url": cfg.get("llm_base_url", "https://api.deepseek.com"),
            "llm_key_configured": bool(
                cfg.get("llm_api_key") and cfg.get("llm_api_key") != "your_deepseek_api_key_here"
            ),
            "ratchet_mode": cfg.get("ratchet_mode", "hybrid"),
            "active_brokers": cfg.get("active_brokers", ["local_paper"]),
            "watchlist": cfg.get("watchlist", []),
        },
        "evaluation": evaluation,
        "ratchet_history": history,
        "signal_regimes": regimes,
        "portfolio": portfolio,
        "strategy_code": strategy_code,
    }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>ATrader · The Karpathy Ratchet Quantitative Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-base: #06090e;
    --bg-surface: #0c111a;
    --bg-card: rgba(15, 23, 38, 0.72);
    --bg-card-hover: rgba(22, 33, 54, 0.85);
    --border: rgba(255, 255, 255, 0.08);
    --border-bright: rgba(56, 189, 248, 0.28);
    --text-primary: #f8fafc;
    --text-secondary: #94a3b8;
    --text-tertiary: #64748b;
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
      radial-gradient(ellipse 80% 50% at 50% -20%, rgba(56, 189, 248, 0.08), transparent 70%),
      radial-gradient(ellipse 60% 40% at 90% 10%, rgba(168, 85, 247, 0.05), transparent 60%);
    color: var(--text-primary);
    line-height: 1.5;
    padding: 24px 32px 48px;
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }

  /* Top Navigation Bar */
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 16px 20px;
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    border: 1px solid var(--border);
    border-radius: 14px;
    margin-bottom: 22px;
    box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
  }
  .brand { display: flex; align-items: center; gap: 14px; }
  .logo-box {
    width: 38px;
    height: 38px;
    border-radius: 10px;
    background: linear-gradient(135deg, #0284c7, #38bdf8);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 20px;
    color: #fff;
    box-shadow: 0 0 20px var(--cyan-glow);
  }
  .brand-title {
    font-size: 19px;
    font-weight: 800;
    letter-spacing: -0.4px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .brand-sub {
    font-size: 12px;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .pulse-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--emerald);
    box-shadow: 0 0 10px var(--emerald);
    animation: pulse 2s infinite ease-in-out;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.85); }
  }

  /* Pills & Badges */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 9px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    font-family: var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.4px;
    background: rgba(56, 189, 248, 0.12);
    color: var(--cyan);
    border: 1px solid rgba(56, 189, 248, 0.28);
  }
  .badge.emerald { background: rgba(16, 185, 129, 0.12); color: var(--emerald); border-color: rgba(16, 185, 129, 0.28); }
  .badge.rose { background: rgba(244, 63, 94, 0.12); color: var(--rose); border-color: rgba(244, 63, 94, 0.28); }
  .badge.amber { background: rgba(245, 158, 11, 0.12); color: var(--amber); border-color: rgba(245, 158, 11, 0.28); }
  .badge.purple { background: rgba(168, 85, 247, 0.12); color: var(--purple); border-color: rgba(168, 85, 247, 0.28); }

  /* Toolbar Actions */
  .toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .mode-switch {
    display: flex;
    background: rgba(0, 0, 0, 0.35);
    padding: 3px;
    border-radius: 9px;
    border: 1px solid var(--border);
  }
  .mode-btn {
    background: transparent;
    border: none;
    color: var(--text-secondary);
    padding: 6px 12px;
    border-radius: 7px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.18s;
  }
  .mode-btn:hover { color: var(--text-primary); }
  .mode-btn.active {
    background: rgba(56, 189, 248, 0.16);
    color: var(--cyan);
    border: 1px solid rgba(56, 189, 248, 0.35);
    box-shadow: 0 0 12px rgba(56, 189, 248, 0.2);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    background: rgba(30, 41, 59, 0.7);
    color: var(--text-primary);
    border: 1px solid var(--border);
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.18s;
  }
  .btn:hover {
    border-color: rgba(255, 255, 255, 0.22);
    background: rgba(45, 62, 90, 0.85);
    transform: translateY(-1px);
  }
  .btn.primary {
    background: linear-gradient(135deg, #0284c7, #0ea5e9);
    border-color: #38bdf8;
    color: #fff;
    box-shadow: 0 0 16px rgba(14, 165, 233, 0.3);
  }
  .btn.primary:hover {
    background: linear-gradient(135deg, #0369a1, #0284c7);
    box-shadow: 0 0 22px rgba(14, 165, 233, 0.5);
  }
  .btn.success {
    background: linear-gradient(135deg, #059669, #10b981);
    border-color: #34d399;
    color: #fff;
    box-shadow: 0 0 16px rgba(16, 185, 129, 0.3);
  }
  .btn.success:hover {
    background: linear-gradient(135deg, #047857, #059669);
    box-shadow: 0 0 22px rgba(16, 185, 129, 0.5);
  }
  .btn.loading { opacity: 0.65; pointer-events: none; }

  /* KPI Grid */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 22px;
  }
  @media (max-width: 1100px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 600px) { .kpi-grid { grid-template-columns: 1fr; } }

  .kpi-card {
    background: var(--bg-card);
    backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 18px 20px;
    transition: all 0.22s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    overflow: hidden;
  }
  .kpi-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, transparent, var(--border-bright), transparent);
    opacity: 0;
    transition: opacity 0.2s;
  }
  .kpi-card:hover {
    border-color: var(--border-bright);
    background: var(--bg-card-hover);
    transform: translateY(-2px);
    box-shadow: 0 12px 28px -8px rgba(0, 0, 0, 0.6);
  }
  .kpi-card:hover::before { opacity: 1; }

  .kpi-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }
  .kpi-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--text-secondary);
  }
  .kpi-value {
    font-family: var(--font-mono);
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 4px;
    display: flex;
    align-items: baseline;
    gap: 6px;
  }
  .kpi-sub {
    font-size: 12px;
    color: var(--text-tertiary);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .kpi-sub span.pos { color: var(--emerald); font-weight: 600; }
  .kpi-sub span.neg { color: var(--rose); font-weight: 600; }

  /* Main Workspace Layout */
  .workspace-layout {
    display: grid;
    grid-template-columns: 1.45fr 1fr;
    gap: 20px;
    margin-bottom: 22px;
  }
  @media (max-width: 1080px) { .workspace-layout { grid-template-columns: 1fr; } }

  .panel {
    background: var(--bg-card);
    backdrop-filter: blur(12px);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 20px;
    display: flex;
    flex-direction: column;
  }
  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .panel-title {
    font-size: 14px;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 9px;
    letter-spacing: -0.2px;
  }
  .panel-title-icon {
    font-size: 16px;
  }

  /* SVG Chart Container */
  .chart-box {
    position: relative;
    width: 100%;
    height: 240px;
    background: rgba(6, 9, 14, 0.45);
    border-radius: 10px;
    border: 1px solid rgba(255, 255, 255, 0.04);
    overflow: hidden;
  }
  .chart-tooltip {
    position: absolute;
    display: none;
    padding: 6px 10px;
    background: rgba(15, 23, 42, 0.95);
    border: 1px solid var(--cyan);
    border-radius: 6px;
    font-family: var(--font-mono);
    font-size: 11px;
    color: #fff;
    pointer-events: none;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
  }

  /* Modern Tabbed Container */
  .tabs-nav {
    display: flex;
    gap: 4px;
    background: rgba(0, 0, 0, 0.35);
    padding: 4px;
    border-radius: 10px;
    margin-bottom: 16px;
    border: 1px solid var(--border);
  }
  .tab-item {
    flex: 1;
    text-align: center;
    padding: 8px 12px;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.16s;
    user-select: none;
  }
  .tab-item:hover { color: var(--text-primary); }
  .tab-item.active {
    background: rgba(56, 189, 248, 0.14);
    color: var(--cyan);
    font-weight: 700;
    border: 1px solid rgba(56, 189, 248, 0.28);
  }

  /* Financial Tables */
  .table-wrap {
    overflow-x: auto;
    max-height: 380px;
  }
  table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 12.5px;
  }
  th {
    position: sticky;
    top: 0;
    background: rgba(12, 17, 26, 0.96);
    color: var(--text-tertiary);
    font-weight: 600;
    text-align: left;
    padding: 10px 12px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
    backdrop-filter: blur(8px);
    z-index: 2;
  }
  td {
    padding: 11px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    color: var(--text-primary);
  }
  tr:hover td {
    background: rgba(56, 189, 248, 0.04);
  }
  .mono { font-family: var(--font-mono); }

  /* Code Viewer */
  .code-viewer-wrap {
    position: relative;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid var(--border);
  }
  .code-topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 14px;
    background: #080c14;
    border-bottom: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text-tertiary);
  }
  pre {
    background: #05080e;
    padding: 14px 16px;
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.6;
    color: #e2e8f0;
    overflow-x: auto;
    max-height: 380px;
  }

  /* Toast Notification */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 28px;
    background: rgba(15, 23, 42, 0.95);
    border: 1px solid var(--cyan);
    color: #fff;
    padding: 12px 20px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 10px;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.6), 0 0 20px var(--cyan-glow);
    transform: translateY(100px);
    opacity: 0;
    transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    z-index: 999;
  }
  #toast.show {
    transform: translateY(0);
    opacity: 1;
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="logo-box">⚡</div>
    <div>
      <div class="brand-title">
        ATrader
        <span class="badge" id="mode-badge">HYBRID RATCHET</span>
      </div>
      <div class="brand-sub">
        <div class="pulse-dot"></div>
        <span>The Karpathy Ratchet Quantitative Terminal</span>
        <span>·</span>
        <span id="model-status" style="font-family:var(--font-mono); color:var(--cyan)">Connecting to DeepSeek...</span>
      </div>
    </div>
  </div>

  <div class="toolbar">
    <div class="mode-switch">
      <button class="mode-btn" id="m-hybrid" onclick="changeMode('hybrid')">Hybrid (Dual-Ratchet)</button>
      <button class="mode-btn" id="m-code" onclick="changeMode('code_ratchet')">Code Only (0-Token)</button>
      <button class="mode-btn" id="m-signal" onclick="changeMode('signal_ratchet')">Signal Only</button>
    </div>

    <button class="btn primary" id="btn-evolve" onclick="triggerAction('/api/evolve', '⚡ 正在执行策略代码棘轮演化 (Evolve)...', this)">
      <span>⚡</span> Evolve Strategy
    </button>
    <button class="btn" id="btn-signal" onclick="triggerAction('/api/signal', '📡 正在更新低频市场状态棘轮...', this)">
      <span>📡</span> Update Signal
    </button>
    <button class="btn success" id="btn-trade" onclick="triggerAction('/api/trade', '🚀 正在执行 0-Token 实盘/模拟调仓...', this)">
      <span>🚀</span> Trade Step
    </button>
    <button class="btn" id="btn-backtest" onclick="triggerAction('/api/backtest', '🔄 正在运行样本外回测验证...', this)">
      <span>🔄</span> Backtest
    </button>
  </div>
</header>

<!-- Top KPI Cards -->
<section class="kpi-grid">
  <div class="kpi-card">
    <div class="kpi-header">
      <span class="kpi-label">Current Ratchet Score</span>
      <span class="badge emerald" id="kpi-gen">Gen 0</span>
    </div>
    <div class="kpi-value" style="color:var(--emerald)" id="kpi-score">--</div>
    <div class="kpi-sub" id="kpi-strategy-name">Locked: baseline</div>
  </div>

  <div class="kpi-card">
    <div class="kpi-header">
      <span class="kpi-label">Walk-Forward Sharpe</span>
      <span class="badge" id="kpi-val-badge">30% Out-of-Sample</span>
    </div>
    <div class="kpi-value" style="color:var(--cyan)" id="kpi-sharpe">--</div>
    <div class="kpi-sub" id="kpi-ret-dd">
      Return: <span class="pos">+0.0%</span> · MaxDD: <span class="neg">-0.0%</span>
    </div>
  </div>

  <div class="kpi-card">
    <div class="kpi-header">
      <span class="kpi-label">Compute ROI (Profit / Token Cost)</span>
      <span class="badge purple">Karpathy Metric</span>
    </div>
    <div class="kpi-value" id="kpi-roi">--</div>
    <div class="kpi-sub" id="kpi-cost-sub">
      Total LLM Spend: <span style="font-family:var(--font-mono); color:var(--text-primary)">$0.00000</span>
    </div>
  </div>

  <div class="kpi-card">
    <div class="kpi-header">
      <span class="kpi-label">Unified Master Ledger Equity</span>
      <span class="badge amber">Multi-Broker</span>
    </div>
    <div class="kpi-value" id="kpi-equity">--</div>
    <div class="kpi-sub" id="kpi-cash-pnl">
      Cash: $10,000 · PnL: <span class="pos">+$0.00</span>
    </div>
  </div>
</section>

<!-- Middle Layout: Interactive Chart + Low-Freq Signals -->
<div class="workspace-layout">
  <div class="panel">
    <div class="panel-header">
      <div class="panel-title">
        <span class="panel-title-icon">📈</span>
        <span>Walk-Forward Equity Curve (Net of Fees & Slippage)</span>
      </div>
      <div style="display:flex; gap:8px;">
        <span class="badge emerald">Trading Fees: 8 bps</span>
        <span class="badge amber">70% Train / 30% Val</span>
      </div>
    </div>
    <div class="chart-box" id="chart-box">
      <div class="chart-tooltip" id="chart-tooltip"></div>
      <svg id="equity-svg" width="100%" height="100%"></svg>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <div class="panel-title">
        <span class="panel-title-icon">📡</span>
        <span>Low-Frequency Signal Ratchet & Shadow Trust</span>
      </div>
      <span class="badge purple">Auto-Weighted</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Routed</th>
            <th>Price</th>
            <th>Bias</th>
            <th>Trust Weight</th>
            <th>Regime Thesis</th>
          </tr>
        </thead>
        <tbody id="signal-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Bottom Layout: Tabbed Deep Dives (Evolution Log, Multi-Broker Custody, Code Viewer) -->
<div class="panel">
  <div class="tabs-nav">
    <div class="tab-item active" onclick="switchTab('tab-evolution', this)">🧬 Karpathy Ratchet Evolution Timeline</div>
    <div class="tab-item" onclick="switchTab('tab-brokers', this)">🏦 Multi-Broker Capital Custody & Master Ledger</div>
    <div class="tab-item" onclick="switchTab('tab-code', this)">💻 strategy.py (Active Ratchet Mutation)</div>
  </div>

  <!-- Tab 1: Evolution Log -->
  <div id="tab-evolution" class="tab-pane">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Gen</th>
            <th>Decision Status</th>
            <th>Ratchet Score</th>
            <th>Score Delta</th>
            <th>Sharpe</th>
            <th>Return / MaxDD</th>
            <th>LLM Cost</th>
            <th>Evolution Hypothesis & Bottleneck Addressed</th>
          </tr>
        </thead>
        <tbody id="evolution-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Tab 2: Multi-Broker Custody -->
  <div id="tab-brokers" class="tab-pane" style="display:none;">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Broker API</th>
            <th>Connection Status</th>
            <th>Where Real Funds Live (Custody Institution)</th>
            <th>Equity (USD)</th>
            <th>Cash (USD)</th>
            <th>Risk Cap</th>
          </tr>
        </thead>
        <tbody id="broker-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- Tab 3: Code Viewer -->
  <div id="tab-code" class="tab-pane" style="display:none;">
    <div class="code-viewer-wrap">
      <div class="code-topbar">
        <span>strategy.py · The Single Mutable File</span>
        <button class="btn" style="padding:3px 8px; font-size:11px;" onclick="copyCode()">Copy Code</button>
      </div>
      <pre><code id="code-viewer">Loading strategy.py...</code></pre>
    </div>
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
  setTimeout(() => { t.classList.remove('show'); }, 3400);
}

function switchTab(tabId, el) {
  document.querySelectorAll('.tab-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-pane').forEach(p => p.style.display = 'none');
  el.classList.add('active');
  document.getElementById(tabId).style.display = 'block';
}

function copyCode() {
  const code = document.getElementById('code-viewer').innerText;
  navigator.clipboard.writeText(code);
  showToast('strategy.py 代码已复制到剪贴板！');
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
  const h = box.clientHeight || 240;
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
        <stop offset="0%" stop-color="#38bdf8" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0"/>
      </linearGradient>
    </defs>
    <!-- Background Grid Lines -->
    <line x1="${padL}" y1="${padT + plotH * 0.25}" x2="${w - padR}" y2="${padT + plotH * 0.25}" stroke="rgba(255,255,255,0.04)" stroke-dasharray="3,3" />
    <line x1="${padL}" y1="${padT + plotH * 0.5}" x2="${w - padR}" y2="${padT + plotH * 0.5}" stroke="rgba(255,255,255,0.04)" stroke-dasharray="3,3" />
    <line x1="${padL}" y1="${padT + plotH * 0.75}" x2="${w - padR}" y2="${padT + plotH * 0.75}" stroke="rgba(255,255,255,0.04)" stroke-dasharray="3,3" />

    <!-- 70/30 Train/Val Boundary -->
    <line x1="${splitX}" y1="${padT}" x2="${splitX}" y2="${padT + plotH}" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4,4" />
    <text x="${splitX + 8}" y="${padT + 14}" fill="#f59e0b" font-size="11" font-family="JetBrains Mono, monospace" font-weight="600">30% Out-of-Sample Val →</text>
    <text x="${padL + 6}" y="${padT + 14}" fill="#94a3b8" font-size="11" font-family="JetBrains Mono, monospace">← 70% In-Sample Train</text>

    <!-- Area & Line -->
    <polygon points="${areaPts}" fill="url(#curveGrad)" />
    <polyline fill="none" stroke="#38bdf8" stroke-width="2.6" points="${polylinePts}" />

    <!-- End Dot -->
    <circle cx="${points[points.length-1].x}" cy="${points[points.length-1].y}" r="4.5" fill="#38bdf8" stroke="#fff" stroke-width="2" />
  `;

  // Hover interaction
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
    const ev = data.evaluation || {};
    const hist = data.ratchet_history || {};
    const port = (data.portfolio || {}).virtual_master_ledger || {};
    const cfg = data.config || {};

    // Mode Buttons
    const curMode = cfg.ratchet_mode || 'hybrid';
    document.getElementById('mode-badge').textContent = curMode.toUpperCase();
    ['hybrid', 'code', 'signal'].forEach(m => {
      const btn = document.getElementById(`m-${m}`);
      if (btn) btn.classList.toggle('active', curMode.includes(m));
    });

    // Model Status
    const modelText = cfg.llm_key_configured
      ? `Connected: ${cfg.llm_model} · DeepSeek API`
      : `Heuristic Mode (${cfg.llm_model}) · Set Key in .env`;
    document.getElementById('model-status').textContent = modelText;

    // Top KPIs
    document.getElementById('kpi-score').textContent = ev.ratchet_score ?? '--';
    document.getElementById('kpi-gen').textContent = `Gen ${hist.current_generation || 0}`;
    document.getElementById('kpi-strategy-name').textContent = `Locked: ${ev.strategy_name || '--'}`;

    document.getElementById('kpi-sharpe').textContent = ev.avg_sharpe ? `${ev.avg_sharpe}` : '--';
    const ret = ev.avg_return_pct ?? 0;
    const dd = ev.avg_max_drawdown_pct ?? 0;
    document.getElementById('kpi-ret-dd').innerHTML = `
      Return: <span class="${ret >= 0 ? 'pos' : 'neg'}">${ret >= 0 ? '+' : ''}${ret}%</span> ·
      MaxDD: <span class="neg">-${dd}%</span>
    `;

    const totalLlmCost = hist.total_llm_cost_usd || 0;
    const profit = ev.net_profit_usd || 0;
    const roiRatio = totalLlmCost > 0 ? (profit / totalLlmCost).toFixed(0) : '∞';
    document.getElementById('kpi-roi').textContent = `${roiRatio}x ROI`;
    document.getElementById('kpi-cost-sub').innerHTML = `
      Total LLM Spend: <span style="font-family:var(--font-mono); color:var(--text-primary)">$${totalLlmCost.toFixed(5)}</span>
    `;

    document.getElementById('kpi-equity').textContent = `$${(port.total_equity_usd || 0).toLocaleString()}`;
    const pnl = port.total_net_pnl_usd || 0;
    document.getElementById('kpi-cash-pnl').innerHTML = `
      Cash: $${(port.total_cash_usd || 0).toLocaleString()} ·
      PnL: <span class="${pnl >= 0 ? 'pos' : 'neg'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>
    `;

    // Evolution Table
    const gens = (hist.generations || []).slice().reverse();
    document.getElementById('evolution-tbody').innerHTML = gens.map(g => {
      const isKeep = (g.status || '').includes('LOCKED') || (g.status || '').includes('BASELINE');
      const badgeCls = isKeep ? 'emerald' : 'rose';
      const delta = g.score_delta || 0;
      const deltaHtml = delta > 0
        ? `<span style="color:var(--emerald); font-weight:700;">+${delta}</span>`
        : (delta < 0 ? `<span style="color:var(--rose); font-weight:700;">${delta}</span>` : `<span style="color:var(--text-tertiary)">0.0</span>`);

      return `<tr>
        <td class="mono"><b>#${g.generation}</b></td>
        <td><span class="badge ${badgeCls}">${g.status}</span></td>
        <td class="mono" style="font-weight:700; color:${isKeep ? 'var(--emerald)' : 'var(--text-secondary)'}">${g.ratchet_score}</td>
        <td class="mono">${deltaHtml}</td>
        <td class="mono">${g.avg_sharpe}</td>
        <td class="mono">+${g.avg_return_pct}% / -${g.avg_max_drawdown_pct}%</td>
        <td class="mono">$${(g.llm_cost_usd || 0).toFixed(5)}</td>
        <td style="color:#cbd5e1; max-width:320px;">${g.hypothesis || ''}</td>
      </tr>`;
    }).join('');

    // Brokers Table
    const accounts = (data.portfolio || {}).accounts || [];
    document.getElementById('broker-tbody').innerHTML = accounts.map(a => {
      const ok = (a.status || '').startsWith('connected');
      return `<tr>
        <td class="mono"><b>${a.broker}</b></td>
        <td><span class="badge ${ok ? 'emerald' : 'amber'}">${a.status}</span></td>
        <td style="color:#94a3b8; font-size:12px;">${a.custody_location || ''}</td>
        <td class="mono" style="font-weight:600;">$${(a.equity_usd || 0).toLocaleString()}</td>
        <td class="mono">$${(a.cash_usd || 0).toLocaleString()}</td>
        <td class="mono" style="color:var(--text-tertiary)">$${(a.capital_cap_usd || 0).toLocaleString()}</td>
      </tr>`;
    }).join('');

    // Signals Table
    const routing = (data.portfolio || {}).symbol_routing || {};
    const regimes = Object.values(data.signal_regimes || {});
    document.getElementById('signal-tbody').innerHTML = regimes.map(r => {
      const snap = r.snapshot || {};
      const bias = r.trend_bias || 0;
      const biasColor = bias > 0 ? 'var(--emerald)' : (bias < 0 ? 'var(--rose)' : 'var(--text-secondary)');
      const trustPct = Math.round((r.trust_weight || 0) * 100);

      return `<tr>
        <td class="mono"><b>${r.symbol}</b></td>
        <td><span class="badge" style="font-size:10px;">${routing[r.symbol] || 'local_paper'}</span></td>
        <td class="mono">$${(snap.price || 0).toLocaleString()}</td>
        <td class="mono" style="color:${biasColor}; font-weight:700;">${bias > 0 ? '+' : ''}${bias}</td>
        <td>
          <div style="display:flex; align-items:center; gap:6px;">
            <div style="width:42px; height:6px; background:rgba(255,255,255,0.08); border-radius:3px; overflow:hidden;">
              <div style="width:${trustPct}%; height:100%; background:var(--purple);"></div>
            </div>
            <span class="mono" style="font-size:11px; font-weight:600;">${trustPct}%</span>
          </div>
        </td>
        <td style="color:#94a3b8; font-size:11.5px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${r.thesis || ''}">${r.thesis || ''}</td>
      </tr>`;
    }).join('');

    // Strategy Code
    document.getElementById('code-viewer').textContent = data.strategy_code || '';

    // Equity Curve
    renderEquityChart(ev.equity_curve || []);
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
        return  # Keep terminal output clean

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

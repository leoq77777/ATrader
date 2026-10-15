"""
ATrader Interactive Single-Page Web Dashboard & REST Server
===========================================================
Zero-dependency HTTP server (`http.server`) providing:
  - Interactive Web UI at `http://127.0.0.1:8787`
  - JSON REST endpoints:
      GET  `/api/state`      -> Full dashboard state (evaluation, ratchet history, signal regimes, portfolio, code)
      POST `/api/evolve`     -> Trigger 1 step of Code Ratchet (`strategy.py` mutation + evaluation + keep/revert)
      POST `/api/signal`     -> Trigger Low-Frequency Signal Ratchet update
      POST `/api/trade`      -> Trigger 1 Zero-Token Live/Paper execution cycle across active brokers
      POST `/api/backtest`   -> Run walk-forward backtest on current `strategy.py`
      POST `/api/mode`       -> Switch `ratchet_mode` (`hybrid` | `code_ratchet` | `signal_ratchet`)
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
<title>ATrader — The Karpathy Ratchet Agent Trader</title>
<style>
  :root {
    --bg: #0b0f17;
    --card: #131a27;
    --border: #232f46;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --green: #10b981;
    --red: #f43f5e;
    --amber: #f59e0b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    padding: 20px 28px;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 14px;
    padding-bottom: 18px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 20px;
  }
  .brand h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.3px; }
  .brand p { color: var(--muted); font-size: 13px; }
  .badge {
    display: inline-block;
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    background: rgba(56, 189, 248, 0.14);
    color: var(--accent);
    border: 1px solid rgba(56, 189, 248, 0.3);
  }
  .badge.green { background: rgba(16, 185, 129, 0.15); color: var(--green); border-color: rgba(16, 185, 129, 0.3); }
  .badge.red { background: rgba(244, 63, 94, 0.15); color: var(--red); border-color: rgba(244, 63, 94, 0.3); }
  .badge.amber { background: rgba(245, 158, 11, 0.15); color: var(--amber); border-color: rgba(245, 158, 11, 0.3); }
  .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  button, select {
    background: #1e293b;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 8px 14px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }
  button:hover { border-color: var(--accent); background: #26354d; }
  button.primary { background: #0284c7; border-color: #38bdf8; color: #fff; }
  button.primary:hover { background: #0369a1; }
  button.success { background: #059669; border-color: #10b981; color: #fff; }
  button.success:hover { background: #047857; }
  .kpis {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px;
  }
  .kpi-title { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .kpi-val { font-size: 24px; font-weight: 700; margin: 4px 0; }
  .kpi-sub { font-size: 12px; color: var(--muted); }
  .grid-2 {
    display: grid;
    grid-template-columns: 1.35fr 1fr;
    gap: 16px;
    margin-bottom: 18px;
  }
  @media (max-width: 1024px) { .grid-2 { grid-template-columns: 1fr; } }
  h2 { font-size: 15px; font-weight: 600; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 12px; }
  pre {
    background: #080b11;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    font-size: 12px;
    overflow-x: auto;
    max-height: 360px;
    color: #cbd5e1;
  }
  #toast {
    position: fixed;
    bottom: 20px;
    right: 24px;
    background: #0284c7;
    color: #fff;
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    display: none;
    box-shadow: 0 8px 20px rgba(0,0,0,0.4);
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>ATrader <span class="badge" id="mode-badge">HYBRID RATCHET</span></h1>
    <p>极简闭环棘轮量化架构 (The Karpathy Ratchet Model) · DeepSeek Cost-Aware Engine</p>
  </div>
  <div class="toolbar">
    <select id="mode-select" onchange="changeMode(this.value)">
      <option value="hybrid">Mode: Hybrid (Code + Signal Ratchet)</option>
      <option value="code_ratchet">Mode: Code Ratchet Only (0-Token Live)</option>
      <option value="signal_ratchet">Mode: Low-Freq Signal Ratchet</option>
    </select>
    <button class="primary" onclick="triggerAction('/api/evolve', '正在执行策略代码棘轮演化 (Evolve)...')">⚡ Evolve Strategy (Code Ratchet)</button>
    <button onclick="triggerAction('/api/signal', '正在更新低频市场状态棘轮 (Signal Ratchet)...')">📡 Update Regime Signal</button>
    <button class="success" onclick="triggerAction('/api/trade', '正在执行 0-Token 实盘/模拟盘调仓...')">🚀 Run Trade Step (0-Token)</button>
    <button onclick="triggerAction('/api/backtest', '正在运行样本外回测验证...')">🔄 Backtest</button>
  </div>
</header>

<section class="kpis">
  <div class="card">
    <div class="kpi-title">Current Ratchet Score (Locked)</div>
    <div class="kpi-val" id="kpi-score">--</div>
    <div class="kpi-sub" id="kpi-strat">--</div>
  </div>
  <div class="card">
    <div class="kpi-title">Walk-Forward Performance</div>
    <div class="kpi-val" id="kpi-sharpe">--</div>
    <div class="kpi-sub" id="kpi-ret-dd">--</div>
  </div>
  <div class="card">
    <div class="kpi-title">Model Cost vs. Strategy Profit</div>
    <div class="kpi-val" id="kpi-cost">--</div>
    <div class="kpi-sub" id="kpi-llm-status">--</div>
  </div>
  <div class="card">
    <div class="kpi-title">Unified Master Ledger Equity</div>
    <div class="kpi-val" id="kpi-equity">--</div>
    <div class="kpi-sub" id="kpi-pnl">--</div>
  </div>
</section>

<div class="grid-2">
  <div class="card">
    <h2>
      <span>📈 Karpathy Ratchet Evolution History (Keep If Better, Revert If Worse)</span>
      <span class="badge" id="gen-count">Gen 0</span>
    </h2>
    <table>
      <thead>
        <tr>
          <th>Gen</th>
          <th>Status</th>
          <th>Ratchet Score</th>
          <th>Sharpe</th>
          <th>Return / MaxDD</th>
          <th>LLM Cost</th>
          <th>Hypothesis</th>
        </tr>
      </thead>
      <tbody id="ratchet-tbody"></tbody>
    </table>
  </div>

  <div class="card">
    <h2>
      <span>📊 Walk-Forward Equity Curve (Net of Fees & Slippage)</span>
      <span class="badge green">Out-of-Sample Verified</span>
    </h2>
    <div id="chart-container" style="height:240px; width:100%;"></div>
  </div>
</div>

<div class="grid-2">
  <div class="card">
    <h2>
      <span>🏦 Multi-Broker Capital Custody & Virtual Master Ledger</span>
      <span class="badge amber">Isolated Custody + Unified Risk Cap</span>
    </h2>
    <table>
      <thead>
        <tr>
          <th>Broker API</th>
          <th>Status</th>
          <th>Where Real Funds Live (Custody)</th>
          <th>Equity (USD)</th>
          <th>Cash (USD)</th>
          <th>Risk Cap</th>
        </tr>
      </thead>
      <tbody id="broker-tbody"></tbody>
    </table>
  </div>

  <div class="card">
    <h2>
      <span>📡 Low-Frequency Signal Ratchet & Shadow Trust Weight</span>
      <span class="badge">Auto-Weighted by Shadow Win Rate</span>
    </h2>
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Routed Broker</th>
          <th>Price</th>
          <th>Trend Bias</th>
          <th>Risk Scale</th>
          <th>Trust Weight</th>
          <th>Regime Thesis</th>
        </tr>
      </thead>
      <tbody id="signal-tbody"></tbody>
    </table>
  </div>
</div>

<div class="card">
  <h2>
    <span>🧬 Active Ratchet-Locked `strategy.py` (Single Mutable File)</span>
    <span class="badge">Zero Runtime Token Execution</span>
  </h2>
  <pre id="code-viewer">Loading strategy.py...</pre>
</div>

<div id="toast"></div>

<script>
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3200);
}

function renderChart(curve) {
  const container = document.getElementById('chart-container');
  if (!curve || curve.length < 2) {
    container.innerHTML = '<p style="color:#94a3b8">No curve data</p>';
    return;
  }
  const w = container.clientWidth || 500;
  const h = 230;
  const minV = Math.min(...curve) * 0.995;
  const maxV = Math.max(...curve) * 1.005;
  const pts = curve.map((v, i) => {
    const x = (i / (curve.length - 1)) * (w - 20) + 10;
    const y = h - 20 - ((v - minV) / Math.max(maxV - minV, 1)) * (h - 40);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const splitX = 0.7 * (w - 20) + 10;
  container.innerHTML = `
    <svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}">
      <line x1="${splitX}" y1="10" x2="${splitX}" y2="${h-20}" stroke="#f59e0b" stroke-dasharray="4,4" />
      <text x="${splitX + 6}" y="24" fill="#f59e0b" font-size="11">30% Out-of-Sample Validation</text>
      <polyline fill="none" stroke="#38bdf8" stroke-width="2.5" points="${pts}" />
    </svg>
  `;
}

async function refreshState() {
  const res = await fetch('/api/state');
  const data = await res.json();
  const ev = data.evaluation || {};
  const hist = data.ratchet_history || {};
  const port = (data.portfolio || {}).virtual_master_ledger || {};
  const cfg = data.config || {};

  document.getElementById('mode-badge').textContent = (cfg.ratchet_mode || 'hybrid').toUpperCase();
  document.getElementById('mode-select').value = cfg.ratchet_mode || 'hybrid';

  document.getElementById('kpi-score').textContent = ev.ratchet_score ?? '--';
  document.getElementById('kpi-strat').textContent = `Active: ${ev.strategy_name || '--'}`;
  document.getElementById('kpi-sharpe').textContent = `Sharpe ${ev.avg_sharpe ?? '--'}`;
  document.getElementById('kpi-ret-dd').textContent = `Net Return: +${ev.avg_return_pct ?? 0}% | MaxDD: ${ev.avg_max_drawdown_pct ?? 0}%`;

  const totalCost = hist.total_llm_cost_usd || 0;
  document.getElementById('kpi-cost').textContent = `$${totalCost.toFixed(4)} LLM Cost`;
  document.getElementById('kpi-llm-status').textContent = cfg.llm_key_configured
    ? `Model: ${cfg.llm_model} (Connected)`
    : `Model: ${cfg.llm_model} (Local Heuristic Fallback - Set Key in .env)`;

  document.getElementById('kpi-equity').textContent = `$${(port.total_equity_usd || 0).toLocaleString()}`;
  const pnl = port.total_net_pnl_usd || 0;
  document.getElementById('kpi-pnl').textContent = `Cash: $${(port.total_cash_usd || 0).toLocaleString()} | Net PnL: ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;

  document.getElementById('gen-count').textContent = `Gen ${hist.current_generation || 0}`;

  // Ratchet Table
  const gens = (hist.generations || []).slice().reverse();
  document.getElementById('ratchet-tbody').innerHTML = gens.map(g => {
    const isKeep = (g.status || '').includes('LOCKED') || (g.status || '').includes('BASELINE');
    const badgeCls = isKeep ? 'green' : 'red';
    return `<tr>
      <td>#${g.generation}</td>
      <td><span class="badge ${badgeCls}">${g.status}</span></td>
      <td><b>${g.ratchet_score}</b></td>
      <td>${g.avg_sharpe}</td>
      <td>${g.avg_return_pct}% / -${g.avg_max_drawdown_pct}%</td>
      <td>$${(g.llm_cost_usd || 0).toFixed(5)}</td>
      <td style="color:#cbd5e1; max-width:290px;">${g.hypothesis || ''}</td>
    </tr>`;
  }).join('');

  // Brokers Table
  const accounts = (data.portfolio || {}).accounts || [];
  document.getElementById('broker-tbody').innerHTML = accounts.map(a => {
    const ok = (a.status || '').startsWith('connected');
    return `<tr>
      <td><b>${a.broker}</b></td>
      <td><span class="badge ${ok ? 'green' : 'amber'}">${a.status}</span></td>
      <td style="color:#94a3b8; font-size:12px;">${a.custody_location || ''}</td>
      <td>$${(a.equity_usd || 0).toLocaleString()}</td>
      <td>$${(a.cash_usd || 0).toLocaleString()}</td>
      <td>$${(a.capital_cap_usd || 0).toLocaleString()}</td>
    </tr>`;
  }).join('');

  // Signal Regimes Table
  const routing = (data.portfolio || {}).symbol_routing || {};
  const regimes = Object.values(data.signal_regimes || {});
  document.getElementById('signal-tbody').innerHTML = regimes.map(r => {
    const snap = r.snapshot || {};
    const bias = r.trend_bias || 0;
    return `<tr>
      <td><b>${r.symbol}</b></td>
      <td><span class="badge">${routing[r.symbol] || 'local_paper'}</span></td>
      <td>$${snap.price ?? '--'}</td>
      <td style="color:${bias >= 0 ? '#10b981' : '#f43f5e'}"><b>${bias >= 0 ? '+' : ''}${bias}</b></td>
      <td>${r.risk_scale}x</td>
      <td><b>${Math.round((r.trust_weight || 0) * 100)}%</b></td>
      <td style="color:#94a3b8; font-size:12px;">${r.thesis || ''}</td>
    </tr>`;
  }).join('');

  document.getElementById('code-viewer').textContent = data.strategy_code || '';
  renderChart(ev.equity_curve || []);
}

async function triggerAction(endpoint, msg) {
  showToast(msg);
  await fetch(endpoint, { method: 'POST' });
  await refreshState();
  showToast('已完成并刷新看板！');
}

async function changeMode(mode) {
  await fetch('/api/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ratchet_mode: mode })
  });
  await refreshState();
  showToast(`已切换棘轮模式为: ${mode}`);
}

window.addEventListener('load', refreshState);
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

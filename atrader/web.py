"""
ATrader Read-Only Web Dashboard & REST Server
===========================================================
Trader-centric interface modeled after modern trading terminals (Robinhood,
Binance, TradingView, Longbridge). Provides instant visibility into:
  1. Overall Portfolio Equity & PnL (Hero Bar & Asset Allocation Bar)
  2. Autonomous Ratchet Auto-Pilot (Hands-free 24/7 research & trading daemon)
  3. Active Strategy & Agent Brain (Strategy logic, parameters, recent trade rationale)
  4. Live Open Positions (Holdings, cost basis, market value, unrealized PnL, target weight)
  5. Asset Watchlist & Live Dynamics (Prices, recent change %, range, agent sentiment)
  6. Performance & Walk-Forward Equity Curve
  7. Karpathy Ratchet Mutation Timeline & Multi-Broker Custody Map
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Any, List
from urllib.parse import urlparse

from atrader.config import PROJECT_ROOT, STATE_DIR, load_config
from atrader.brokers import MultiBrokerRouter
from atrader.daemon import DAEMON_STATE_FILE
from atrader.journal import read_cycles, closed_positions
from atrader.research import public_campaign
from atrader.providers import provider_details, model_endpoint
from atrader.observation import public_state as observation_state


def _read_state(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def collect_dashboard_state() -> Dict[str, Any]:
    cfg = load_config()
    history = _read_state(STATE_DIR / "ratchet_history.json")
    evaluation = _read_state(STATE_DIR / "last_evaluation.json")
    regimes = _read_state(STATE_DIR / "signal_regime.json")

    router = MultiBrokerRouter(cfg)
    portfolio = router.get_unified_portfolio()
    strategy_code = (PROJECT_ROOT / "strategy.py").read_text(encoding="utf-8")
    daemon_state = _read_state(DAEMON_STATE_FILE)

    cycles = read_cycles()

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
                "target_weight_pct": None,
            })

    # Rankings are deterministic portfolio observations, not an untested LLM overlay.
    watchlist_items = []
    latest = cycles[0] if cycles else {}
    preview = _read_state(STATE_DIR/'portfolio_preview.json')
    if latest.get('evaluation_policy') != 'shared_portfolio_daily_v1' or preview.get('timestamp',0) > latest.get('timestamp',0):
        latest = preview
    decisions = {e['symbol']: e for e in latest.get('executions', [])}
    from atrader.universe import SECTORS, UNIVERSE_VERSION
    for sym in cfg['watchlist']:
        decision = decisions.get(sym, {})
        watchlist_items.append({'symbol': sym, 'sector': SECTORS[sym], 'routed_broker': cfg.get('execution_provider','alpaca'),
            'price': decision.get('price'), 'score': decision.get('score'), 'rank': decision.get('rank'),
            'selected': sym in latest.get('selected', []), 'target_weight': decision.get('effective_target_weight'),
            'thesis': decision.get('reason', '等待首轮日线组合决策'),
            'indicators': decision.get('indicators', {})})

    # Extract active strategy meta
    active_strat = {
        "name": evaluation.get("strategy_name", "baseline"),
        "generation": history.get("current_generation", 0),
        "description": evaluation.get("strategy_description", "自适应双周期 EMA 顺势跟踪 + RSI 超卖抄底 + 动态 ATR 移动止损"),
        "params": evaluation.get("strategy_params", {}),
        "ratchet_score": evaluation.get("ratchet_score", 0.0),
        "total_llm_cost_usd": history.get("total_llm_cost_usd", 0.0),
        "model_roi": evaluation.get("profit_to_model_cost_ratio", "∞"),
    }

    is_alpaca = cfg.get('execution_provider') != 'local_paper'
    paper_orders = ([order for account in portfolio['accounts'] for order in account.get('recent_orders', [])]
                    if is_alpaca else _read_state(STATE_DIR / "paper_account.json").get("orders", []))
    archived_orders = []
    for archive in ([] if is_alpaca else sorted((STATE_DIR / "archives").glob("paper_synthetic_*.json"))):
        archived_orders.extend({**order, "market_data_source": "synthetic"} for order in _read_state(archive).get("orders", []))
    latest_decisions = {}
    for cycle in cycles:
        if cycle.get("evaluation_policy") != "shared_portfolio_daily_v1":
            continue
        for execution in cycle.get("executions", []):
            latest_decisions.setdefault((execution["routed_broker"], execution["symbol"]), execution)
    for position in flat_positions:
        last_execution = latest_decisions.get((position["broker"], position["symbol"]))
        position["in_scope"] = position["symbol"] in cfg["watchlist"]
        position["target_weight_pct"] = (round(float(last_execution["effective_target_weight"]) * 100, 1) if last_execution else None) if position["in_scope"] else (None if is_alpaca else 0.0)

    return {
        "providers": provider_details(cfg, history),
        "paper_observation": observation_state(STATE_DIR) if cfg.get('paper_observation_enabled') else {'enabled':False},
        "paper_epoch": {} if is_alpaca else _read_state(STATE_DIR / "paper_epoch.json"),
        "portfolio_plan": {k: latest.get(k) for k in ("status","signal_asof","strategy_name","selected")},
        "decision_cycles": cycles,
        "closed_positions": closed_positions(paper_orders, flat_positions),
        "recent_orders": sorted(paper_orders + archived_orders, key=lambda o: o.get("timestamp", 0), reverse=True)[:200],
        "history_coverage": {"cycle_limit": 200, "closed_positions_source": "alpaca_order_fills" if is_alpaca else "local_paper_fills", "cycles_recorded_from": cycles[-1]["timestamp"] if cycles else None},
        "config": {
            "llm_model": cfg.get("llm_model", "deepseek-chat"),
            "llm_base_url": model_endpoint(cfg),
            "llm_key_configured": bool(
                cfg.get("llm_api_key") and cfg.get("llm_api_key") != "your_deepseek_api_key_here"
            ),
            "market_data_source": cfg.get("market_data_source", "yahoo"),
            "execution_market_data_source": cfg.get("execution_market_data_source", "alpaca"),
            "investment_scope": UNIVERSE_VERSION,
            "benchmark_symbol": cfg['benchmark_symbol'],
            "market_interval": '1d',
            "max_holdings": cfg['max_holdings'],
            "paper_execution_frozen": bool(cfg.get('paper_observation_enabled')) or cfg.get('paper_execution_frozen',True) is not False,
            "paper_observation_enabled": bool(cfg.get('paper_observation_enabled')),
            "max_gross_exposure": cfg['max_gross_exposure'],
            "ratchet_mode": cfg.get("ratchet_mode", "hybrid"),
            "active_brokers": cfg.get("active_brokers", ["local_paper"]),
        },
        "portfolio": portfolio,
        "positions": flat_positions,
        "watchlist": watchlist_items,
        "active_strategy": active_strat,
        "daemon": daemon_state,
        "evaluation": evaluation,
        "ratchet_history": history,
        "research_revaluation": _read_state(STATE_DIR / "research_revaluation.json"),
        "retired_paper_run": _read_state(STATE_DIR / "retired_paper_run.json"),
        "research_campaign": public_campaign(_read_state(STATE_DIR / "research" / "campaign.json")),
        "strategy_code": strategy_code,
    }


DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


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
            try:
                self._send_json(collect_dashboard_state())
            except (RuntimeError, ValueError, OSError):
                self._send_json({'error':'account_unavailable','message':'券商账户读取失败；未使用本地模拟余额替代'},503)
            return
        raw_html = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw_html)))
        self.end_headers()
        self.wfile.write(raw_html)

    def do_POST(self) -> None:
        self._send_json({"error": "read_only", "message": "Dashboard is read-only"}, status=405)


def run_web_server(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = HTTPServer((host, port), ATraderDashboardHandler)
    print(f"🚀 ATrader Web Dashboard running at: http://{host}:{port}")
    print("   Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down ATrader Web Dashboard.")
        server.server_close()

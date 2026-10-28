"""
ATrader Autonomous Ratchet Daemon (Auto-Pilot)
==============================================
Runs the Karpathy Ratchet fully autonomously without human intervention:
  1. Autonomous Research Loop (Evolve strategy.py at periodic intervals or market close)
     with strict daily token budget guardrails ($0.50/day default).
  2. Autonomous Trading Loop (Bar-by-bar live/paper rebalancing at fixed intervals with 0 runtime tokens).
"""

import threading
import time
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from atrader.config import STATE_DIR, load_config
from atrader.portfolio import session_date
from atrader.ratchet import SIGNAL_REGIME_FILE
from atrader.ratchet import run_code_ratchet_step, run_signal_ratchet_update, run_live_trading_cycle


DAEMON_STATE_FILE = STATE_DIR / "daemon_state.json"


class AutonomousDaemon:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.lock = threading.RLock()

    def get_state(self) -> Dict[str, Any]:
        with self.lock:
            if DAEMON_STATE_FILE.exists():
                try:
                    return json.loads(DAEMON_STATE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            default_state = {
                "is_running": False,
                "trade_interval_sec": 60,
                "evolve_interval_sec": 1800,  # 30 mins
                "last_trade_ts": 0,
                "last_evolve_ts": 0,
                "next_trade_in_sec": 0,
                "next_evolve_in_sec": 0,
                "daily_budget_usd": 0.50,
                "today_llm_spent_usd": 0.0,
                "today_date": session_date(time.time()),
                "recent_logs": [
                    {
                        "ts": int(time.time()),
                        "type": "SYSTEM",
                        "msg": "Autonomous Ratchet Daemon initialized. Ready to auto-pilot.",
                    }
                ],
            }
            self._save_state(default_state)
            return default_state

    def _save_state(self, state: Dict[str, Any]) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = DAEMON_STATE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(DAEMON_STATE_FILE)

    def _update_state(self, **changes: Any) -> Dict[str, Any]:
        with self.lock:
            state = self.get_state()
            state.update(changes)
            self._save_state(state)
            return state

    def add_log(self, log_type: str, message: str) -> None:
        with self.lock:
            state = self.get_state()
            logs = state.setdefault("recent_logs", [])
            logs.append({"ts": int(time.time()), "type": log_type, "msg": message})
            state["recent_logs"] = logs[-30:]
            self._save_state(state)

    def start(self, trade_interval: int = 60, evolve_interval: int = 1800, budget: Optional[float] = None) -> Dict[str, Any]:
        with self.lock:
            if self._thread and self._thread.is_alive():
                return self.get_state()

            self._stop_event.clear()
            state = self.get_state()
            state["is_running"] = True
            state["trade_interval_sec"] = trade_interval
            state["evolve_interval_sec"] = evolve_interval
            if budget is not None:
                state["daily_budget_usd"] = max(0.0, budget)
            self._save_state(state)

            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            self.add_log("AUTONOMOUS", f"Auto-Pilot started: Trade every {trade_interval}s, Evolve every {evolve_interval}s.")
            return state

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            self._stop_event.set()
            state = self.get_state()
            state["is_running"] = False
            self._save_state(state)
            self.add_log("AUTONOMOUS", "Auto-Pilot paused.")
            return state

    def toggle(self) -> Dict[str, Any]:
        state = self.get_state()
        if state.get("is_running", False):
            return self.stop()
        else:
            return self.start(
                trade_interval=state.get("trade_interval_sec", 60),
                evolve_interval=state.get("evolve_interval_sec", 1800),
            )

    def _tick(self, now: float) -> None:
        cfg = load_config()
        state = self.get_state()
        today = session_date(now)
        from atrader.research import recorded_daily_cost
        daily_cost = recorded_daily_cost(STATE_DIR, today)
        if state.get('today_date') != today or state.get('today_llm_spent_usd') != daily_cost:
            state = self._update_state(today_date=today, today_llm_spent_usd=daily_cost)

        # Refresh the hybrid overlay before trading; never reuse a stale signal.
        signal_interval = max(60, float(cfg.get("signal_interval_hours", 4)) * 3600)
        signal_ts = 0
        if SIGNAL_REGIME_FILE.exists():
            try:
                signals = json.loads(SIGNAL_REGIME_FILE.read_text(encoding="utf-8"))
                watched = cfg.get("watchlist", ["BTC/USDT", "ETH/USDT", "NVDA", "SPY"])
                if all(sym in signals and signals[sym].get("snapshot", {}).get("source") == cfg.get("market_data_source", "yahoo") for sym in watched):
                    signal_ts = min(signals[sym].get("updated_at", 0) for sym in watched)
            except (OSError, ValueError):
                pass
        if cfg.get("ratchet_mode", "hybrid") in ("hybrid", "signal_ratchet"):
            if now - signal_ts >= signal_interval and now - state.get("last_signal_attempt_ts", 0) >= 60:
                self._update_state(last_signal_attempt_ts=int(now))
                if state.get("today_llm_spent_usd", 0) < state.get("daily_budget_usd", 0.50):
                    try:
                        result = run_signal_ratchet_update()
                        state = self._update_state(
                            last_signal_ts=int(now),
                            today_llm_spent_usd=round(state.get("today_llm_spent_usd", 0) + result.get("llm_cost_usd", 0), 6),
                        )
                        signal_ts = now
                        self.add_log("SIGNAL", f"Market signal refreshed from {result.get('provider')}")
                    except Exception as exc:
                        self.add_log("ERROR", f"Signal refresh failed; stale overlay will be ignored: {exc}")
                else:
                    self.add_log("GUARDRAIL", "Daily budget reached; skipping model signal refresh.")

        state = self.get_state()
        trade_interval = state.get("trade_interval_sec", 60)
        if now - max(state.get("last_trade_ts", 0), state.get("last_trade_attempt_ts", 0)) >= trade_interval:
            self._update_state(last_trade_attempt_ts=int(now))
            try:
                result = run_live_trading_cycle()
                filled = sum(e.get("order_result", {}).get("status") == "filled" for e in result.get("executions", []))
                self._update_state(last_trade_ts=int(now), last_trade_status=result.get('status'))
                if result.get("status") == "completed":
                    self.add_log("TRADE", f"Daily portfolio [{result.get('strategy_name')}] completed; {filled} filled order(s).")
                elif result.get('status') in ('open_orders_blocked','execution_failed','orders_pending','observation_blocked','exit_pending','cash','external_cleanup_pending') and state.get('last_trade_status') != result.get('status'):
                    self.add_log('GUARDRAIL', 'Alpaca paper: ' + result['status'])
            except Exception as exc:
                self.add_log("ERROR", f"Trade cycle skipped or failed: {exc}")

        state = self.get_state()
        evolve_interval = state.get("evolve_interval_sec", 1800)
        if now - state.get("last_evolve_ts", 0) >= evolve_interval and now - state.get("last_evolve_attempt_ts", 0) >= 60:
            self._update_state(last_evolve_attempt_ts=int(now))
            spent = state.get("today_llm_spent_usd", 0)
            if spent < state.get("daily_budget_usd", 0.50):
                try:
                    self.add_log("RESEARCH", "Evaluating candidate and incumbent on one frozen market sample...")
                    research = run_code_ratchet_step(steps=1)
                    self._update_state(last_evolve_ts=int(now), next_evolve_in_sec=evolve_interval, research_status=research.get('research_status', 'active'))
                    if not research['steps_executed']:
                        if state.get('research_status') != research.get('research_status'):
                            self.add_log('GUARDRAIL', 'Research: ' + research.get('research_status', 'no candidate'))
                        return
                    step = research['steps_executed'][0]
                    self._update_state(last_evolve_ts=int(now), today_llm_spent_usd=round(spent + step.get("llm_cost_usd", 0), 6))
                    self.add_log("RESEARCH", f"Gen #{step['generation']} [{step['status']}] Score: {step['ratchet_score']} | Cost: ${step.get('llm_cost_usd', 0):.6f}")
                except Exception as exc:
                    self.add_log("ERROR", f"Research loop error: {exc}")
            else:
                self._update_state(last_evolve_ts=int(now))
                self.add_log("GUARDRAIL", "Daily LLM budget reached; skipping research.")

        state = self.get_state()
        self._update_state(
            next_trade_in_sec=max(0, int(trade_interval - (time.time() - state.get("last_trade_ts", 0)))),
            next_evolve_in_sec=max(0, int(evolve_interval - (time.time() - state.get("last_evolve_ts", 0)))),
            next_signal_in_sec=max(0, int(signal_interval - (time.time() - signal_ts))),
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick(time.time())
            except Exception as exc:
                self.add_log("ERROR", f"Scheduler error: {exc}")
            self._stop_event.wait(5)


# Global singleton instance
daemon_instance = AutonomousDaemon()

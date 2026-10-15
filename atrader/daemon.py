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
from atrader.ratchet import run_code_ratchet_step, run_signal_ratchet_update, run_live_trading_cycle


DAEMON_STATE_FILE = STATE_DIR / "daemon_state.json"


class AutonomousDaemon:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.lock = threading.Lock()

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
                "today_date": time.strftime("%Y-%m-%d"),
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
        try:
            DAEMON_STATE_FILE.write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def add_log(self, log_type: str, message: str) -> None:
        state = self.get_state()
        state.setdefault("recent_logs", []).append({
            "ts": int(time.time()),
            "type": log_type,
            "msg": message,
        })
        # Keep latest 30 logs
        state["recent_logs"] = state["recent_logs"][-30:]
        self._save_state(state)

    def start(self, trade_interval: int = 60, evolve_interval: int = 1800) -> Dict[str, Any]:
        with self.lock:
            if self._thread and self._thread.is_alive():
                return self.get_state()

            self._stop_event.clear()
            state = self.get_state()
            state["is_running"] = True
            state["trade_interval_sec"] = trade_interval
            state["evolve_interval_sec"] = evolve_interval
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

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            now = time.time()
            state = self.get_state()

            # Date reset for daily spend budget
            today = time.strftime("%Y-%m-%d")
            if state.get("today_date") != today:
                state["today_date"] = today
                state["today_llm_spent_usd"] = 0.0

            trade_int = state.get("trade_interval_sec", 60)
            evolve_int = state.get("evolve_interval_sec", 1800)
            last_trade = state.get("last_trade_ts", 0)
            last_evolve = state.get("last_evolve_ts", 0)

            # 1. Check Trading Loop
            if now - last_trade >= trade_int:
                try:
                    res = run_live_trading_cycle()
                    strat_name = res.get("strategy_name", "unknown")
                    n_orders = sum(1 for e in res.get("executions", []) if e.get("order_result", {}).get("status") == "filled")
                    state["last_trade_ts"] = int(now)
                    self._save_state(state)
                    self.add_log("TRADE", f"0-Token cycle executed [{strat_name}]. Filled {n_orders} rebalance order(s).")
                except Exception as e:
                    self.add_log("ERROR", f"Trade loop error: {e}")

            # 2. Check Research Ratchet Loop
            if now - last_evolve >= evolve_int:
                daily_spent = state.get("today_llm_spent_usd", 0.0)
                daily_limit = state.get("daily_budget_usd", 0.50)

                if daily_spent < daily_limit:
                    try:
                        self.add_log("RESEARCH", "Waking up autonomous Karpathy Code Ratchet mutation...")
                        step_out = run_code_ratchet_step(steps=1)
                        step = step_out["steps_executed"][0]
                        cost = step.get("llm_cost_usd", 0.0)

                        state["last_evolve_ts"] = int(now)
                        state["today_llm_spent_usd"] = round(daily_spent + cost, 5)
                        self._save_state(state)

                        status_tag = step.get("status", "UNKNOWN")
                        score = step.get("ratchet_score", 0.0)
                        hyp = step.get("hypothesis", "")[:60]
                        self.add_log("RESEARCH", f"Gen #{step['generation']} [{status_tag}] Score: {score} | Cost: ${cost:.5f} | {hyp}")
                    except Exception as e:
                        self.add_log("ERROR", f"Research loop error: {e}")
                else:
                    self.add_log("GUARDRAIL", f"Daily LLM budget cap reached (${daily_spent:.4f} >= ${daily_limit:.2f}). Skipping evolve.")
                    state["last_evolve_ts"] = int(now)
                    self._save_state(state)

            # Compute next countdowns
            state["next_trade_in_sec"] = max(0, int(trade_int - (time.time() - state.get("last_trade_ts", 0))))
            state["next_evolve_in_sec"] = max(0, int(evolve_int - (time.time() - state.get("last_evolve_ts", 0))))
            self._save_state(state)

            # Sleep 1 second before next tick check
            for _ in range(5):
                if self._stop_event.is_set():
                    break
                time.sleep(1)


# Global singleton instance
daemon_instance = AutonomousDaemon()

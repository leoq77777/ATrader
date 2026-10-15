"""
Agent B Wrapper Template for ATrader Arena
==========================================
Use this file to wrap your other trading agent so it can compete
head-to-head against ATrader in `python3 main.py arena`.

Only one method is required:
  `on_bars(bars, position_qty, cash, regime_state) -> dict`
Returning:
  `{"target_weight": float in [0.0, 1.0], "reason": str}`
"""

from typing import List, Dict, Any, Optional
from strategy import calc_ema, calc_rsi


class Strategy:
    name: str = "custom_external_agent_b"
    description: str = "User's second trading agent / baseline competitor."

    def __init__(self, custom_params: Optional[Dict[str, Any]] = None):
        self.params = {
            "fast_period": 9,
            "slow_period": 21,
            "rsi_filter": 50.0,
        }
        if custom_params:
            self.params.update(custom_params)

    def on_bars(
        self,
        bars: List[Dict[str, Any]],
        position_qty: float,
        cash: float,
        regime_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Replace or delegate this method to your other agent!
        You can call an external LLM, an external REST API, or custom Python logic here.
        """
        if len(bars) < 25:
            return {"target_weight": 0.0, "reason": "warmup"}

        closes = [float(b["close"]) for b in bars]
        fast = calc_ema(closes, self.params["fast_period"])
        slow = calc_ema(closes, self.params["slow_period"])
        rsi = calc_rsi(closes, 14)

        if fast and slow and fast > slow and rsi > self.params["rsi_filter"]:
            return {"target_weight": 0.85, "reason": "agent_b_momentum_long"}
        return {"target_weight": 0.0, "reason": "agent_b_cash"}

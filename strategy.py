"""
ATrader Mutable Strategy File (The Karpathy Ratchet Target)
===========================================================
In `code_ratchet` and `hybrid` modes, the Agent LLM iteratively mutates ONLY this file.
The immutable evaluator (`atrader/evaluator.py`) runs walk-forward backtests on this file,
deducts trading fees, slippage, and LLM token costs, and either:
  - LOCKS (Git commits) the mutation if the Ratchet Score strictly improves over baseline, OR
  - REVERTS (`git checkout -- strategy.py`) immediately if performance degrades.

DO NOT break the `Strategy` class signature:
  - `Strategy.name` (str)
  - `Strategy.params` (dict)
  - `Strategy.on_bars(bars, position, cash, regime_state) -> dict`
"""

from typing import List, Dict, Any, Optional
import math


# ------------------------------------------------------------------------------
# Pure-Python Technical Indicator Primitives (Zero External Dependencies)
# ------------------------------------------------------------------------------

def calc_sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def calc_ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    k = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period
    for price in values[period:]:
        ema_val = price * k + ema_val * (1.0 - k)
    return ema_val


def calc_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def calc_atr(bars: List[Dict[str, Any]], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: List[float] = []
    window = bars[-(period + 1):]
    for i in range(1, len(window)):
        high = float(window[i]["high"])
        low = float(window[i]["low"])
        prev_close = float(window[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def calc_zscore(values: List[float], period: int = 20) -> float:
    if len(values) < period or period <= 1:
        return 0.0
    subset = values[-period:]
    mean = sum(subset) / period
    variance = sum((x - mean) ** 2 for x in subset) / period
    std = math.sqrt(variance)
    if std < 1e-9:
        return 0.0
    return (values[-1] - mean) / std


# ------------------------------------------------------------------------------
# Mutable Strategy Definition (Evolved by the Karpathy Ratchet)
# ------------------------------------------------------------------------------

class Strategy:
    """
    Baseline Strategy: Adaptive Dual-Regime Trend + Volatility Breakout with ATR Trailing Stop.
    The LLM Agent is encouraged to evolve indicator combinations, entry/exit filters,
    volatility scaling, and regime adaptation to maximize out-of-sample Ratchet Score.
    """

    name: str = "ratchet_gen3_momentum_dip_accumulator"
    description: str = (
        "Combines fast/slow EMA trend alignment with RSI momentum filter, "
        "Z-score mean-reversion guard, and ATR volatility-targeted position sizing."
    )

    def __init__(self, custom_params: Optional[Dict[str, Any]] = None):
        self.params: Dict[str, Any] = {
            "fast_ema": 4,
            "slow_ema": 9,
            "rsi_period": 14,
            "rsi_buy_max": 72.0,
            "rsi_oversold": 36.0,
            "rsi_exit": 79.0,
            "atr_period": 14,
            "stop_atr_mult": 1.1,
            "target_vol_pct": 0.024,
            "max_leverage": 0.95,
            "min_bars": 36,
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
        Deterministic bar-by-bar decision function (0 LLM tokens during execution).

        Parameters:
            bars: Historical OHLCV dicts up to current bar `[{"timestamp", "open", "high", "low", "close", "volume"}, ...]`
            position_qty: Current held units of the asset
            cash: Available cash in account currency
            regime_state: Optional low-frequency signal_ratchet overlay
                          `{"trend_bias": float, "risk_scale": float, "trust_weight": float}`

        Returns:
            dict with keys:
              - `target_weight`: Desired portfolio allocation in [0.0, 1.0] (long-only spot/equity safe default)
              - `reason`: Concise human-readable explanation of signal
        """
        if len(bars) < self.params["min_bars"]:
            return {"target_weight": 0.0, "reason": "warmup_insufficient_bars"}

        closes = [float(b["close"]) for b in bars]
        price = closes[-1]
        fast_ema = calc_ema(closes, int(self.params["fast_ema"]))
        slow_ema = calc_ema(closes, int(self.params["slow_ema"]))
        rsi = calc_rsi(closes, int(self.params["rsi_period"]))
        atr = calc_atr(bars, int(self.params["atr_period"]))
        zscore = calc_zscore(closes, 20)

        if fast_ema is None or slow_ema is None or price <= 0:
            return {"target_weight": 0.0, "reason": "indicator_not_ready"}

        # Volatility-targeted base allocation
        atr_pct = (atr / price) if price > 0 else 0.02
        vol_scalar = min(
            float(self.params["max_leverage"]),
            float(self.params["target_vol_pct"]) / max(atr_pct, 0.004),
        )

        # Core technical logic
        trend_up = fast_ema > slow_ema * 1.001
        momentum_healthy = 42.0 <= rsi <= float(self.params["rsi_buy_max"])
        oversold_bounce = (rsi < float(self.params["rsi_oversold"])) and (zscore < -1.6) and (closes[-1] > closes[-2])
        overbought_exhaustion = (rsi > float(self.params["rsi_exit"])) or (zscore > 2.3)

        # Trailing stop check against recent local high
        recent_high = max(float(b["high"]) for b in bars[-10:])
        trailing_stop_hit = price < (recent_high - float(self.params["stop_atr_mult"]) * atr)

        target_weight = 0.0
        reason = "hold_flat"

        if position_qty > 0:
            if trailing_stop_hit:
                target_weight = 0.0
                reason = f"atr_trailing_stop (price={price:.2f}, high={recent_high:.2f})"
            elif overbought_exhaustion:
                target_weight = round(vol_scalar * 0.35, 4)
                reason = f"partial_take_profit (rsi={rsi:.1f}, z={zscore:.2f})"
            elif not trend_up and rsi < 45.0:
                target_weight = 0.0
                reason = "trend_reversal_exit"
            else:
                target_weight = round(vol_scalar, 4)
                reason = "ride_uptrend"
        else:
            if (trend_up and momentum_healthy and not trailing_stop_hit) or oversold_bounce:
                target_weight = round(vol_scalar, 4)
                reason = "trend_entry" if trend_up else "oversold_rebound_entry"

        # Hybrid Mode Overlay: Blend with low-frequency Signal Ratchet if present
        if regime_state and isinstance(regime_state, dict):
            trust = float(regime_state.get("trust_weight", 0.0))
            risk_scale = float(regime_state.get("risk_scale", 1.0))
            trend_bias = float(regime_state.get("trend_bias", 0.0))
            if trust > 0.05:
                # Modulate weight gently according to trusted LLM regime assessment
                regime_multiplier = (1.0 - trust) + trust * max(0.0, min(1.5, risk_scale * (1.0 + 0.35 * trend_bias)))
                target_weight = max(0.0, min(float(self.params["max_leverage"]), round(target_weight * regime_multiplier, 4)))
                reason += f"|regime_adj(trust={trust:.2f},bias={trend_bias:+.2f})"

        return {
            "target_weight": max(0.0, min(1.0, target_weight)),
            "reason": reason,
            "indicators": {
                "fast_ema": round(fast_ema, 4),
                "slow_ema": round(slow_ema, 4),
                "rsi": round(rsi, 2),
                "atr": round(atr, 4),
                "zscore": round(zscore, 2),
            },
        }



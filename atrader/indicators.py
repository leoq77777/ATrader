"""Fixed indicators used by advisory snapshots; independent of mutable strategy code."""
import math
from typing import List, Dict, Any, Optional

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

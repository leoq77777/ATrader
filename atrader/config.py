"""
ATrader Configuration & Environment Loader
Supports .env files, environment variables, and runtime config.json persistence.
"""

import os
import json
from pathlib import Path
from typing import Dict, Any, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = STATE_DIR / "config.json"


def _parse_dotenv(dotenv_path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not dotenv_path.exists():
        return values
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        values[key] = val
    return values


DEFAULT_CONFIG: Dict[str, Any] = {
    # LLM (DeepSeek default, OpenAI-compatible)
    "llm_api_key": "",
    "llm_base_url": "https://api.deepseek.com",
    "llm_model": "deepseek-chat",
    "llm_cost_input_per_1m": 0.27,
    "llm_cost_output_per_1m": 1.10,
    # Ratchet settings: code_ratchet | signal_ratchet | hybrid
    "ratchet_mode": "hybrid",
    "min_score_improvement": 0.015,
    "signal_interval_hours": 4,
    # Active brokers & Multi-broker capital routing
    "active_brokers": ["local_paper"],
    "symbol_routing": {
        "BTC/USDT": "ccxt",
        "ETH/USDT": "ccxt",
        "SOL/USDT": "ccxt",
        "SPY": "alpaca",
        "NVDA": "alpaca",
        "AAPL": "alpaca",
        "QQQ": "alpaca",
        "00700.HK": "longbridge",
        "700.HK": "longbridge",
        "09988.HK": "longbridge",
    },
    # Capital custody & per-broker risk allocation caps (Virtual Master Ledger)
    "broker_allocations": {
        "local_paper": {"enabled": True, "capital_cap_usd": 10000.0, "max_drawdown_pct": 0.08},
        "alpaca": {"enabled": True, "capital_cap_usd": 100000.0, "max_drawdown_pct": 0.05},
        "ccxt": {"enabled": False, "capital_cap_usd": 3000.0, "max_drawdown_pct": 0.06},
        "longbridge": {"enabled": True, "capital_cap_usd": 10000.0, "max_drawdown_pct": 0.05},
    },
    # Broker credentials
    "alpaca_api_key": "",
    "alpaca_secret_key": "",
    "alpaca_base_url": "https://paper-api.alpaca.markets",
    "ccxt_exchange": "binance",
    "ccxt_api_key": "",
    "ccxt_secret_key": "",
    "ccxt_password": "",
    "ccxt_sandbox": True,
    "longbridge_app_key": "",
    "longbridge_app_secret": "",
    "longbridge_access_token": "",
    # Backtest & evaluation parameters
    "initial_paper_cash": 10000.0,
    "max_position_pct": 0.25,
    "max_daily_drawdown_pct": 0.05,
    "trading_fee_bps": 5.0,
    "slippage_bps": 3.0,
    "watchlist": ["BTC/USDT", "ETH/USDT", "NVDA", "SPY"],
}


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    init_file = PROJECT_ROOT / "atrader" / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""ATrader package"""\n__version__ = "0.1.0"\n', encoding="utf-8")
    return STATE_DIR


def load_config() -> Dict[str, Any]:
    ensure_state_dir()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    # 1. Load saved state/config.json if present
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            pass

    # 2. Overlay .env and environment variables
    dotenv_vars = _parse_dotenv(ENV_FILE)
    merged_env = {**dotenv_vars, **os.environ}

    env_map = {
        "LLM_API_KEY": ("llm_api_key", str),
        "DEEPSEEK_API_KEY": ("llm_api_key", str),
        "LLM_BASE_URL": ("llm_base_url", str),
        "LLM_MODEL": ("llm_model", str),
        "LLM_COST_INPUT_PER_1M": ("llm_cost_input_per_1m", float),
        "LLM_COST_OUTPUT_PER_1M": ("llm_cost_output_per_1m", float),
        "RATCHET_MODE": ("ratchet_mode", str),
        "ALPACA_API_KEY": ("alpaca_api_key", str),
        "ALPACA_SECRET_KEY": ("alpaca_secret_key", str),
        "ALPACA_BASE_URL": ("alpaca_base_url", str),
        "CCXT_EXCHANGE": ("ccxt_exchange", str),
        "CCXT_API_KEY": ("ccxt_api_key", str),
        "CCXT_SECRET_KEY": ("ccxt_secret_key", str),
        "CCXT_PASSWORD": ("ccxt_password", str),
        "LONGBRIDGE_APP_KEY": ("longbridge_app_key", str),
        "LONGBRIDGE_APP_SECRET": ("longbridge_app_secret", str),
        "LONGBRIDGE_ACCESS_TOKEN": ("longbridge_access_token", str),
        "INITIAL_PAPER_CASH": ("initial_paper_cash", float),
        "MAX_POSITION_PCT": ("max_position_pct", float),
        "MAX_DAILY_DRAWDOWN_PCT": ("max_daily_drawdown_pct", float),
        "TRADING_FEE_BPS": ("trading_fee_bps", float),
        "SLIPPAGE_BPS": ("slippage_bps", float),
    }

    for env_key, (cfg_key, caster) in env_map.items():
        raw_val = merged_env.get(env_key)
        if raw_val is not None and raw_val != "" and raw_val != "your_deepseek_api_key_here":
            try:
                cfg[cfg_key] = caster(raw_val)
            except Exception:
                pass

    # Sanitize Alpaca Base URL (strip trailing / and /v2)
    alpaca_url = str(cfg.get("alpaca_base_url", "https://paper-api.alpaca.markets")).strip().rstrip("/")
    if alpaca_url.endswith("/v2"):
        alpaca_url = alpaca_url[:-3].rstrip("/")
    cfg["alpaca_base_url"] = alpaca_url or "https://paper-api.alpaca.markets"

    # Auto-activate brokers when credentials exist in .env
    active_set = set(cfg.get("active_brokers", ["local_paper"]))
    if cfg.get("alpaca_api_key") and cfg.get("alpaca_secret_key"):
        active_set.add("alpaca")
        cfg["broker_allocations"]["alpaca"]["enabled"] = True
    if cfg.get("longbridge_app_key") and cfg.get("longbridge_access_token"):
        active_set.add("longbridge")
        cfg["broker_allocations"]["longbridge"]["enabled"] = True
    if cfg.get("ccxt_api_key") and cfg.get("ccxt_secret_key"):
        active_set.add("ccxt")
        cfg["broker_allocations"]["ccxt"]["enabled"] = True

    if "ACTIVE_BROKERS" in merged_env and merged_env["ACTIVE_BROKERS"].strip():
        user_specified = [b.strip() for b in merged_env["ACTIVE_BROKERS"].split(",") if b.strip()]
        if user_specified and user_specified != ["local_paper"]:
            active_set = set(user_specified)
            active_set.add("local_paper")

    cfg["active_brokers"] = list(active_set)

    if "CCXT_SANDBOX" in merged_env:
        cfg["ccxt_sandbox"] = merged_env["CCXT_SANDBOX"].lower() in ("1", "true", "yes")

    return cfg


def save_config(new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    ensure_state_dir()
    current = load_config()
    current.update(new_cfg)
    CONFIG_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current

"""
ATrader Unified Multi-Broker Adapter & Virtual Master Ledger
============================================================
Built-in support for:
  1. `local_paper` : Zero-config local persistent Paper Trading simulator
  2. `alpaca`      : Alpaca Markets REST / SDK (US Stocks, ETFs, Crypto - Paper & Live)
  3. `ccxt`        : Unified Crypto Exchange adapter (Binance, OKX, Bybit, Coinbase)
  4. `longbridge`  : Longbridge (LongPort) OpenAPI adapter (HK & US Equities)

Also provides `MultiBrokerRouter` (Virtual Master Ledger), which aggregates
balances and positions across all active accounts while enforcing per-broker
capital allocation caps.
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional

from atrader.config import STATE_DIR, load_config, ensure_state_dir
from atrader.evaluator import generate_multi_regime_bars


PAPER_LEDGER_FILE = STATE_DIR / "paper_account.json"


class BaseBroker:
    name: str = "base"
    custody_description: str = ""

    def is_configured(self) -> bool:
        return False

    def get_account_summary(self) -> Dict[str, Any]:
        raise NotImplementedError

    def get_recent_bars(self, symbol: str, limit: int = 120) -> List[Dict[str, Any]]:
        return generate_multi_regime_bars(symbol, n_bars=max(limit, 120))[-limit:]

    def place_target_weight_order(
        self, symbol: str, target_weight: float, current_price: float, reason: str = ""
    ) -> Dict[str, Any]:
        raise NotImplementedError


# ------------------------------------------------------------------------------
# 1. Local Paper Trading Broker (Default, Zero-Config, Always Available)
# ------------------------------------------------------------------------------

class LocalPaperBroker(BaseBroker):
    name = "local_paper"
    custody_description = "Local Sandbox Ledger (state/paper_account.json) — $0 real capital required"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        ensure_state_dir()
        self._ensure_ledger()

    def _ensure_ledger(self) -> Dict[str, Any]:
        if PAPER_LEDGER_FILE.exists():
            try:
                return json.loads(PAPER_LEDGER_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        init_cash = float(self.cfg.get("initial_paper_cash", 10000.0))
        ledger = {
            "broker": "local_paper",
            "initial_cash": init_cash,
            "cash": init_cash,
            "positions": {},  # symbol -> {"qty": float, "avg_price": float, "last_price": float}
            "orders": [],
            "updated_at": int(time.time()),
        }
        PAPER_LEDGER_FILE.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
        return ledger

    def _save_ledger(self, ledger: Dict[str, Any]) -> None:
        ledger["updated_at"] = int(time.time())
        PAPER_LEDGER_FILE.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")

    def is_configured(self) -> bool:
        return True

    def get_account_summary(self) -> Dict[str, Any]:
        ledger = self._ensure_ledger()
        cash = float(ledger.get("cash", 0.0))
        positions = ledger.get("positions", {})
        pos_value = 0.0
        formatted_positions: List[Dict[str, Any]] = []

        for sym, pos in positions.items():
            qty = float(pos.get("qty", 0.0))
            if qty <= 1e-9:
                continue
            bars = self.get_recent_bars(sym, limit=2)
            last_price = float(bars[-1]["close"]) if bars else float(pos.get("last_price", 0.0))
            mkt_val = qty * last_price
            avg_price = float(pos.get("avg_price", last_price))
            unrealized_pnl = (last_price - avg_price) * qty
            pos_value += mkt_val
            formatted_positions.append({
                "symbol": sym,
                "qty": round(qty, 6),
                "avg_price": round(avg_price, 4),
                "last_price": round(last_price, 4),
                "market_value_usd": round(mkt_val, 2),
                "unrealized_pnl_usd": round(unrealized_pnl, 2),
            })

        equity = cash + pos_value
        init_cash = float(ledger.get("initial_cash", 10000.0))
        return {
            "broker": self.name,
            "status": "connected (local sandbox)",
            "custody_location": self.custody_description,
            "cash_usd": round(cash, 2),
            "positions_value_usd": round(pos_value, 2),
            "equity_usd": round(equity, 2),
            "net_pnl_usd": round(equity - init_cash, 2),
            "positions": formatted_positions,
            "recent_orders": ledger.get("orders", [])[-10:],
        }

    def place_target_weight_order(
        self, symbol: str, target_weight: float, current_price: float, reason: str = ""
    ) -> Dict[str, Any]:
        ledger = self._ensure_ledger()
        summary = self.get_account_summary()
        equity = float(summary["equity_usd"])
        cash = float(ledger["cash"])
        positions = ledger.get("positions", {})
        cur_pos = positions.get(symbol, {"qty": 0.0, "avg_price": current_price})
        cur_qty = float(cur_pos.get("qty", 0.0))

        # Apply global max_position_pct cap
        max_pos_pct = float(self.cfg.get("max_position_pct", 0.25))
        effective_weight = max(0.0, min(max_pos_pct, target_weight))

        target_notional = equity * effective_weight
        current_notional = cur_qty * current_price
        delta_usd = target_notional - current_notional

        if abs(delta_usd) < equity * 0.015 or current_price <= 0:
            return {
                "broker": self.name,
                "symbol": symbol,
                "status": "skipped_small_delta",
                "target_weight": effective_weight,
                "reason": reason,
            }

        fee_rate = (float(self.cfg.get("trading_fee_bps", 5.0)) + float(self.cfg.get("slippage_bps", 3.0))) / 10000.0
        fee_usd = abs(delta_usd) * fee_rate

        if delta_usd > 0:
            spend_usd = min(max(0.0, cash - fee_usd), delta_usd)
            buy_qty = spend_usd / current_price
            new_qty = cur_qty + buy_qty
            new_avg = ((cur_qty * float(cur_pos.get("avg_price", current_price))) + spend_usd) / max(new_qty, 1e-9)
            ledger["cash"] = round(cash - spend_usd - fee_usd, 4)
            positions[symbol] = {
                "qty": round(new_qty, 6),
                "avg_price": round(new_avg, 4),
                "last_price": round(current_price, 4),
            }
            side = "BUY"
            filled_qty = buy_qty
        else:
            sell_usd = min(current_notional, abs(delta_usd))
            sell_qty = sell_usd / current_price
            new_qty = max(0.0, cur_qty - sell_qty)
            ledger["cash"] = round(cash + sell_usd - fee_usd, 4)
            if new_qty <= 1e-8:
                positions.pop(symbol, None)
            else:
                positions[symbol]["qty"] = round(new_qty, 6)
                positions[symbol]["last_price"] = round(current_price, 4)
            side = "SELL"
            filled_qty = sell_qty

        order_record = {
            "timestamp": int(time.time()),
            "broker": self.name,
            "symbol": symbol,
            "side": side,
            "qty": round(filled_qty, 6),
            "price": round(current_price, 4),
            "fee_usd": round(fee_usd, 4),
            "target_weight": round(effective_weight, 4),
            "reason": reason,
        }
        ledger.setdefault("orders", []).append(order_record)
        self._save_ledger(ledger)
        return {"status": "filled", "order": order_record}


# ------------------------------------------------------------------------------
# 2. Alpaca Trading API Adapter (US Equities, ETFs & Crypto)
# ------------------------------------------------------------------------------

class AlpacaBroker(BaseBroker):
    name = "alpaca"
    custody_description = "Alpaca Securities LLC Account (SIPC-insured US Brokerage / Paper Account)"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.api_key = cfg.get("alpaca_api_key", "")
        self.secret_key = cfg.get("alpaca_secret_key", "")
        self.base_url = cfg.get("alpaca_base_url", "https://paper-api.alpaca.markets").rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def get_account_summary(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {
                "broker": self.name,
                "status": "not_configured (set ALPACA_API_KEY in .env)",
                "custody_location": self.custody_description,
                "cash_usd": 0.0,
                "positions_value_usd": 0.0,
                "equity_usd": 0.0,
                "net_pnl_usd": 0.0,
                "positions": [],
            }
        try:
            req = urllib.request.Request(f"{self.base_url}/v2/account", headers=self._headers())
            with urllib.request.urlopen(req, timeout=8) as resp:
                acct = json.loads(resp.read().decode("utf-8"))

            pos_req = urllib.request.Request(f"{self.base_url}/v2/positions", headers=self._headers())
            with urllib.request.urlopen(pos_req, timeout=8) as resp:
                raw_positions = json.loads(resp.read().decode("utf-8"))

            positions = [
                {
                    "symbol": p.get("symbol"),
                    "qty": float(p.get("qty", 0)),
                    "avg_price": float(p.get("avg_entry_price", 0)),
                    "last_price": float(p.get("current_price", 0)),
                    "market_value_usd": float(p.get("market_value", 0)),
                    "unrealized_pnl_usd": float(p.get("unrealized_pl", 0)),
                }
                for p in raw_positions
            ]
            equity = float(acct.get("equity", 0))
            last_equity = float(acct.get("last_equity", equity))
            return {
                "broker": self.name,
                "status": "connected (paper)" if "paper" in self.base_url else "connected (live)",
                "custody_location": self.custody_description,
                "cash_usd": round(float(acct.get("cash", 0)), 2),
                "positions_value_usd": round(float(acct.get("long_market_value", 0)), 2),
                "equity_usd": round(equity, 2),
                "net_pnl_usd": round(equity - last_equity, 2),
                "positions": positions,
            }
        except Exception as exc:
            return {
                "broker": self.name,
                "status": f"connection_error: {exc}",
                "custody_location": self.custody_description,
                "cash_usd": 0.0,
                "positions_value_usd": 0.0,
                "equity_usd": 0.0,
                "net_pnl_usd": 0.0,
                "positions": [],
            }

    def place_target_weight_order(
        self, symbol: str, target_weight: float, current_price: float, reason: str = ""
    ) -> Dict[str, Any]:
        if not self.is_configured():
            return {"broker": self.name, "status": "not_configured"}
        summary = self.get_account_summary()
        equity = float(summary.get("equity_usd", 0.0))
        if equity <= 0 or current_price <= 0:
            return {"broker": self.name, "status": "insufficient_equity"}

        # Apply broker-level capital cap from Virtual Master Ledger
        cap_cfg = self.cfg.get("broker_allocations", {}).get("alpaca", {})
        capped_equity = min(equity, float(cap_cfg.get("capital_cap_usd", equity)))
        max_pos_pct = float(self.cfg.get("max_position_pct", 0.25))
        effective_weight = max(0.0, min(max_pos_pct, target_weight))

        cur_qty = 0.0
        for p in summary.get("positions", []):
            if p["symbol"] == symbol.replace("/", ""):
                cur_qty = float(p["qty"])

        target_notional = capped_equity * effective_weight
        delta_usd = target_notional - (cur_qty * current_price)
        if abs(delta_usd) < 10.0:
            return {"broker": self.name, "symbol": symbol, "status": "skipped_small_delta"}

        side = "buy" if delta_usd > 0 else "sell"
        payload = json.dumps({
            "symbol": symbol.replace("/", ""),
            "notional": str(round(abs(delta_usd), 2)),
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                f"{self.base_url}/v2/orders", data=payload, headers=self._headers(), method="POST"
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                order_resp = json.loads(resp.read().decode("utf-8"))
            return {"broker": self.name, "status": "submitted", "order_id": order_resp.get("id"), "reason": reason}
        except Exception as exc:
            return {"broker": self.name, "status": f"order_error: {exc}"}


# ------------------------------------------------------------------------------
# 3. CCXT Unified Crypto Exchange Adapter (Binance / OKX / Bybit / Coinbase)
# ------------------------------------------------------------------------------

class CCXTBroker(BaseBroker):
    name = "ccxt"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.exchange_id = cfg.get("ccxt_exchange", "binance")
        self.api_key = cfg.get("ccxt_api_key", "")
        self.secret_key = cfg.get("ccxt_secret_key", "")
        self.password = cfg.get("ccxt_password", "")
        self.sandbox = bool(cfg.get("ccxt_sandbox", True))
        self.custody_description = (
            f"Crypto Exchange Account ({self.exchange_id.upper()} {'Testnet/Sandbox' if self.sandbox else 'Spot/Margin USDT Wallet'})"
        )

    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _get_client(self) -> Any:
        import ccxt  # Optional dependency
        exchange_cls = getattr(ccxt, self.exchange_id)
        params: Dict[str, Any] = {
            "apiKey": self.api_key,
            "secret": self.secret_key,
            "enableRateLimit": True,
        }
        if self.password:
            params["password"] = self.password
        client = exchange_cls(params)
        if self.sandbox and hasattr(client, "set_sandbox_mode"):
            client.set_sandbox_mode(True)
        return client

    def get_account_summary(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {
                "broker": self.name,
                "exchange": self.exchange_id,
                "status": f"not_configured (set CCXT_API_KEY for {self.exchange_id} in .env)",
                "custody_location": self.custody_description,
                "cash_usd": 0.0,
                "positions_value_usd": 0.0,
                "equity_usd": 0.0,
                "net_pnl_usd": 0.0,
                "positions": [],
            }
        try:
            client = self._get_client()
            bal = client.fetch_balance()
            usdt_free = float(bal.get("free", {}).get("USDT", 0.0))
            positions = []
            pos_val = 0.0
            for asset, total_qty in bal.get("total", {}).items():
                if asset in ("USDT", "USDC", "USD") or float(total_qty or 0) <= 1e-6:
                    continue
                pair = f"{asset}/USDT"
                ticker = client.fetch_ticker(pair)
                last_p = float(ticker.get("last", 0.0))
                val = float(total_qty) * last_p
                pos_val += val
                positions.append({
                    "symbol": pair,
                    "qty": float(total_qty),
                    "avg_price": last_p,
                    "last_price": last_p,
                    "market_value_usd": round(val, 2),
                    "unrealized_pnl_usd": 0.0,
                })
            return {
                "broker": self.name,
                "exchange": self.exchange_id,
                "status": f"connected ({self.exchange_id})",
                "custody_location": self.custody_description,
                "cash_usd": round(usdt_free, 2),
                "positions_value_usd": round(pos_val, 2),
                "equity_usd": round(usdt_free + pos_val, 2),
                "net_pnl_usd": 0.0,
                "positions": positions,
            }
        except Exception as exc:
            return {
                "broker": self.name,
                "exchange": self.exchange_id,
                "status": f"error_or_sdk_missing: {exc}",
                "custody_location": self.custody_description,
                "cash_usd": 0.0,
                "positions_value_usd": 0.0,
                "equity_usd": 0.0,
                "net_pnl_usd": 0.0,
                "positions": [],
            }

    def place_target_weight_order(
        self, symbol: str, target_weight: float, current_price: float, reason: str = ""
    ) -> Dict[str, Any]:
        if not self.is_configured():
            return {"broker": self.name, "status": "not_configured"}
        try:
            client = self._get_client()
            summary = self.get_account_summary()
            equity = float(summary.get("equity_usd", 0.0))
            cap_cfg = self.cfg.get("broker_allocations", {}).get("ccxt", {})
            capped_equity = min(equity, float(cap_cfg.get("capital_cap_usd", equity)))
            max_pos_pct = float(self.cfg.get("max_position_pct", 0.25))
            effective_weight = max(0.0, min(max_pos_pct, target_weight))

            cur_qty = 0.0
            for p in summary.get("positions", []):
                if p["symbol"] == symbol:
                    cur_qty = float(p["qty"])

            delta_usd = (capped_equity * effective_weight) - (cur_qty * current_price)
            if abs(delta_usd) < 10.0 or current_price <= 0:
                return {"broker": self.name, "symbol": symbol, "status": "skipped_small_delta"}

            qty = abs(delta_usd) / current_price
            side = "buy" if delta_usd > 0 else "sell"
            order = client.create_market_order(symbol, side, qty)
            return {"broker": self.name, "status": "submitted", "order_id": order.get("id"), "reason": reason}
        except Exception as exc:
            return {"broker": self.name, "status": f"order_error: {exc}"}


# ------------------------------------------------------------------------------
# 4. Longbridge (LongPort) OpenAPI Adapter (HK & US Equities)
# ------------------------------------------------------------------------------

class LongbridgeBroker(BaseBroker):
    name = "longbridge"
    custody_description = "Longbridge Securities Account (HK/US Licensed Broker Account)"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.app_key = cfg.get("longbridge_app_key", "")
        self.app_secret = cfg.get("longbridge_app_secret", "")
        self.access_token = cfg.get("longbridge_access_token", "")

    def is_configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.access_token)

    def get_account_summary(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {
                "broker": self.name,
                "status": "not_configured (set LONGBRIDGE_APP_KEY in .env)",
                "custody_location": self.custody_description,
                "cash_usd": 0.0,
                "positions_value_usd": 0.0,
                "equity_usd": 0.0,
                "net_pnl_usd": 0.0,
                "positions": [],
            }
        try:
            from longbridge.openapi import Config, TradeContext
            lb_cfg = Config(self.app_key, self.app_secret, self.access_token)
            ctx = TradeContext(lb_cfg)
            balances = ctx.account_balance()
            total_cash = sum(float(b.total_cash) for b in balances)
            net_assets = sum(float(b.net_assets) for b in balances)
            return {
                "broker": self.name,
                "status": "connected (longbridge)",
                "custody_location": self.custody_description,
                "cash_usd": round(total_cash, 2),
                "positions_value_usd": round(max(0.0, net_assets - total_cash), 2),
                "equity_usd": round(net_assets, 2),
                "net_pnl_usd": 0.0,
                "positions": [],
            }
        except Exception as exc:
            return {
                "broker": self.name,
                "status": f"error_or_sdk_missing: {exc}",
                "custody_location": self.custody_description,
                "cash_usd": 0.0,
                "positions_value_usd": 0.0,
                "equity_usd": 0.0,
                "net_pnl_usd": 0.0,
                "positions": [],
            }

    def place_target_weight_order(
        self, symbol: str, target_weight: float, current_price: float, reason: str = ""
    ) -> Dict[str, Any]:
        if not self.is_configured():
            return {"broker": self.name, "status": "not_configured"}
        return {
            "broker": self.name,
            "symbol": symbol,
            "status": "ready_for_live_execution",
            "target_weight": target_weight,
            "reason": reason,
        }


# ------------------------------------------------------------------------------
# 5. Multi-Broker Router & Virtual Master Ledger
# ------------------------------------------------------------------------------

class MultiBrokerRouter:
    """
    Solves the multi-API capital management problem:
      - Keeps real capital safely inside each regulated broker/exchange account
      - Routes each symbol to its designated broker (or falls back to `local_paper`)
      - Aggregates all accounts into a single Virtual Master Ledger with per-broker risk caps
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or load_config()
        self.brokers: Dict[str, BaseBroker] = {
            "local_paper": LocalPaperBroker(self.cfg),
            "alpaca": AlpacaBroker(self.cfg),
            "ccxt": CCXTBroker(self.cfg),
            "longbridge": LongbridgeBroker(self.cfg),
        }

    def resolve_broker_for_symbol(self, symbol: str) -> BaseBroker:
        active = self.cfg.get("active_brokers", ["local_paper"])
        routing = self.cfg.get("symbol_routing", {})
        preferred_name = routing.get(symbol, "local_paper")

        if preferred_name in active and preferred_name in self.brokers:
            candidate = self.brokers[preferred_name]
            if candidate.is_configured():
                return candidate

        # Fallback to first configured active broker, or local_paper
        for b_name in active:
            b = self.brokers.get(b_name)
            if b and b.is_configured():
                return b
        return self.brokers["local_paper"]

    def get_unified_portfolio(self) -> Dict[str, Any]:
        active = set(self.cfg.get("active_brokers", ["local_paper"]))
        allocations = self.cfg.get("broker_allocations", {})
        accounts: List[Dict[str, Any]] = []
        total_equity = 0.0
        total_cash = 0.0
        total_pnl = 0.0

        for name, broker in self.brokers.items():
            summary = broker.get_account_summary()
            alloc_cfg = allocations.get(name, {})
            is_active = (name in active) or bool(alloc_cfg.get("enabled", False))
            summary["is_active"] = is_active
            summary["capital_cap_usd"] = float(alloc_cfg.get("capital_cap_usd", 10000.0))
            summary["max_drawdown_pct"] = float(alloc_cfg.get("max_drawdown_pct", 0.05))
            accounts.append(summary)
            if is_active:
                total_equity += float(summary.get("equity_usd", 0.0))
                total_cash += float(summary.get("cash_usd", 0.0))
                total_pnl += float(summary.get("net_pnl_usd", 0.0))

        return {
            "virtual_master_ledger": {
                "total_equity_usd": round(total_equity, 2),
                "total_cash_usd": round(total_cash, 2),
                "total_positions_usd": round(total_equity - total_cash, 2),
                "total_net_pnl_usd": round(total_pnl, 2),
                "active_brokers": list(active),
            },
            "symbol_routing": self.cfg.get("symbol_routing", {}),
            "accounts": accounts,
        }

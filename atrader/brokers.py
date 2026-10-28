"""Paper-only execution adapter. IBKR orders require a separate reviewed integration."""
import json
import time
from pathlib import Path
from atrader.config import STATE_DIR, load_config, ensure_state_dir, write_json_atomic
from atrader.market_data import get_recent_bars
from atrader.portfolio import mark_equity
from atrader.universe import SECTORS

PAPER_LEDGER_FILE = STATE_DIR / 'paper_account.json'


class LocalPaperBroker:
    name = 'local_paper'
    custody_description = 'USD paper account · shared cash ledger · no real orders'

    def __init__(self, cfg):
        self.cfg = cfg
        ensure_state_dir()

    def _ensure_ledger(self):
        if PAPER_LEDGER_FILE.exists():
            # Corrupt ledgers must fail, never silently recreate funds.
            return json.loads(PAPER_LEDGER_FILE.read_text())
        cash = float(self.cfg.get('initial_paper_cash', 10000))
        book = {'broker': self.name, 'initial_cash': cash, 'cash': cash, 'positions': {}, 'orders': [], 'updated_at': int(time.time())}
        self._save_ledger(book)
        return book

    def _save_ledger(self, book):
        write_json_atomic(PAPER_LEDGER_FILE, book)

    def get_recent_bars(self, symbol, limit=120):
        return get_recent_bars(symbol, limit, self.cfg)

    def get_account_summary(self):
        if not PAPER_LEDGER_FILE.exists():
            return {'broker': self.name, 'status': 'not_initialized', 'custody_location': self.custody_description,
                    'cash_usd': 0, 'equity_usd': 0, 'positions_value_usd': 0, 'net_pnl_usd': 0, 'positions': [], 'recent_orders': []}
        book = self._ensure_ledger()
        positions, prices = [], {}
        for symbol, pos in book.get('positions', {}).items():
            bars = self.get_recent_bars(symbol, 2)
            price = float(bars[-1].get('quote_price', bars[-1]['close']))
            prices[symbol] = price
            value = pos['qty']*price
            positions.append({'symbol': symbol, 'qty': pos['qty'], 'avg_price': pos['avg_price'], 'last_price': price,
                'market_value_usd': value, 'unrealized_pnl_usd': (price-pos['avg_price'])*pos['qty']})
        equity = mark_equity(book, prices)
        return {'broker': self.name, 'status': 'connected (shared-cash paper)', 'custody_location': self.custody_description,
                'cash_usd': book['cash'], 'equity_usd': equity, 'positions_value_usd': equity-book['cash'],
                'net_pnl_usd': equity-book.get('initial_cash', 10000), 'positions': positions, 'recent_orders': book.get('orders', [])[-10:]}


class MultiBrokerRouter:
    """Compatibility boundary for CLI/dashboard; execution is explicitly paper-only."""
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.broker = LocalPaperBroker(self.cfg) if self.cfg.get('execution_provider') == 'local_paper' else AlpacaPaperBroker(self.cfg)

    def resolve_broker_for_symbol(self, symbol):
        if symbol not in self.cfg['watchlist']:
            raise ValueError('Symbol outside reviewed universe: ' + symbol)
        return self.broker

    def get_unified_portfolio(self):
        account = self.broker.get_account_summary()
        account.update(is_active=True)
        return {'accounts': [account], 'symbol_routing': {s: self.broker.name for s in self.cfg['watchlist']},
                'virtual_master_ledger': {'total_equity_usd': account['equity_usd'], 'total_cash_usd': account['cash_usd'],
                    'total_positions_usd': account['positions_value_usd'], 'total_net_pnl_usd': account['net_pnl_usd'], 'active_brokers': [self.broker.name], 'pnl_label': account.get('pnl_label', '累计盈亏')}}


class AlpacaPaperBroker:
    name = 'alpaca'
    custody_description = 'Alpaca Paper Trading · USD · broker-reported balances and fills'

    def __init__(self,cfg):
        from atrader.alpaca import PaperClient
        self.cfg=cfg
        self.client=PaperClient(cfg)

    def get_recent_bars(self,symbol,limit=260):
        return get_recent_bars(symbol,limit,dict(self.cfg,market_data_source='alpaca',market_interval='1d'))

    def get_account_summary(self):
        from atrader.alpaca_execution import order_view
        account=self.client.request('/v2/account')
        positions=self.client.request('/v2/positions')
        orders=self.client.request('/v2/orders?status=closed&limit=500&direction=desc')
        cash,equity=float(account['cash']),float(account['equity'])
        return {'broker':self.name,'status':account['status'],'custody_location':self.custody_description,
            'cash_usd':cash,'equity_usd':equity,'positions_value_usd':sum(float(p['market_value']) for p in positions),
            'net_pnl_usd':equity-float(account['last_equity']),'pnl_label':'当日盈亏（Alpaca）',
            'positions':[{'symbol':p['symbol'],'qty':float(p['qty']),'avg_price':float(p['avg_entry_price']),
                'last_price':float(p['current_price']),'market_value_usd':float(p['market_value']),
                'unrealized_pnl_usd':float(p['unrealized_pl'])} for p in positions],
            'recent_orders':[order_view(o) for o in orders if float(o.get('filled_qty') or 0)>0],
            'history_coverage':'Alpaca 最近 500 笔已关闭订单；费用未由订单接口提供',
            'trading_blocked':bool(account.get('trading_blocked') or account.get('account_blocked'))}

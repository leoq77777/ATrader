"""Persistent execution audit trail. Dashboard readers never create the database."""
from contextlib import closing
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from atrader.config import STATE_DIR

JOURNAL_FILE = STATE_DIR / 'decisions.sqlite3'


def record_cycle(cycle: Dict[str, Any]) -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(JOURNAL_FILE)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, timestamp INTEGER, payload TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO cycles VALUES (?, ?, ?)',
                   (cycle['cycle_id'], cycle['timestamp'], json.dumps(cycle, ensure_ascii=False)))


def read_cycles(limit: int = 200) -> List[Dict[str, Any]]:
    if not JOURNAL_FILE.exists():
        return []
    with closing(sqlite3.connect(JOURNAL_FILE.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT payload FROM cycles ORDER BY timestamp DESC, rowid DESC LIMIT ?', (limit,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def closed_positions(orders: List[Dict[str, Any]], positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Reconstruct completed paper lots from fills, including fees and closure reasons."""
    active = {(p['broker'], p['symbol']) for p in positions}
    lots: Dict[Any, Dict[str, Any]] = {}
    closed = []
    for order in sorted(orders, key=lambda o: o.get('timestamp', 0)):
        key = (order.get('broker', 'local_paper'), order['symbol'])
        lot = lots.setdefault(key, {'qty': 0.0, 'cost': 0.0, 'realized': 0.0, 'complete': True})
        qty, price = float(order.get('qty', 0)), float(order.get('price', 0))
        fee = float(order.get('fee_usd', 0))
        if order.get('side') == 'BUY':
            if lot['qty'] <= 1e-5:
                lot.update(qty=0.0, cost=0.0, realized=0.0, complete=True, opened_at=order.get('timestamp'))
            lot['qty'] += qty
            lot['cost'] += qty * price + fee
        elif order.get('side') == 'SELL':
            average = lot['cost'] / lot['qty'] if lot['qty'] > 1e-5 else 0
            if qty > lot['qty'] + 1e-5:
                lot['complete'] = False
            lot['realized'] += qty * (price - average) - fee
            lot['cost'] = max(0.0, lot['cost'] - qty * average)
            lot['qty'] = max(0.0, lot['qty'] - qty)
            if lot['qty'] <= 1e-5:
                closed.append({
                    'symbol': key[1], 'broker': key[0],
                    'opened_at': lot.get('opened_at'), 'closed_at': order.get('timestamp'),
                    'exit_price': price, 'reason': order.get('reason', ''),
                    'realized_pnl_usd': round(lot['realized'], 2) if lot['complete'] else None,
                    'currently_held': key in active,
                })
                lots.pop(key, None)
    return sorted(closed, key=lambda c: c.get('closed_at') or 0, reverse=True)

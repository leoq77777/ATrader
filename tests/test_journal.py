import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from atrader import journal, ratchet


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'decisions.sqlite3'
        self.patcher = patch.object(journal, 'JOURNAL_FILE', self.db_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_missing_read_is_read_only_and_cycles_survive_reopen(self):
        self.assertEqual(journal.read_cycles(), [])
        self.assertFalse(self.db_path.exists())
        for i in range(3):
            journal.record_cycle({'cycle_id': str(i), 'timestamp': i, 'executions': []})
        self.assertEqual([c['cycle_id'] for c in journal.read_cycles(2)], ['2', '1'])
        journal.record_cycle({'cycle_id': '2', 'timestamp': 2, 'status': 'updated'})
        self.assertEqual(len(journal.read_cycles()), 3)
        self.assertEqual(journal.read_cycles()[0]['status'], 'updated')

    def test_closed_lots_include_fees_partial_sales_and_reentry(self):
        def order(side, qty, price, ts):
            return {'symbol': 'NVDA', 'broker': 'local_paper', 'side': side, 'qty': qty, 'price': price, 'fee_usd': 1, 'timestamp': ts, 'reason': 'trend_reversal_exit'}
        orders = [order('BUY', 10, 100, 1), order('SELL', 4, 110, 2), order('SELL', 6, 120, 3), order('BUY', 2, 130, 4)]
        closed = journal.closed_positions(orders, [{'symbol': 'NVDA', 'broker': 'local_paper'}])
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['realized_pnl_usd'], 157)
        self.assertTrue(closed[0]['currently_held'])
        self.assertEqual(closed[0]['closed_at'], 3)
        self.assertEqual(closed[0]['reason'], 'trend_reversal_exit')
        self.assertEqual(journal.closed_positions([order('SELL', 5, 110, 1)], [])[0]['realized_pnl_usd'], None)


if __name__ == '__main__':
    unittest.main()

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from atrader import market_data, brokers, daemon
from atrader.config import DEFAULT_CONFIG
from copy import deepcopy


class MarketDataTest(unittest.TestCase):
    def setUp(self):
        market_data._CACHE.clear()
        self.addCleanup(market_data._CACHE.clear)

    def payload(self, now=100000):
        return {'chart': {'result': [{'meta': {'regularMarketTime': now, 'currentTradingPeriod': {'regular': {'start': now-3600, 'end': now+3600}}}, 'timestamp': [now-3600, now], 'indicators': {'quote': [{'open': [100, 101], 'high': [101, 102], 'low': [99, 100], 'close': [100, 101], 'volume': [10, 20]}]}}]}}

    def test_cache_expires_and_quotes_advance_without_synthetic_fallback(self):
        def response(*args, **kwargs):
            return io.StringIO(json.dumps(self.payload(int(market_data.time.time()))))
        with patch.object(market_data.time, 'time', return_value=100000) as clock, patch.object(market_data.urllib.request, 'urlopen', side_effect=response) as request:
            first = market_data.get_recent_bars('SPY', 2, {})
            market_data.get_recent_bars('SPY', 1, {})
            self.assertEqual(request.call_count, 1)
            clock.return_value = 100061
            later = market_data.get_recent_bars('SPY', 2, {})
            self.assertGreater(later[-1]['market_timestamp'], first[-1]['market_timestamp'])
        market_data._CACHE.clear()
        with patch.object(market_data.urllib.request, 'urlopen', side_effect=OSError('offline')):
            with self.assertRaisesRegex(OSError, 'offline'):
                market_data.get_recent_bars('SPY', 2, {})

    def test_stale_open_market_quote_rejected(self):
        with patch.object(market_data.time, 'time', return_value=800000), patch.object(market_data.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(self.payload()))):
            with self.assertRaisesRegex(RuntimeError, 'Stale'):
                market_data.get_recent_bars('SPY', 2, {})

    def test_daily_source_marks_partial_bar_and_includes_dividend(self):
        now = 1792072800  # US regular session
        payload = self.payload(now)
        row = payload['chart']['result'][0]
        row['timestamp'] = [now-86400, now]
        row['events'] = {'dividends': {'x': {'date': now-86400, 'amount': .5}}}
        with patch.object(market_data.time, 'time', return_value=now), patch.object(market_data.urllib.request, 'urlopen', return_value=io.StringIO(json.dumps(payload))):
            bars = market_data.get_recent_bars('XLK',2,{'market_interval':'1d'})
        self.assertTrue(bars[0]['complete'])
        self.assertFalse(bars[1]['complete'])
        self.assertEqual(bars[0]['dividend'],.5)


class BrokerTest(unittest.TestCase):
    def test_corrupt_ledger_fails_instead_of_recreating_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'paper.json';path.write_text('corrupt')
            with patch.object(brokers,'PAPER_LEDGER_FILE',path),patch.object(brokers,'ensure_state_dir'):
                with self.assertRaises(ValueError):brokers.LocalPaperBroker({})._ensure_ledger()
                self.assertEqual(path.read_text(),'corrupt')


class SchedulerTest(unittest.TestCase):
    def test_logs_not_lost_and_duplicate_daily_checks_are_silent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(daemon,'STATE_DIR',Path(tmp)),patch.object(daemon,'DAEMON_STATE_FILE',Path(tmp)/'daemon.json'),patch.object(daemon,'load_config',return_value={'ratchet_mode':'code_ratchet','watchlist':['XLK']}),patch.object(daemon,'run_live_trading_cycle',return_value={'status':'already_rebalanced','executions':[]}),patch.object(daemon,'run_code_ratchet_step') as research:
            instance=daemon.AutonomousDaemon();instance._update_state(last_evolve_ts=100000)
            instance.add_log('TEST','keep me')
            instance._tick(100000)
            self.assertEqual(instance.get_state()['recent_logs'][-1]['msg'],'keep me')
            research.assert_not_called()


class ReadOnlyAccountTest(unittest.TestCase):
    def test_dashboard_account_read_does_not_initialize_funds(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(brokers,'PAPER_LEDGER_FILE',Path(tmp)/'paper.json'),patch.object(brokers,'ensure_state_dir'):
            summary=brokers.LocalPaperBroker({}).get_account_summary()
            self.assertEqual(summary['status'],'not_initialized')
            self.assertFalse((Path(tmp)/'paper.json').exists())


class CliSessionTest(unittest.TestCase):
    def test_closed_or_already_rebalanced_session_does_not_require_order_payload(self):
        from atrader import cli
        for status in ('market_closed','already_rebalanced'):
            with self.subTest(status=status),patch.object(cli,'run_live_trading_cycle',return_value={'status':status,'executions':[]}),patch('builtins.print'):
                cli.cmd_trade(loop=False,interval=60)

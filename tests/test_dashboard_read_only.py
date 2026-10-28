import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

from atrader.web import ATraderDashboardHandler, DASHBOARD_HTML


class ReadOnlyDashboardTest(unittest.TestCase):
    def test_dashboard_and_api_allow_reading_but_reject_all_actions(self):
        with patch('atrader.web.collect_dashboard_state', return_value={'daemon': {'is_running': True}}) as collect:
            server = HTTPServer(('127.0.0.1', 0), ATraderDashboardHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(base + '/') as response:
                    self.assertIn('只读看板', response.read().decode())
                with urllib.request.urlopen(base + '/api/state') as response:
                    self.assertTrue(json.load(response)['daemon']['is_running'])
                for route in ('daemon/toggle', 'daemon/start', 'daemon/stop', 'trade', 'evolve', 'signal', 'backtest', 'mode'):
                    with self.subTest(route=route):
                        request = urllib.request.Request(base + '/api/' + route, data=b'{}', method='POST')
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            urllib.request.urlopen(request)
                        self.assertEqual(caught.exception.code, 405)
                        self.assertEqual(json.load(caught.exception)['error'], 'read_only')
                self.assertEqual(collect.call_count, 1)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_page_has_no_mutating_controls(self):
        for control in ('<button', "method: 'POST'", 'triggerAction', 'changeMode', 'toggleAutoPilot', '点击暂停', '立即调仓'):
            self.assertNotIn(control, DASHBOARD_HTML)


if __name__ == '__main__':
    unittest.main()

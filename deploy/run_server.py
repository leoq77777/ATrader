"""Run the dashboard and paper-trading loop in one managed process."""
from atrader.daemon import daemon_instance
from atrader.web import run_web_server

if __name__ == "__main__":
    daemon_instance.start(trade_interval=60, evolve_interval=1800)
    try:
        run_web_server(host="127.0.0.1", port=18787)
    finally:
        daemon_instance.stop()

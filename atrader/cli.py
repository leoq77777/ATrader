"""
ATrader User-Friendly CLI
=========================
Commands:
  python3 main.py daemon              -> Start autonomous dual-ratchet loop (Auto-pilot research & trading)
  python3 main.py run                 -> Alias for daemon
  python3 main.py init                -> Initialize `.env`, baseline backtest, and ratchet state
  python3 main.py status              -> Show current ratchet score, active strategy, and portfolio summary
  python3 main.py backtest            -> Run Walk-Forward evaluation on `strategy.py`
  python3 main.py evolve [--steps N]  -> Run N steps of Code Ratchet (mutate -> test -> keep/revert)
  python3 main.py signal              -> Run Low-Frequency Signal Ratchet & update Shadow Trust weights
  python3 main.py trade [--loop]      -> Execute 0-Token live/paper trading step across active brokers
  python3 main.py portfolio           -> Inspect Virtual Master Ledger & multi-broker custody status
  python3 main.py ui [--port 8787]    -> Launch interactive Web Dashboard
"""

import argparse
import json
import shutil
import time
from typing import List, Optional

from atrader.config import PROJECT_ROOT, ENV_FILE, load_config, save_config, ensure_state_dir
from atrader.evaluator import evaluate_strategy
from atrader.ratchet import (
    get_ratchet_history,
    run_code_ratchet_step,
    run_signal_ratchet_update,
    run_live_trading_cycle,
)
from atrader.brokers import MultiBrokerRouter
from atrader.daemon import daemon_instance
from atrader.web import run_web_server


def cmd_init() -> None:
    ensure_state_dir()
    example_env = PROJECT_ROOT / ".env.example"
    if not ENV_FILE.exists() and example_env.exists():
        shutil.copyfile(example_env, ENV_FILE)
        print("Created `.env` from `.env.example` (fill in your DeepSeek & Broker keys anytime).")
    else:
        print("`.env` already exists.")

    save_config({})
    hist = get_ratchet_history()
    sig = run_signal_ratchet_update()
    router = MultiBrokerRouter()
    port = router.get_unified_portfolio()

    print("\n=== ATrader Initialized (The Karpathy Ratchet Model) ===")
    print(f"Active Strategy       : {hist['best_strategy_name']}")
    print(f"Baseline Ratchet Score: {hist['best_score']}")
    print(f"Signal Regimes Ready  : {', '.join(sig['regimes'].keys())}")
    print(f"Master Ledger Equity  : ${port['virtual_master_ledger']['total_equity_usd']:,.2f}")
    print("\nNext steps:")
    print("  1. Run `python3 main.py daemon` to launch autonomous 24/7 research & trading")
    print("  2. Run `python3 main.py ui` to open the Web Dashboard at http://127.0.0.1:8787")


def cmd_daemon(trade_interval: int = 60, evolve_interval: int = 1800, budget: float = 0.50) -> None:
    print("\n" + "=" * 76)
    print(" 🤖 ATrader Autonomous Ratchet Auto-Pilot (无人值守双棘轮) 🤖")
    print("=" * 76)
    print(f" - 交易调仓循环 : 每 {trade_interval} 秒检查一次行情与持仓 (0 运行时 Token)")
    print(f" - 策略研究循环 : 每 {evolve_interval} 秒 (或盘后) 调用一次 LLM 演化 strategy.py")
    print(f" - 日算力保护栏 : 每日最多消耗 ${budget:.2f} 大模型费用 (超出自动休眠代码演化)")
    print(" - 退出方式     : 按 Ctrl+C 安全停止")
    print("=" * 76 + "\n")

    daemon_instance.start(trade_interval=trade_interval, evolve_interval=evolve_interval)

    try:
        while True:
            state = daemon_instance.get_state()
            logs = state.get("recent_logs", [])
            latest_msg = logs[-1]["msg"] if logs else "Running..."
            print(
                f"\r[Auto-Pilot] Trade in: {state.get('next_trade_in_sec', 0):>2d}s | "
                f"Evolve in: {state.get('next_evolve_in_sec', 0):>4d}s | "
                f"Spent: ${state.get('today_llm_spent_usd', 0.0):.4f}/${budget:.2f} | {latest_msg[:42]}",
                end="",
                flush=True,
            )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\nStopping Auto-Pilot...")
        daemon_instance.stop()
        print("Auto-Pilot successfully paused.")


def cmd_backtest() -> None:
    res = evaluate_strategy()
    print("\n=== Walk-Forward Strategy Evaluation (70% Train / 30% Out-of-Sample) ===")
    print(f"Strategy Name         : {res['strategy_name']}")
    print(f"Karpathy Ratchet Score: {res['ratchet_score']} (Train: {res['train_score']} | Val: {res['validation_score']})")
    print(f"Avg Annualized Sharpe : {res['avg_sharpe']}")
    print(f"Avg Net Return        : +{res['avg_return_pct']}% (Net Profit: ${res['net_profit_usd']:,.2f})")
    print(f"Avg Max Drawdown      : -{res['avg_max_drawdown_pct']}%")
    print(f"Trading Fees Deducted : ${res['total_trading_fees_usd']:,.2f}")
    print("\nPer-Symbol Out-of-Sample Validation Breakdown:")
    for sym, detail in res["symbols"].items():
        v = detail["validation"]
        print(
            f"  - {sym:<10} | Val Sharpe: {v['sharpe']:>5.2f} | Val Return: {v['net_return_pct']:>+6.2f}% "
            f"| Alpha: {v['alpha_pct']:>+6.2f}% | MaxDD: -{v['max_drawdown_pct']:>5.2f}% | Trades: {v['trade_count']}"
        )


def cmd_evolve(steps: int, mock: bool) -> None:
    print(f"\n=== Running Karpathy Code Ratchet ({steps} step(s)) ===")
    out = run_code_ratchet_step(steps=steps, force_mock=mock)
    for step in out["steps_executed"]:
        print(
            f"[Gen #{step['generation']}] {step['status']} | Score: {step['ratchet_score']} "
            f"(prev best: {step['baseline_score_before']}) | Sharpe: {step['avg_sharpe']} | "
            f"Ret: +{step['avg_return_pct']}% | DD: -{step['avg_max_drawdown_pct']}% | Cost: ${step['llm_cost_usd']:.5f}"
        )
        print(f"   Hypothesis: {step['hypothesis']}")
    print(f"\nCurrent Locked Best Strategy: {out['best_strategy_name']} (Score: {out['best_score']})")


def cmd_signal() -> None:
    print("\n=== Updating Low-Frequency Signal Ratchet & Shadow Trust Weights ===")
    out = run_signal_ratchet_update()
    for sym, reg in out["regimes"].items():
        print(
            f"  - {sym:<10} | Bias: {reg['trend_bias']:>+5.2f} | Risk Scale: {reg['risk_scale']:.2f}x "
            f"| Shadow Trust Weight: {int(reg['trust_weight']*100):>2d}% | {reg['thesis']}"
        )


def cmd_trade(loop: bool, interval: int) -> None:
    while True:
        out = run_live_trading_cycle()
        print(f"\n=== Live/Paper Execution Cycle (Strategy: {out['strategy_name']} | Runtime Tokens: 0) ===")
        for ex in out["executions"]:
            ord_status = ex["order_result"].get("status", "unknown")
            print(
                f"  - {ex['symbol']:<10} -> [{ex['routed_broker']}] | Price: ${ex['price']:,.2f} "
                f"| Target Weight: {ex['target_weight']*100:>5.1f}% | Order: {ord_status} ({ex['reason']})"
            )
        vml = out["portfolio"]["virtual_master_ledger"]
        print(
            f"Virtual Master Ledger: Equity=${vml['total_equity_usd']:,.2f} | "
            f"Cash=${vml['total_cash_usd']:,.2f} | Positions=${vml['total_positions_usd']:,.2f}"
        )
        if not loop:
            break
        time.sleep(max(5, interval))


def cmd_portfolio() -> None:
    router = MultiBrokerRouter()
    port = router.get_unified_portfolio()
    vml = port["virtual_master_ledger"]
    print("\n=== Multi-Broker Virtual Master Ledger & Custody Map ===")
    print(f"Total Unified Equity  : ${vml['total_equity_usd']:,.2f}")
    print(f"Total Unified Cash    : ${vml['total_cash_usd']:,.2f}")
    print(f"Total Unified Net PnL : ${vml['total_net_pnl_usd']:+,.2f}")
    print("\nCustody Accounts Breakdown:")
    for acct in port["accounts"]:
        print(
            f"  * [{acct['broker']:<11}] Status: {acct['status']}\n"
            f"    CustodyWhere : {acct['custody_location']}\n"
            f"    Equity: ${acct['equity_usd']:,.2f} | Cash: ${acct['cash_usd']:,.2f} | Allocation Cap: ${acct['capital_cap_usd']:,.2f}"
        )


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="atrader",
        description="ATrader — Minimalist Closed-Loop Ratchet Agent Trader (The Karpathy Ratchet Model)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize configuration, baseline evaluation, and local paper ledger")
    sub.add_parser("status", help="Display current ratchet status and portfolio overview")
    sub.add_parser("backtest", help="Run walk-forward backtest on strategy.py")

    p_daemon = sub.add_parser("daemon", help="Run autonomous 24/7 research & trading daemon")
    p_daemon.add_argument("--trade-interval", type=int, default=60, help="Seconds between trading cycles")
    p_daemon.add_argument("--evolve-interval", type=int, default=1800, help="Seconds between strategy evolution cycles")
    p_daemon.add_argument("--budget", type=float, default=0.50, help="Daily LLM spend limit in USD")

    sub.add_parser("run", help="Alias for daemon")

    p_evolve = sub.add_parser("evolve", help="Run Karpathy Code Ratchet steps to evolve strategy.py")
    p_evolve.add_argument("--steps", type=int, default=1, help="Number of evolution generations to run")
    p_evolve.add_argument("--mock", action="store_true", help="Force local heuristic mutation ($0 API cost)")

    sub.add_parser("signal", help="Run Low-Frequency Signal Ratchet update")

    p_trade = sub.add_parser("trade", help="Execute 0-token trading cycle across configured brokers")
    p_trade.add_argument("--loop", action="store_true", help="Continuously run trading loop")
    p_trade.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")

    sub.add_parser("portfolio", help="View Multi-Broker Virtual Master Ledger & custody locations")

    p_ui = sub.add_parser("ui", help="Start interactive Web Dashboard")
    p_ui.add_argument("--host", type=str, default="127.0.0.1", help="Host address")
    p_ui.add_argument("--port", type=int, default=8787, help="Port number")

    args = parser.parse_args(argv)

    if args.command in ("daemon", "run"):
        t_int = getattr(args, "trade_interval", 60)
        e_int = getattr(args, "evolve_interval", 1800)
        b = getattr(args, "budget", 0.50)
        cmd_daemon(trade_interval=t_int, evolve_interval=e_int, budget=b)
    elif args.command == "init":
        cmd_init()
    elif args.command == "backtest":
        cmd_backtest()
    elif args.command == "evolve":
        cmd_evolve(steps=args.steps, mock=args.mock)
    elif args.command == "signal":
        cmd_signal()
    elif args.command == "trade":
        cmd_trade(loop=args.loop, interval=args.interval)
    elif args.command == "portfolio":
        cmd_portfolio()
    elif args.command == "status":
        cmd_backtest()
        cmd_portfolio()
    elif args.command == "ui":
        run_web_server(host=args.host, port=args.port)
    else:
        parser.print_help()

# ATrader — 极简闭环棘轮量化交易系统 (The Karpathy Ratchet Model)

ATrader 是一个受 Andrej Karpathy `autoresearch` 极简闭环思想启发构建的轻量级 Agent Trader。它通过 **“模型演化与确定性执行解耦 + 单向棘轮锁定（Keep if strictly better, Revert if worse）”**，在 **Model Cost（大模型 Token 成本）** 与 **Strategy Profit（策略净收益）** 之间取得最优平衡，并提供开箱即用的 **CLI + 单页 Web 监控看板**。

## 1. 核心架构：双模式与混合棘轮 (Code Ratchet + Signal Ratchet)

系统内置三种可一键切换的棘轮模式（默认启用 `hybrid` 混合棘轮）：

| 棘轮模式 | 运作机制 | Token 消耗与适用场景 |
| :--- | :--- | :--- |
| **`code_ratchet`（策略代码演化棘轮）** | Agent 读取唯一可变策略文件 [`strategy.py`](./strategy.py) 与历史回测弱点，调用 [DeepSeek API](https://api-docs.deepseek.com/) 生成新策略代码；由不可篡改的评估器 [`atrader/evaluator.py`](./atrader/evaluator.py) 进行 70% 训练集 + 30% 样本外验证集回测（扣除手续费、滑点与 LLM 调用成本）。得分严格优于历史最优才锁定保存（`LOCKED`），否则立即回滚（`REVERTED`）。 | **实盘盯盘 0 Token**；仅在盘后/定周期演化时消耗极少 Token，计算性价比最高 |
| **`signal_ratchet`（低频信号与影子信任棘轮）** | Agent 仅在低频周期（如 4 小时 / 日线）读取压缩后的多标的指标摘要（约 250 Tokens），输出市场趋势偏置（`trend_bias`）与风险缩放（`risk_scale`）。内置 **影子账本（Shadow Ledger）** 追踪历史信号胜率，自动调升或降低信号信任权重（`trust_weight`）。 | 适合捕捉宏观与跨市场状态切换，用影子盘胜率防止大模型幻觉直接亏损实盘本金 |
| **`hybrid`（双层混合棘轮，默认推荐）** | 底层由 [`strategy.py`](./strategy.py) 0-Token 毫秒级执行日内/波段择时与 ATR 移动止损；顶层叠加 `signal_ratchet` 的低频市场状态乘数（按 `trust_weight` 动态加权）。 | 兼具 0-Token 高频风控执行与低频大模型宏观感知能力 |

## 2. 多交易 API 实盘资金存管与路由机制 (Virtual Master Ledger)

**交易 API 只是加密指令通道，本身绝不托管资金**。当启用多个交易接口时，你的实盘资金始终安全存放在你本人在各合规券商/交易所开设的独立账户中，由 [`atrader/brokers.py`](./atrader/brokers.py) 中的 `MultiBrokerRouter`（虚拟总账本）统一调度与限额风控：

- **本地模拟盘 (`local_paper`)**：默认激活，账本保存在 `state/paper_account.json`，无需入金即可跑通完整棘轮与调仓闭环。
- **美股与加密聚合账户 ([Alpaca Trading API](https://docs.alpaca.markets/us/docs/trading-api))**：若希望**所有实盘资金放在同一个账户**同时交易美股（`NVDA`、`SPY`）与主流加密货币（`BTC/USD`、`ETH/USD`），推荐只入金 Alpaca 一家券商账户，免去跨平台划转资金。
- **全币种加密交易所 ([CCXT](https://docs.ccxt.com/))**：支持 Binance、OKX、Bybit、Coinbase 等 100+ 交易所，实盘 USDT/USDC 存放在对应交易所现货/统一账户。
- **港美股券商账户 ([Longbridge OpenAPI](https://open.longbridge.com/docs/getting-started))**：支持港股（如 `00700.HK`）与美股，实盘港币/美元存放在长桥证券账户。

## 3. 60 秒快速上手 (Zero-Dependency Quickstart)

项目核心运行库完全基于 Python 3 标准库实现，**无需预装任何第三方依赖即可直接启动 CLI 与 Web 看板**（连接实盘 SDK 时可按需执行 `pip install -r requirements.txt`）。

```bash
# 1. 初始化配置文件 (.env)、基准策略回测与本地模拟账本
python3 main.py init

# 2. 配置你的 DeepSeek API Key (未配置时自动使用内置启发式演化器免费体验)
# 编辑 .env 填入: LLM_API_KEY=sk-...

# 3. 运行 3 轮 Karpathy 代码棘轮演化 (自动演示 LOCKED 晋级与 REVERTED 回滚)
python3 main.py evolve --steps 3

# 4. 更新低频市场状态信号棘轮 (Signal Ratchet)
python3 main.py signal

# 5. 执行一轮 0-Token 模拟/实盘自动调仓并查看多账户虚拟总账本
python3 main.py trade
python3 main.py portfolio

# 6. 启动交互式单页 Web 看板 (默认 http://127.0.0.1:8787)
python3 main.py ui --port 8787
```

## 4. 目录结构

- [`main.py`](./main.py)：统一命令行入口。
- [`strategy.py`](./strategy.py)：**唯一可变策略文件**（由棘轮引擎自动演化或回滚，内置纯 Python 指标库）。
- [`atrader/evaluator.py`](./atrader/evaluator.py)：**不可变裁判引擎**（样本内外分段回测、手续费/滑点/Token 成本扣除与 `ratchet_score` 计算）。
- [`atrader/ratchet.py`](./atrader/ratchet.py)：双模棘轮控制器（支持 `code_ratchet`、`signal_ratchet` 与 `hybrid`）。
- [`atrader/brokers.py`](./atrader/brokers.py)：四大交易通道适配器（`local_paper`、`alpaca`、`ccxt`、`longbridge`）及多账户虚拟总账本。
- [`atrader/web.py`](./atrader/web.py)：零依赖响应式单页 Web 监控看板与 REST API。
- [`.env.example`](./.env.example)：DeepSeek API 与多交易接口配置模板。

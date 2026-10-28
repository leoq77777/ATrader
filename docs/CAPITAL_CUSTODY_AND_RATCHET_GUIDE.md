> 历史设计说明：本页包含已过时的多券商和本地模拟设想，不代表当前已启用能力。后续工程边界以[投资实验架构定稿](AGENT_HARNESS_ARCHITECTURE.md)为准；当前仅连接 Alpaca Paper，完整 Paper 验收与回滚尚未落地。

> 历史设计文档：以下多券商自动路由说明已退役。当前版本仅运行美股行业 ETF 的共享资金模拟盘；以根目录 README 和 deploy/README.md 为准。IBKR/实盘接入尚未实现。

# ATrader 架构指南：两种棘轮模式对比与多交易 API 实盘资金存管方案

## 一、两种棘轮模式（Code Ratchet vs. Signal Ratchet）谁更好？

在量化 Agent 交易中，**大模型推理成本（Model Cost）** 与 **策略净收益（Strategy Profit）** 的核心矛盾在于：金融行情是高频变化的（秒级/分钟级），而 LLM 每次推理既慢（数秒延迟）又消耗 Token。

### 1. 深度对比表

| 维度 | 模式 A：策略代码演化棘轮 (`code_ratchet`) | 模式 B：低频信号决策棘轮 (`signal_ratchet`) | 模式 C：双层混合棘轮 (`hybrid`，默认启用) |
| :--- | :--- | :--- | :--- |
| **核心定位** | **LLM 扮演“盘后量化研究员”** | **LLM 扮演“低频宏观/波段交易员”** | **LLM 低频定方向 + 纯 Python 代码毫秒级执行** |
| **运作机制** | LLM 定期修改 [`strategy.py`](../strategy.py) 源码 → [`evaluator.py`](../atrader/evaluator.py) 跑样本内外回测（扣除手续费与 Token 成本）→ 得分严格提升则锁定（`LOCKED`），退化则自动回滚（`REVERTED`） | LLM 在 4h / 1d 收盘时读取极简指标摘要（~250 Tokens），输出趋势偏置 `trend_bias` 与风险系数 `risk_scale`；由**影子账本（Shadow Ledger）**验证胜率后动态调整信任权重 `trust_weight` | 底层用 `code_ratchet` 锁定的最优 [`strategy.py`](../strategy.py) 执行 0-Token 盯盘与止损；顶层用 `signal_ratchet` 的 `trust_weight` 缩放总仓位 |
| **实盘盯盘 Token 消耗** | **0 Token**（纯 Python 确定性执行） | 极低（每 4 小时仅调用 1 次，每日约 $0.002） | **盯盘 0 Token** + 每日仅需极少低频状态更新 Token |
| **风控可靠性** | 极高（止损、仓位计算全部经过历史回测验证，无实盘幻觉风险） | 中等（依赖影子账本 `trust_weight` 过滤错误信号） | **最高**（硬代码风控兜底 + 软信号动态加减仓） |

### 2. 核心设计建议

- **为什么单选推荐 `code_ratchet`，综合推荐 `hybrid`？**
  - 如果让 LLM 直接在盘中决定买卖点，一旦遇到极端插针行情，API 延迟或偶发幻觉就会导致严重亏损，且高频调用 [DeepSeek API](https://api-docs.deepseek.com/) 也会累积不必要成本。
  - 采用 **`code_ratchet`（Karpathy `autoresearch` 范式）** 时，LLM 的智慧被固化进 [`strategy.py`](../strategy.py) 的数学公式与风控参数里——**一次几美分的 Token 投入，演化出的优秀策略可以 0 成本无限次实盘运行**。
  - 当开启 **`hybrid` 模式**（项目默认配置）时，还能在每日/每 4 小时利用 [DeepSeek API](https://api-docs.deepseek.com/) 的推理能力判断大级别市场状态（顺势放大仓位、逆势收缩风险），且新信号必须先在影子账本证明准确率才能获得高权重（`trust_weight`）。

## 二、同时使用多个交易 API 时，实盘资金应该存放在哪里？

### 1. 基本原则：API 只是加密指令通道，资金永远在你名下的合规机构账户内

无论你在 `.env` 中开启了多少个交易 API，**ATrader 程序本身不触碰、不托管任何资金**，也不存在所谓的“第三方中间资金池”。你的实盘资金始终存放在你本人在对应券商或交易所实名开设的账户里：

| 交易接口通道 | 适用交易标的 | 你的实盘资金实际存放位置（托管方） | API 权限安全边界 |
| :--- | :--- | :--- | :--- |
| **`local_paper`** | 全市场模拟验证 | 本地沙盒账本 [`state/paper_account.json`](../state/paper_account.json)（无需真实资金） | 纯本地模拟撮合，0 资金风险 |
| **[Alpaca Trading API](https://docs.alpaca.markets/us/docs/trading-api)** | 美股、ETF、主流加密货币 | 你在 Alpaca Securities LLC 开设的受美股 SIPC 保护的美元证券账户 | 仅授予 `Trading` 权限，**禁用提现（Withdrawal）** |
| **[CCXT](https://docs.ccxt.com/)** | 加密货币现货/合约（`BTC/USDT` 等） | 你在对应交易所（如 Binance / OKX / Coinbase）的个人现货或统一交易账户（存放 USDT / USDC） | 仅勾选 `Spot/Margin Trading`，**严禁勾选 Enable Withdrawals（允许提币）**与绑定 IP 白名单 |
| **[Longbridge OpenAPI](https://open.longbridge.com/docs/getting-started)** | 港股、美股 | 你在长桥证券（Longbridge）开设的持牌港美股证券账户（存放 USD / HKD） | 仅用于行情订阅与证券交易指令下发 |

### 2. 避免多平台资金碎片化的三套实操方案

1. **方案一：单账户跨资产聚合（最省心，适合希望所有钱放在一处的用户）**
   - 如果你希望同时交易 **美股/ETF（如 `NVDA`、`SPY`）+ 主流加密货币（如 `BTC/USD`、`ETH/USD`）**，且不想把资金拆散在多个平台，可以直接将美元资金全部存放在 **[Alpaca](https://alpaca.markets/) 同一个账户内**。Alpaca 支持用同一个美元余额池同时买卖美股与 Crypto。
2. **方案二：法币归券商、稳定币归交易所 + ATrader 虚拟总账本（适合全品种玩家）**
   - 由于传统证券清算网络（USD/HKD）与加密交易所（USDT）物理隔离，若你需要交易币安/OKX 上的海量加密代币以及港美股：
     - 将 **法币（USD/HKD）** 存放在 [Alpaca](https://docs.alpaca.markets/us/docs/trading-api) 或 [Longbridge](https://open.longbridge.com/docs/getting-started)；
     - 将 **稳定币（USDT/USDC）** 存放在 [CCXT](https://docs.ccxt.com/) 连接的加密交易所。
   - ATrader 在 [`atrader/brokers.py`](../atrader/brokers.py) 中内置了 **`MultiBrokerRouter`（虚拟总账本 Virtual Master Ledger）**：
     - 自动将 `BTC/USDT` 路由至 `ccxt`，将 `NVDA`/`SPY` 路由至 `alpaca`，将 `00700.HK` 路由至 `longbridge`；
     - 在 Web 看板和 `python3 main.py portfolio` 中自动汇总所有账户的折合美元总净值，并通过 `broker_allocations` 为每个通道设置独立的资金上限（`capital_cap_usd`）与最大回撤熔断线，无需频繁跨平台搬砖。
3. **方案三：先跑免费模拟盘赛马，再向胜出市场入金**
   - 初期无需向任何实盘账户入金，直接使用默认的 `local_paper` + Alpaca Paper Trading + 交易所 Testnet 跑 1~2 周棘轮演化，观察哪个品种/市场的样本外净收益最高，再把实盘资金集中存放在该主力通道对应的账户中。

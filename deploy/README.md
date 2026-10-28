# Ubuntu 部署与行业轮动迁移

默认部署目录 `/opt/atrader`；systemd 服务 `atrader`；HTTPS 入口 `https://YOUR_SERVER_IP`。安装前将 Nginx 模板中的 `YOUR_SERVER_IP` 替换为自己的域名或 IP。Nginx 443 提供 TLS 和登录保护，代理至 `127.0.0.1:18787`；使用 IP 时需自行配置合适的证书。

当前执行与风控观察模式见[架构定稿第 11 节](../docs/AGENT_HARNESS_ARCHITECTURE.md#11-本次上线范围执行与风控观察2026-10-27)。下文的旧版迁移步骤保留供历史部署参考，不是新安装流程；新安装不要运行旧本地模拟盘迁移脚本。

## 启动结构

将 `deploy/run_server.py` 复制到根目录作为 `run_server.py`，安装 `deploy/atrader.service` 到 `/etc/systemd/system/atrader.service`。进程内只有一个调度器和只读 HTTP 看板；候选策略使用短生命周期、无凭据的受限子进程。每日只在美股正常交易时段进行一次组合调仓。

## 从旧独立标的回测迁移

1. 停止服务，备份 `atrader/`、`strategy.py`、`state/`、`.env` 和启动脚本。
2. 更新应用代码及经过审查的新 `strategy.py`；旧策略只支持单标的接口，需归档后替换为 `rank_assets` 接口。
3. 执行 `python3 deploy/migrate_portfolio.py`。保留 LLM 配置，固定为美股非杠杆行业 ETF、日线、模拟执行；建立新组合基准并生成未执行排名预览。
4. 原市场行情模拟账本的现金与持仓不重置。范围外仓位在下一次正常交易时段仅执行退出。旧研究与成交保留，新规则重新评分。
5. 运行 `python3 -m unittest discover -s tests -v`，确认行情与回测预检通过后重启服务。

重复迁移不应重置账本；检查 `ratchet_history.json` 的 `evaluation_policy` 为 `shared_portfolio_daily_v1`。仅在临时验证目录可用 `--configure-only` 配置而不执行完整迁移。

失败时停服，恢复整套备份的代码、策略、状态和 `.env` 后重启。不要只恢复策略而保留不兼容的 harness。迁移前的合成行情阶段账本仍在 `state/archives/`。

## 运维

```bash
ssh remote 'systemctl status atrader --no-pager'
ssh remote 'sudo journalctl -u atrader -n 100 --no-pager'
ssh remote 'sudo systemctl restart atrader'
ssh remote 'sudo systemctl stop atrader'
```

登录凭据在 `/opt/atrader/dashboard-login.txt`（600）；认证哈希在 `/etc/nginx/atrader.htpasswd`；证书在 `/etc/nginx/atrader-tls/`。证书需在到期前续期。

备份时包括 `state/decisions.sqlite3`、`state/datasets/`、研究快照和账本；不要把密钥放入发布包。IBKR 账户接入和真实订单均未实现；配置其他券商凭据不会自动启用交易。

## 多路径研究升级

只发布 `atrader/`、测试和文档，保留服务器自主演化后的 `strategy.py`、`.env`、账本、SQLite 及全部研究历史；本地策略不能覆盖线上策略。先在临时发布目录跑测试，再停服、完整备份、覆盖代码并验证当前策略可被新 worker 运行。首轮研究会初始化冻结批次，首次使用真实行情，之后复用已校验的样本。

默认批次限制 120 次候选或 $1 估算费用，每日预算维持 $0.50；到限只停止研究，模拟执行和只读看板继续。看板显示批次、各轮路径、档案接纳结果与预算。`state/research/` 的样本、提示词、档案、试验登记及最终留出文件须随状态一起备份。不要删除该目录来恢复研究；新批次需要审查新的时间样本和协议，并保留旧批次的试验与考试记录。

批次预算耗尽后可通过 SSH 显式执行一次 `python3 main.py audit`。此操作打开最终考试，不是普通健康检查；部署验收不要运行它。回滚使用停服备份，保留升级后产生的研究目录作为审计记录，避免重复计数或覆盖新交易。

## 修复账户提供方：接回 Alpaca Paper Trading

线上账户固定为 Alpaca Paper Trading，凭据从用户已提供的本地 `.env` 通过 SSH 标准输入传输，不进入发布包或命令参数。先停服并完整备份代码、策略、状态和服务器 `.env`，再仅迁移 Alpaca 三个配置项；禁用本地账本执行。不要把本地回测资金当作 Alpaca 余额。

旧本地账本与旧 paper_epoch 迁入 `state/archives/`，研究批次、成本、策略源码及历史 SQLite 保留。Alpaca 账户不重置、不注资、不人为关闭已有 NVDA 仓位。在线数据固定 Alpaca IEX；当前研究批次仍用已冻结的 Yahoo 数据。部署验收使用账户、持仓、订单和信号预览的只读请求，不提交探针交易。账户配置缺失或连接失败应报错，不回退本地模拟账户。

回滚时先停止服务并核对 Alpaca 未完成挂单。代码与凭据可从停服备份恢复，但不要删除或回退已经产生的 `alpaca_execution.json` 订单意图；这些 ID 是防止重复订单的依据。恢复旧的本地执行版本会重新导致展示错误账户，因此只用于紧急排查，并停止自动交易。

## 活跃研究迁入 Alpaca 数据

停服并备份后，在 `research.lock` 下对当前执行策略与活跃档案按 AST 去重。`rebaseline.prepare` 冻结新数据并重评，`commit` 只有在当前策略有效且原策略/研究历史未变化时发布。评估不会调用模型或提交券商订单；沿用现有风险和评分规则，补入 Alpaca 现金分红，并保留原最终留出日期边界。

提交后将 `.env` 的 MARKET_DATA_SOURCE 与保存配置切换为 alpaca。旧 Yahoo 研究目录与 last_evaluation 归档，新档案使用新分数和持仓行为重建，但继承旧试验次数、成本与预算。旧本地账本标记 ABORTED；不取消、清空或重置 Alpaca 账户。只有整套迁移完成且配置校验通过才重启服务。检查看板显示 Alpaca IEX 历史行情及重新评分明细，确认累计次数未归零、旧成绩仍在历史中。

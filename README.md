# WEEX AutoTrade

[![CI](https://github.com/GrooveSino/weex-autotrade/actions/workflows/ci.yml/badge.svg)](https://github.com/GrooveSino/weex-autotrade/actions/workflows/ci.yml)

面向 WEEX USDT 合约的本地命令行交易工具。默认使用 WEEX Demo，所有写操作默认只生成计划；只有同时提供 `--execute`、完全一致的确认短语，并在实盘模式下显式开启环境开关，命令才会提交到交易所。

## 安全边界

- 默认模式是 `demo`，不会自动切换到实盘。
- 下单、撤单、平仓、杠杆、保证金和风险单都先输出 dry-run。
- 所有 mutation 必须复制 dry-run 输出的精确确认短语。
- 实盘 mutation 还要求 `WEEX_LIVE_TRADING_ENABLED=true`。
- `POST_ONLY` 被拒绝时不会追价，也不会降级为普通限价或市价单。
- 网络错误后不会自动重试下单；CLI 会先按 `clientOrderId` 回读，无法确认时报告“结果未知”。
- 更新止损使用“提交新止损 -> 验证新止损 -> 撤销旧止损”的顺序。
- `.env`、日志、数据库和本地运行产物均被 Git 忽略。

## 环境要求

- macOS 或 Linux
- Python 3.11 以上，建议 3.12
- [uv](https://docs.astral.sh/uv/)
- WEEX API Key、Secret 和 Passphrase

```bash
uv sync --all-groups
cp .env.example .env
chmod +x ./weex
./weex --help
```

编辑 `.env`：

```dotenv
WEEX_API_KEY=
WEEX_API_SECRET=
WEEX_API_PASSPHRASE=
WEEX_WEB_CC_TOKEN=
WEEX_WEB_TERMINAL_CODE=
WEEX_DEFAULT_MODE=demo
WEEX_LIVE_TRADING_ENABLED=false
```

API Key 建议绑定固定出口 IP。新 Key 初期只开启所需的合约权限，不要开启提现权限。CLI 只会从当前目录查找到最近的 Git/`pyproject.toml` 项目根，绝不会越过项目根读取父级或其他项目的 `.env`；也可以用 `--env-file` 显式指定来源。

## 日常使用

主界面只保留日常需要的状态、Maker 和成交量入口：

```bash
# 一屏确认当前环境、仓位、活动订单和凭据状态
./weex status

# 最近 24 小时的交易量；不需要手写时间戳
./weex activity
./weex activity --hours 1 --details

# 使用已实测的默认值生成 dry-run：BTC、10000 SUSDT、10 腿、最大仓位 1200、单腿 120 秒
./weex maker run

# 默认连续运行 3 轮
./weex maker soak

# 自动读取当前 BTC 多仓数量并生成纯 Maker 平仓计划
./weex maker flatten
```

所有 Maker 命令默认只显示简洁计划和精确确认短语。执行时原样传回确认短语：

```bash
./weex maker run \
  --execute \
  --confirm 'EXECUTE WEEX DEMO MAKER VOLUME BTC TARGET_10000 FILLS_10 MAX_POSITION_1200 TIMEOUT_120'
```

需要调整时只覆盖对应参数，例如 `--target 20000`、`--rounds 5` 或 `--max-position 2000`。默认输出面向人阅读；自动化脚本统一加 `--json`。

底层行情、账户、单笔订单、风险单和诊断命令统一放在 `advanced`：

```bash
./weex advanced --help
./weex advanced account positions --mode demo
./weex advanced orders open --mode demo
```

旧的 `weex account ...`、`weex orders ...`、`weex volume ...` 路径继续兼容，但不再挤占主帮助界面。

## 快速检查

```bash
# 配置和 DNS/公共接口检查，不读取私有账户
./weex doctor

# 只读验证 Demo Key
./weex doctor --private --mode demo

# 查看实际加载了哪个 .env，只显示是否已配置，不显示密钥
./weex config show
```

如 `doctor` 显示 WEEX 域名解析异常，应修复系统 DNS、代理或部署机网络。不要把 CloudFront IP 硬编码进 `/etc/hosts` 或项目配置，因为这些地址会变化。

## 行情与账户

```bash
./weex market ticker BTC
./weex market book BTC --limit 5

./weex account balance --mode demo
./weex account positions --mode demo
./weex orders history --mode demo --symbol BTC
./weex orders open --mode demo

./weex account balance --mode live
./weex account positions --mode live --symbol BTC
./weex orders open --symbol BTC
./weex orders open --symbol BTC --trigger
```

Demo 使用 WEEX 的模拟资产和交易对，例如 `SUSDT`、`BTCSUSDT`。CLI 接受 `BTC`、`BTCUSDT`、`BTCSUSDT` 或 `BTC/USDT:USDT`，会按模式转换。

## Maker 下单

先生成计划：

```bash
./weex order plan BTC \
  --mode demo \
  --side buy \
  --position-side long \
  --quantity 0.001 \
  --price 60000 \
  --time-in-force POST_ONLY \
  --take-profit 63000 \
  --stop-loss 58500
```

`order place` 在没有 `--execute` 时同样只输出计划：

```bash
./weex order place BTC \
  --mode demo \
  --side buy \
  --position-side long \
  --quantity 0.001 \
  --price 60000
```

确认计划后，把输出中的 `confirm` 原样传回：

```bash
./weex order place BTC \
  --mode demo \
  --side buy \
  --position-side long \
  --quantity 0.001 \
  --price 60000 \
  --execute \
  --confirm 'EXECUTE WEEX DEMO ORDER BTCSUSDT BUY LONG LIMIT 0.001 60000 POST_ONLY'
```

默认会阻止在已有仓位的交易对上继续开仓；实盘还会同时预检普通挂单。WEEX 官方 V3 Demo API 没有普通挂单查询接口；配置 Web Demo 凭据后，可以用 `orders open --mode demo` 通过未公开的 Web 接口补充检查。只有明确检查过账户状态后才使用 `--allow-existing`。平仓单使用 `--reduce-only`。

## Demo Maker 交易量批处理

在调用交易所前，可先运行完全离线、无需凭据的自适应 Maker 基准。它会在训练种子上搜索参数，随后用独立种子验证至少 5 次完整开平、至少 10 笔 Maker 成交、累计 10,000 USDT、最终空仓，并与固定 5 秒撤单策略比较平均/P50/P95 完成时间：

```bash
./weex volume benchmark \
  --target 10000 \
  --cycles 5 \
  --train-trials 15 \
  --validation-trials 15 \
  --json
```

该命令只运行本地合成盘口和撮合，不读取 API Key、不发网络请求，也不能证明实盘或 Demo 的成交速度。只有 `acceptance` 中全部硬门槛为 `true` 时，结果状态才是 `passed`。

批处理把仓位预检、盘口读取、精度处理、下单和成交轮询放在同一进程中，减少人工复制价格和重复启动 CLI 造成的报价陈旧。它只支持 Demo，并按“开多 -> 平多”交替执行，要求偶数笔成交并以空仓开始和结束。

实际执行使用自适应 Maker 策略：V3 API 负责下单、历史和仓位，未公开的 Demo Web API 负责活动挂单查询与撤单确认。程序强制保持单一活动订单，遵守 10.1 秒 Demo 下单间隔；报价过期时只有在撤单已验证后才允许生成新 client order ID。清理中断批次留下的单一多仓时，先生成独立计划：

```bash
./weex volume flatten BTC \
  --quantity 0.016 \
  --max-position 1200 \
  --timeout 120 \
  --json
```

先生成计划：

```bash
./weex volume maker BTC \
  --target 100000 \
  --fills 10 \
  --max-position 12000 \
  --timeout 120 \
  --json
```

确认计划后执行：

```bash
./weex volume maker BTC \
  --target 100000 \
  --fills 10 \
  --max-position 12000 \
  --timeout 120 \
  --execute \
  --confirm 'EXECUTE WEEX DEMO MAKER VOLUME BTC TARGET_100000 FILLS_10 MAX_POSITION_12000 TIMEOUT_120' \
  --json
```

每笔订单都使用新的 client order ID 和 `POST_ONLY`，只有历史委托确认全额成交且仓位与预期一致后才会进入下一笔。提交结果不确定、部分成交、拒单、取消、仓位不一致或单笔超时都会终止批次；不会自动重提、追价或降级为 Taker。配置 Web Demo 凭据后可通过未公开接口查询并撤销普通挂单；撤单提交后必须回查验证，验证失败时状态为 `uncertain`，不能直接重提。

## 实盘开关

先在 `.env` 保持：

```dotenv
WEEX_LIVE_TRADING_ENABLED=false
```

完成 Demo 验证、IP 白名单和小额人工核对后，再改为：

```dotenv
WEEX_LIVE_TRADING_ENABLED=true
```

实盘依然需要 `--mode live --execute --confirm '...'`，环境开关不会绕过确认短语。

## 仓位与订单管理

以下命令第一次运行都只输出计划：

```bash
./weex account configure BTC --leverage 10 --margin-mode isolated
./weex account close BTC --position-side long
./weex account close-all

./weex orders cancel BTC 123456789
./weex orders cancel-all --symbol BTC
./weex orders cancel-all --symbol BTC --trigger

# Demo 普通挂单：先查询精确 order ID，再生成撤单计划
./weex orders open --mode demo
./weex orders cancel BTC 123456789 --mode demo
```

`account close BTC` 会平掉该交易对的多空仓位；增加 `--position-side LONG` 或 `SHORT` 时，CLI 会先查询对应仓位并使用官方 `positionId` 精确平仓，找不到唯一仓位时拒绝执行。

WEEX 官方 V3 Demo API 目前只公开余额、下单、仓位和历史委托，没有 Demo 撤单、杠杆或独立条件单接口。本项目的 Demo 普通挂单查询、单笔撤单和全撤使用 `http-gateway2.weex.com` 的未公开 Web API，只允许 Demo，且仍要求 `--execute` 和精确确认短语。该接口不受官方兼容性承诺，可能随前端更新变化；失败时不会回退到实盘接口。Demo `cancel-all` 不接受交易对过滤，以免错误映射 `contractId` 后误撤其他订单；需要按交易对处理时先查询并逐个撤销精确 order ID。

Web Demo 接口使用网页登录会话，不使用 V3 API Key 签名。只在当前项目 `.env` 中手动配置自己的 `WEEX_WEB_CC_TOKEN` 和 `WEEX_WEB_TERMINAL_CODE`；`config show` 只显示 `web_credentials_configured`，不会输出值。不要从其他项目、全局配置或其他账号读取这些值。

## 止盈止损

```bash
# 单个止损/止盈，quantity=0 表示全部仓位
./weex risk tp-sl BTC \
  --plan-type STOP_LOSS \
  --trigger-price 58500 \
  --position-side LONG

# 同时规划 TP/SL；执行时先提交并验证 SL，再提交 TP
./weex risk bracket BTC \
  --position-side LONG \
  --take-profit 63000 \
  --stop-loss 58500

# 防御性替换止损：新单确认存在后才撤旧单
./weex risk replace-stop BTC \
  --old-order-id 123456789 \
  --trigger-price 59000 \
  --position-side LONG

./weex risk orders --symbol BTC
./weex risk modify 123456789 --trigger-price 59000
./weex risk cancel 123456789
```

## 成交列表与交易量

按指定时间段查询归一化成交列表和成交额汇总：

```bash
./weex trades report \
  --mode demo \
  --symbol BTC \
  --start '2026-07-17T00:00:00+08:00' \
  --end '2026-07-17T23:59:59+08:00' \
  --json

# 只看汇总，不输出逐条成交
./weex trades report \
  --mode live \
  --start '2026-07-01T00:00:00+08:00' \
  --end '2026-07-07T23:59:59+08:00' \
  --summary-only \
  --json
```

时间可使用 Unix 秒、Unix 毫秒或带时区的 ISO-8601。汇总中的 `total_quote_volume` 是每笔已成交计价金额之和；成功开仓和平仓分别计入，因此完整开平会累计两侧成交额。`opening_quote_volume` 和 `closing_quote_volume` 会分别列出。

Live 使用逐笔成交接口，能够区分 Maker/Taker、手续费和已实现盈亏。WEEX 没有公开 Demo 逐笔成交接口，因此 Demo 使用订单历史的 `cumQuote`，或以 `executedQty × avgPrice` 计算订单级成交额。该本地统计不承诺等同于 WEEX 活动、返佣或等级系统的有效交易量；应以对应活动规则和交易所结算为准。若响应中的 `complete` 为 `false`，不得把结果视为完整总量。

## JSON 输出

所有命令支持 `--json`，便于脚本和 Agent 消费：

```bash
./weex account positions --mode demo --json
./weex order plan BTC --side sell --position-side short --quantity 0.001 --price 70000 --json
```

## 项目结构

```text
src/weex_cli/
  cli.py          # 根命令和 doctor
  commands/       # Typer 命令组
  config.py       # 当前项目环境配置
  models.py       # 订单意图与 payload 编译
  gateway.py      # CCXT 与 WEEX V3/demo 接口
  service.py      # precheck、回读验证和风险更新顺序
  trade_reporting.py # 成交归一化、分页和交易量汇总
  safety.py       # 确认短语与实盘开关
tests/            # 完全离线的单元和 CLI 测试
```

## 开发与验证

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=weex_cli --cov-report=term-missing
./weex --help
./weex order plan BTC --side buy --position-side long --quantity 0.001 --price 60000 --json
```

CI 不读取 API Key，也不执行任何网络请求或交易。真实接口验证必须由操作者在明确选择的环境中运行。

## 本地 API 文档与 Codex Skill

仓库包含 `weex-api-development` skill 和官方 V3 合约文档同步器：

```bash
uv run python skills/weex-api-development/scripts/sync_docs.py
rg -n "POST_ONLY|placeTpSlOrder|Invalid IP" docs/api
```

同步器从官方 sitemap 选择当前 V3 合约页面，排除旧 V2，将正文转换为 Markdown，并生成 `docs/api/ENDPOINTS.md` 与带 SHA-256 的 `manifest.json`。默认通过可信 DoH 获取当前 CDN 地址，避免依赖异常的本机 DNS，同时不会把短期 CDN IP 写入仓库。

Skill 的可版本化源码位于 `skills/weex-api-development/`。本机安装使用指向该目录的 `~/.codex/skills/weex-api-development` 链接，因此仓库更新后技能内容同步更新。

## 参考

- [WEEX 合约 API V3](https://www.weex.com/api-doc/zh-CN/contract/intro)
- [WEEX Demo 下单](https://www.weex.com/api-doc/zh-CN/contract/demo/PlaceOrder)
- [WEEX 正式下单](https://www.weex.com/api-doc/zh-CN/contract/Transaction_API/PlaceOrder)
- [WEEX 止盈止损](https://www.weex.com/api-doc/zh-CN/contract/Transaction_API/PlaceTpSlOrder)

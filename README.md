# Local Trading & Wallet Tools

[![CI](https://github.com/GrooveSino/weex-autotrade/actions/workflows/ci.yml/badge.svg)](https://github.com/GrooveSino/weex-autotrade/actions/workflows/ci.yml)
[![Secret scan](https://github.com/GrooveSino/weex-autotrade/actions/workflows/security.yml/badge.svg)](https://github.com/GrooveSino/weex-autotrade/actions/workflows/security.yml)

本仓库包含面向 WEEX USDT 合约的本地交易工具、网页控制中心，以及独立的 Aptos 本地多账户钱包。所有服务默认只监听本机；交易写操作默认关闭或只生成计划，必须经过环境开关、预检和精确人工确认才能提交。

## 仓库组成

| 目录 | 用途 | 默认安全状态 |
| --- | --- | --- |
| `src/weex_cli/` | WEEX 命令行交易、对账和恢复工具 | Demo、dry-run |
| [`control-center/`](control-center/README.md) | WEEX 多账号网页控制中心和本地 API | Mock、实盘关闭 |
| [`aptos-wallet/`](aptos-wallet/README.md) | Aptos 多账户钱包和批量转账工具 | 主网只读、提交关闭 |
| [`docs/`](docs/README.md) | API、接口契约、可靠性设计和已脱敏缺陷报告 | 不包含账户数据 |

公开仓库不包含 `.env`、API 凭据、助记词、私钥、钱包数据库、账户快照、订单日志或运行产物。克隆后必须使用自己的本地配置；不要把真实秘密粘贴到 Issue、PR、截图或测试夹具中。

## 安全边界

- 默认模式是 `demo`，不会自动切换到实盘。
- 下单、撤单、平仓、杠杆、保证金和风险单都先输出 dry-run。
- 所有 mutation 必须复制 dry-run 输出的精确确认短语。
- 实盘 mutation 还要求 `WEEX_LIVE_TRADING_ENABLED=true`。
- `POST_ONLY` 被拒绝时不会追价，也不会降级为普通限价或市价单。
- 网络错误后不会自动重试下单；CLI 会先按 `clientOrderId` 回读，无法确认时报告“结果未知”。
- 更新止损使用“提交新止损 -> 验证新止损 -> 撤销旧止损”的顺序。
- `.env`、日志、数据库和本地运行产物均被 Git 忽略。

## 多账号控制中心

多账号 API Key、独立 HTTPS/SOCKS5 代理、余额/成交量遥测和按需日志控制台位于 [`control-center/`](control-center/README.md)。Beta 比例服务地址必须通过本地配置显式提供；仓库不包含私人部署地址或账户资料。

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

## 实盘纯 Maker 目标交易量

`live maker-volume` 把一轮定义为一次开仓和对应平仓的实际成交额之和。默认目标为 5000 USDT、
每轮约 500 USDT，因此单轮瞬时仓位约为 250 USDT；轮次按多、空交替执行，并且每轮结束后必须回到空仓。
交易量只采用 WEEX `userTrades.quoteQty` 中已确认的 Maker fill，计划金额和订单提交金额不会入账。

可以直接读取项目内显式 TOML profile。profile 只是凭据和额外的拒绝开关，不能替代独立的实盘环境门：

```bash
# 1. 生成并保存 dry-run，不会下单
./weex --profile data/live-test/weex-live-test.toml live maker-volume \
  --symbol BTC --target 5000 --round 500 --timeout 120 --leverage 2

# 2. 使用上一步输出的 plan ID 和完整确认短语执行
WEEX_LIVE_TRADING_ENABLED=true ./weex \
  --profile data/live-test/weex-live-test.toml \
  live maker-volume --plan-id lmv-example --execute \
  --confirm 'EXECUTE WEEX LIVE MAKER VOLUME ...'
```

`--leverage` 必须与账户中已经配置的逐仓杠杆一致；程序不会静默修改杠杆。部分成交会在撤单已确认后按实际仓位
继续纯 Maker 平仓，必要时使用有限次数的恢复尝试。订单状态不确定、Taker fill、`POST_ONLY` 拒绝、无法验证撤单
或无法证明最终空仓时，整个 session 立即停止，不会追价、降级或自动重试不确定的提交。数量步进可能造成少量目标超额，
结果中的 `excess_quote` 会明确列出该数值。

## 实盘 Beta 配对交易量

需要无人值守地跨多个安全 session 自动续跑时，使用 `live beta-campaign`。它只要求一次 campaign 级确认，
默认授权 6 小时内最多 20 个 child session；每个 child 仍然只能使用 `POST_ONLY`，并且只有在 BTC/ETH
均空仓、普通单和条件单都为零时才会进入下一个 child：

```bash
# 1. 生成 3000 USDT、单轮约 300 USDT 的 campaign，不下单
./weex --profile data/live-test/weex-live-test.toml \
  live beta-campaign \
  --target 3000 \
  --cycle-volume 300 \
  --hold-min 5 --hold-max 7 \
  --round-gap-min 5 --round-gap-max 7

# 2. 原样执行输出中的 execute_command；确认短语整个 campaign 只输入一次
WEEX_LIVE_TRADING_ENABLED=true ./weex \
  --profile data/live-test/weex-live-test.toml \
  live beta-campaign --execute \
  --confirm 'EXECUTE WEEX LIVE BETA-CAMPAIGN WC-EXAMPLE RUNS_20 POST_ONLY'
```

Campaign 只会为“空轮耗尽”或“轮次数耗尽”这类已确认空仓的软停止自动创建新 child。提交状态不确定、
非 Maker/未知流动性成交、成交对账失败、仓位或挂单不可观测都会终止整个 campaign。累计量只采用每个
child 经 `userTrades.quoteQty` 验证的 Maker 成交，不使用计划量估算。

`--hold-min/--hold-max` 以分钟为单位，控制两条腿都确认开仓后，到开始并发平仓前的随机等待范围；
`--round-gap-min/--round-gap-max` 也以分钟为单位，控制本轮确认空仓后，到下一轮开仓前的随机等待范围。
每轮独立均匀取值。默认不持仓等待，轮次间固定等待 1 分钟。超时、恢复次数、最大仓位、杠杆和 campaign 上限由程序采用保守默认值，
不再作为普通命令行参数暴露。

`live beta-volume` 默认只生成计划。默认目标为 5000 USDT、每个完整配对周期约 500 USDT。单轮目标量先
除以 2 得到开仓预算，再按 `BTC = budget / (1 + beta)`、`ETH = beta * BTC` 分配为 BTC 多仓和 ETH
空仓。BTC/ETH 使用独立客户端并发开仓；两边开仓阶段结束后，再按实际仓位并发平仓：

```bash
# 生成计划，不下单
./weex --profile data/live-test/weex-live-test.toml \
  live beta-volume --target 5000 --round 500

# 计划输出会给出 plan ID 和唯一确认短语
WEEX_LIVE_TRADING_ENABLED=true ./weex \
  --profile data/live-test/weex-live-test.toml \
  live beta-volume \
  --plan wv-example \
  --execute \
  --confirm 'EXECUTE WEEX LIVE BETA-VOLUME WV-EXAMPLE LEVERAGE_AUTO POST_ONLY'
```

杠杆默认是 `auto`，不需要手工估算。程序在每个空仓周期开始前重新读取可用 USDT，并按
`ceil(本轮实际开仓名义金额 × 1.20 / 可用余额)` 选择最小整数杠杆；BTC 和 ETH 的逐仓杠杆必须设置并
回读验证一致后才会开仓。自动杠杆上限为 99x，超过上限、余额不可用或杠杆回读不一致都会在下单前停止。
确有需要时仍可用 `--leverage 5` 固定覆盖，但余额不足时不会自动提高该固定值。

每个周期只有在 BTC、ETH 均确认空仓后才检查目标量。最终交易量、Maker/Taker、手续费和盈亏来自 WEEX
`userTrades` 成交明细，不会用计划金额或稀疏订单状态代替成交对账。自动化调用加 `--json --no-progress`，
可获得带 `schema_version=3`、`accounting`、`cycles`、`legs` 和脱敏 `timeline` 的稳定结果。旧版 Beta
计划不会按新逻辑执行，必须重新生成计划并确认。

Beta 会在计划中锁定，执行前只检查新鲜度和相对漂移。`low_confidence`、`usable=false` 和置信阈值仅作为
可见元数据，不阻止执行；过期、不可用、非法或非正 Beta 仍然拒绝。完整接口契约见
[`docs/interfaces/beta-volume-workflow.md`](docs/interfaces/beta-volume-workflow.md)。

## 快速检查

CLI 默认使用中文界面，包括帮助、进度、结果表格和错误提示。需要英文时可在命令最前面加
`--lang en`，或设置 `WEEX_CLI_LANG=en`；未显式设置时始终回落到中文，不跟随服务器系统语言。
`--json` 的字段名、状态码和确认短语不参与翻译，便于网页和自动化程序稳定接入。

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

# 持续写入与 weex-calc 兼容的 SQLite ticks 表；公开行情不需要 API Key
./weex market collect --db-path data/weex.db

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

`market collect` 默认订阅 WEEX 公共 WebSocket 行情，每秒原子写入一组 BTC/ETH 最新成交价，并每 5 分钟删除 12 小时以前的 tick。可用
`--poll-interval-seconds`、`--retention-hours` 和 `--cleanup-interval-seconds` 调整；部署检查可先加
`--once` 只采集一轮。`--transport rest` 可用于诊断回退，但不适合高频采集。该命令只调用公开行情接口，不读取账户余额，也不会发起任何交易。

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
control-center/   # React 控制台与 FastAPI 本地控制平面
aptos-wallet/     # React/Fastify/SQLite Aptos 本地钱包
docs/             # 接口、可靠性、API 与脱敏缺陷文档
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

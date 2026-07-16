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
WEEX_DEFAULT_MODE=demo
WEEX_LIVE_TRADING_ENABLED=false
```

API Key 建议绑定固定出口 IP。新 Key 初期只开启所需的合约权限，不要开启提现权限。CLI 只会从当前目录查找到最近的 Git/`pyproject.toml` 项目根，绝不会越过项目根读取父级或其他项目的 `.env`；也可以用 `--env-file` 显式指定来源。

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

默认会阻止在已有仓位的交易对上继续开仓；实盘还会同时预检普通挂单。WEEX Demo API 没有普通挂单查询接口，因此 Demo 只能预检仓位，执行前仍需结合历史委托人工确认。只有明确检查过账户状态后才使用 `--allow-existing`。平仓单使用 `--reduce-only`。

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
```

`account close BTC` 会平掉该交易对的多空仓位；增加 `--position-side LONG` 或 `SHORT` 时，CLI 会先查询对应仓位并使用官方 `positionId` 精确平仓，找不到唯一仓位时拒绝执行。

Demo API 目前只公开余额、下单、仓位和历史委托，没有 Demo 撤单、杠杆或独立条件单接口。这些管理命令仅支持 `live`。

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

# WEEX Fleet Control Center

多账号交易控制台。前端默认使用浏览器内 Mock 数据，也可以连接本地 Mock 控制平面；另有显式开启的 `weex-readonly` 控制平面，用于只读 Live 账户遥测。`weex-live` 是单独的、默认关闭的实盘 Beta Campaign 控制平面，只有满足全部安全门禁后才允许网页启动一个账号的一条 campaign。

## 本地运行

```bash
cd control-center
npm install
npm run dev
```

浏览器访问 `http://127.0.0.1:4173/`。

## 连接本地控制平面

打开两个终端：

```bash
cd control-center
npm run dev:api
```

```bash
cd control-center
cp .env.example .env
npm run dev
```

控制平面地址为 `http://127.0.0.1:8000`，OpenAPI 页面为 `http://127.0.0.1:8000/docs`。默认只提供 Mock 实例调度，Live 实例会被拒绝启动。

Mock 配对周期从已部署的 Beta v2 只读接口获取 BTC 多 / ETH 空比例。默认配置为：

```bash
export FLEET_BETA_RATIO_URL=http://127.0.0.1:5888/api/v1/hedge-ratio
export FLEET_BETA_RATIO_TIMEOUT_SECONDS=3
export FLEET_BETA_REFRESH_SECONDS=10
export FLEET_BETA_BACKGROUND_REFRESH_ENABLED=true
```

远程部署时，在未跟踪的 `.env` 中把该变量改为自己的 Beta 服务地址。上游没有单独的 `final_beta` 字段，控制中心把 `ratio.beta` 视为 Final Beta，并用 Decimal 按 `1 / (1 + beta)`、`beta / (1 + beta)` 重新计算 BTC 和 ETH 的名义金额权重。

生产配置启动后由唯一后台采集器先抓取一次，随后以 10 秒为最大间隔刷新同一份内存快照；如果上游 `max_age_ms` 即将耗尽，会预留最多 0.5 秒提前刷新，避免可预见的过期空窗。账号实例和页面只读取这份快照，不会各自访问上游；即使几十个账号同时开始新周期，也只会在内部复制分发。`FLEET_BETA_RATIO_CACHE_SECONDS` 仍可作为旧配置名兼容读取，但新部署应使用 `FLEET_BETA_REFRESH_SECONDS`。

页面通过 `GET /api/v1/beta` 每 10 秒展示 Final Beta、置信度、动态数据年龄和来源。展示链路保留上游的 `usable` 为 `upstreamUsable`；执行链路暂时忽略 `usable` 和置信度门槛，接受 `status=ok` 或 `status=low_confidence` 的新鲜合法响应。过期、`stale`、`unavailable`、HTTP 错误、超时或非法响应属于真实异常：运行中的受影响实例会进入带原因的系统暂停并撤销、核验活动挂单；获得新鲜合法快照后只自动恢复由 Beta 引起的系统暂停，人工暂停不会被自动解除。

当前上游响应包含：`schema_version`、`status`、`usable`、`reason_codes`、`strategy`、`as_of`、`generated_at`、`age_ms`、`max_age_ms`、`ratio.{btc_long,eth_short,beta}`、`allocation.{btc_long_weight,eth_short_weight}`、`confidence`、`confidence_threshold` 和 `source`。其中 `allocation` 是按 Beta 二次归一化后的两腿权重，不是另一套 Beta。

只读接入必须显式配置，且不会创建执行协调器：

```bash
export FLEET_CONTROL_ADAPTER=weex-readonly
export FLEET_SEED_DEMO_DATA=false
npm run dev:api
```

该模式只读取 Live 账号余额、仓位和 `userTrades`，每个账号使用自己的 API 凭据与 HTTPS/SOCKS5 代理；网页上的启动和暂停动作会被服务端拒绝，但停止动作仍可用。账号设置可填写“历史起点”：在 WEEX 最近 365 天留存范围内时，扫描完成后累计量可标记为完整；未填写或早于留存边界时会保留“未完整”状态并显示原因。

## 网页 Beta Campaign 实盘

实盘控制平面必须显式配置以下四个门禁，并使用加密 SQLite 存储。默认 `mock` 和 `weex-readonly` 不会创建下单 worker：

```bash
export FLEET_CONTROL_ADAPTER=weex-live
export FLEET_LIVE_CAMPAIGNS_ENABLED=true
export WEEX_LIVE_TRADING_ENABLED=true
export FLEET_STORAGE=sqlite
export FLEET_DB_PATH=server/data/fleet-control.db
export FLEET_MASTER_KEY="$(uv run --project server python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

网页端只暴露目标量、每轮量、持仓时间和轮间隔等简单参数。服务端继续调用 CLI 的 `LiveBetaVolumeCampaignService`，自动执行 Beta 锁定、杠杆预检、纯 `POST_ONLY` 开平仓、成交对账和故障收敛。执行前需要预览，并同时勾选风险确认和输入页面生成的完整 confirmation phrase；普通策略启动入口不会触发实盘下单。

首版只允许一个 Live 账号和一个 active campaign。停止是安全边界请求，worker 会先完成当前可确认的撤单/平仓；服务重启会把 `executing`/`stopping` 标记为 `uncertain`，该状态只允许人工核对，不提供自动重试、补单或继续执行按钮。`POST_ONLY` 拒绝、撤单/仓位/成交无法确认和网络不确定结果均不会自动重提订单。

默认使用内存存储。需要在重启后保留账号时，先生成并妥善保存主密钥，再启动 SQLite 模式：

```bash
export FLEET_STORAGE=sqlite
export FLEET_DB_PATH=server/data/fleet-control.db
export FLEET_MASTER_KEY="$(uv run --project server python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
npm run dev:api
```

更换或丢失 `FLEET_MASTER_KEY` 后，旧凭据无法恢复；服务会在启动时直接拒绝错误密钥。数据库、WAL 文件和主密钥均不得提交。

生产构建：

```bash
npm run build
```

## 当前交互

- 高密度账号实例表格、搜索、状态筛选和多选操作
- 单账号手动填写 API Key、Secret、Passphrase 与 HTTPS/SOCKS5 代理
- 单账号可选填写历史起点，用于判断累计成交量覆盖范围
- 账号配置与成交量策略分开管理；一套共享策略可同时应用到多个账号，单账号和批量换策略都受停止态与空仓门禁约束
- 策略目标可在“首次启动后新增量”和“账号历史累计绝对量”之间切换；可配置每轮总交易量范围、开仓后持仓时间范围和轮次间隔范围
- 每轮总交易量指 BTC 开仓、ETH 开仓、BTC 平仓和 ETH 平仓四笔实际成功成交额之和；用户不需要单独填写 BTC 金额
- 每轮在开仓规划前读取后台采集器的最新 Final Beta，将一半总交易量作为两腿开仓名义金额，并据此分配 BTC 多和 ETH 空
- Final Beta、两腿金额和版本会锁进周期计划；本轮开仓、等待和平仓始终沿用该计划，只有下一轮开仓规划前才读取更新后的快照
- 页面直接按总交易量范围展示预计剩余轮数；实际执行始终使用当轮 Beta v2 权威比例
- 最后一轮按剩余目标反算两腿金额，允许低于用户最小区间，Mock 账本精确停在目标交易量
- 停止实例后可逐个修改名称、分组，并选择性替换凭据或代理
- 停止实例后可二次确认删除；保存失败时保留弹窗内容，便于修正后重试
- 启停、暂停、全局停止和账户快照刷新；暂停/停止在账号运行锁内撤销活动挂单并核验，未核验成功时实例进入不可自动恢复的错误态
- 账号操作列只常驻启停、刷新和日志；审计、换策略、编辑及后续低频动作统一收进“更多”菜单
- 仅当策略不在运行且 BTC/ETH 至少一腿有敞口时，“更多”菜单才显示一键平仓；确认窗展示两腿和合计敞口，单腿残仓会单独警示
- 一键平仓在账号运行锁内先撤销并核验活动挂单，再按当前仓位执行；Mock 会记录本次实际模拟平仓成交量并核对已打开周期，策略不会自动恢复
- 汇总合约钱包、累计/今日交易量、BTC 多单与 ETH 空单敞口
- 仅在打开单个实例日志抽屉时，每 2 秒增量请求该实例日志；关闭后立即停止请求
- 仅在打开执行审计抽屉时读取该账号最近周期；展示本轮交易量贡献、精确双腿金额、随机等待、比例版本、原因码和周期 ID
- Demo/Live 状态显式区分；Mock 和只读模式都不会启动真实交易
- `WEEX READONLY` 模式在前端禁用启动、暂停和批量运行操作，保留停止、刷新、日志和审计查看
- 可选 FastAPI 控制平面：账号 CRUD、共享策略 CRUD/批量分配、启停、条件平仓、刷新、全停和按需日志 API
- `/api/v1/health` 显式返回执行能力；前端在能力未知或只读时默认锁定运行控制
- API 模式通过 SSE 实时推送完整账号快照；浏览器数量不会重复推进 Mock 状态
- 逐账号隔离遥测适配器；每个账号绑定自己的凭据与代理，并受统一并发上限控制
- 每个账号记录最后轮询成功/失败时间、耗时、连续失败次数和脱敏错误类型
- 全局调度指标记录轮次、成功/失败数、上轮耗时和实际峰值并发，并随 SSE 快照推送
- 服务重启后不会自动恢复运行态，必须人工重新启动账号实例
- 成交历史使用稳定身份去重和 Decimal 精确账本；重叠分页、游标循环和扫描预算耗尽都会保持“未完整”状态
- 只读账号更换凭据或历史起点后会清空派生遥测和旧成交账本，避免不同账号或统计口径混算
- BTC 多 / ETH 空使用独立配对执行协调器；每个周期先持久化唯一执行 ID，再分开推进开仓和到时平仓
- Mock 一轮写入 BTC/ETH 开仓与平仓四笔成交，策略贡献按四笔成交额之和计算
- 持仓等待和轮间等待均由持久化策略状态控制；轮询提前到达时不会跳过等待
- 持仓阶段若检测到 BTC/ETH 已被人工全部平仓，会先核验撤单再把周期记为人工完成；只剩单腿时暂停、撤单并报警，不自行补腿
- Beta v2 使用单一后台任务和共享 AsyncClient，最多每 10 秒刷新并在新鲜度耗尽前提前刷新；所有账号和页面只读内存快照
- Beta 真实异常时 fail closed：不创建新计划或成交，运行实例系统暂停并核验撤单；服务恢复后只恢复 Beta 系统暂停
- Post-Only 拒绝和不确定结果都是终态；服务重启会把残留 planned/opened 周期标为 uncertain，绝不自动重提
- 不确定周期在审计抽屉中明确标记为“待核对”；页面只允许复制周期 ID，不提供重试、补单或改写结果操作
- 达到目标交易量后实例自动停止；手动刷新、重复调度和再次启动都不会越过目标继续执行

## 代码结构

- `src/App.tsx`：页面状态、筛选、多选操作、控制面连接和 SSE 快照订阅。
- `src/services/controlCenter.ts`：浏览器 Mock 与 FastAPI API 的统一服务接口；日志和执行审计均按需读取。
- `src/components/AccountTable.tsx`：高密度账号行和操作列；`LogDrawer.tsx` 只轮询当前打开实例。
- `server/src/fleet_api/main.py`：FastAPI 路由、SSE、依赖组装和应用生命周期。
- `server/src/fleet_api/runtime.py`：逐账号遥测轮询、并发限制、健康指标和 Mock 周期调度。
- `server/src/fleet_api/beta_allocation.py`：Beta v2 HTTP 校验、Decimal 权重重算、集中刷新和只读快照分发。
- `server/src/fleet_api/weex_readonly.py`：Live 只读余额、仓位和 `userTrades` 扫描，不包含下单路径。
- `server/src/fleet_api/volume_history.py`：成交身份去重、Decimal 账本、历史游标和完整性判定。
- `server/src/fleet_api/strategy.py`：随机每轮总交易量、残差收尾和轮数估算的纯领域算法。
- `server/src/fleet_api/execution.py`：BTC 多 / ETH 空配对执行协议、唯一周期和不可自动重试的审计状态机。
- Beta v2 已通过 `PairAllocationProvider` 接入现有 Mock 链路；真实下单只应实现 `PairedExecutionAdapter`，并在确认参数、权限和人工门禁后替换 Mock 工厂。

遥测适配器只允许读取钱包、累计交易量、敞口和代理状态；网页上的手动刷新永远不会触发执行，也不会请求 Beta 上游。只有 Mock 模式会创建独立配对执行协调器。`GET /api/v1/runtime/metrics` 返回只读调度指标；`GET /api/v1/instances/{id}/executions` 是只读执行审计接口，返回的 `retryAllowed` 恒为 `false`。`POST /api/v1/instances/{id}/positions/close` 只在非运行、存在仓位且执行协调器可用时接受请求；WEEX 只读模式始终拒绝。Mock 执行器从策略的每轮总交易量范围抽样，在开仓规划前读取集中分发的最新 Final Beta 并把精确两腿金额与比例版本持久化；之后的开仓和平仓都使用同一个周期计划。开仓拒绝或结果不确定不会自动重提，也不会为同一周期换用另一个 Beta。只读模式不会创建执行器，也不会连接任何下单路径。真实 WEEX 下单仅由 `weex-live` Beta Campaign 路径接入；普通策略和批量控制仍保持不可下单。

策略的 `targetMode=incremental` 时，`targetVolumeQuote` 表示每个绑定账号从该策略第一次启动后需要新增的成交量，暂停/继续不重置 `startedAtMs`；`targetMode=lifetime` 时，它表示账号成交历史累计量的绝对目标，历史扫描未完整前禁止启动。换用另一套策略会重置账号的策略起点和进度，但保留执行序号与审计历史；修改共享策略会同步更新所有绑定账号的配置投影。最后一轮在 Mock 中可以用任意 Decimal 残差精确命中目标；实盘必须以成功订单的实际成交历史累计，并结合最小下单额、数量步进和价格精度确定可实现的容差。

## 后续边界

浏览器不持久化交易所 Secret，API 只返回 API Key 尾号和已去除认证信息的代理主机。控制平面默认使用进程内凭据仓，也可启用上面的 Fernet 加密 SQLite 凭据仓；后者适合本地原型恢复，不等同于生产密钥管理。正式部署前仍需接入登录鉴权、访问审计和外部 KMS/Secret Manager。现有 `src/weex_cli` 将作为交易领域核心逐步接入。

# Aptos 本地钱包

面向 Aptos Mainnet 的本地多账户钱包和批量转账工具。支持 APT、原生 Tether USDt、本地加密密钥、一对多/多对多转账、随机金额、随机间隔、可选 Fee Payer 和执行审计。

## 环境

- Node.js 22 或 24（`@aptos-labs/ts-sdk@7.2.0` 要求 Node.js 22+）
- pnpm 11.15.1（由 Corepack 和 `packageManager` 自动固定）

## 启动

```bash
cd aptos-wallet
corepack enable
pnpm install --frozen-lockfile
cp .env.example .env
pnpm dev
```

浏览器访问 `http://127.0.0.1:4310`。API 监听 `http://127.0.0.1:4311`，两个服务都只绑定本机回环地址。

生产构建和单端口运行：

```bash
pnpm build
pnpm start
```

生产页面由 API 服务托管，访问 `http://127.0.0.1:4311`。

## 主网门禁

默认只允许查询、编辑和预览。启用真实 Mainnet 提交必须在启动前显式设置：

```bash
export APTOS_MAINNET_EXECUTION_ENABLED=true
pnpm dev
```

每个任务仍必须完成余额预检、交易模拟、保险库解锁，并输入页面生成的完整确认短语。交易提交不会自动重试；传输异常会按预计算交易哈希查询链上，无法确认时任务进入 `uncertain`。

## 密钥与备份

- 主密码使用 `scrypt` 派生密钥，随机 DEK 和钱包材料使用 AES-256-GCM 加密。
- SQLite、WAL、`.env`、日志和备份文件均已在项目 `.gitignore` 中排除。
- 应用不会自动生成明文私钥文件。助记词和私钥查看均需重新验证主密码和完整名称，并通过一次性 RSA 公钥加密传给浏览器。
- 创建钱包时必须完成 24 词助记词的离线备份和随机词位确认；之后应下载加密保险库备份。
- 丢失主密码后无法恢复保险库；数据库文件不能代替主密码备份。

## 钱包与账户

- 一个钱包由一组助记词保护，钱包里可以添加多个账户，每个账户都有自己的 Aptos 地址。
- 新钱包默认创建第一个账户，可继续添加新账户，或按账户编号找回以前使用过的账户；单次最多处理 200 个。
- 仅凭助记词恢复时，应用不会扫描 Mainnet 猜测用过哪些账户。加密备份会保留完整的账户清单、名称和历史。
- 高级信息：账户使用标准路径 `m/44'/637'/{accountIndex}'/0'/0'`，账户编号范围为 `0..2,147,483,647`。
- 助记词只加密保存一次；各账户的私钥仅在签名或单独查看时临时生成。钱包和账户归档后不会物理删除，已用编号也不会自动复用。

## 资产

- APT：8 位小数。
- 原生 USDt：6 位小数，固定元数据对象 `0x357b0b74bc833e95a115ad22604854d6b0fca151cecd94111770e5d6ffc9dc2b`。
- 不接受 LayerZero、Wormhole 或其他同名桥接 USDT。

## 验证

```bash
pnpm test
pnpm test:e2e
pnpm build
pnpm audit
```

单元、API 和浏览器测试使用 Fake Gateway 或浏览器请求拦截，不会连接 Aptos 或提交真实交易。

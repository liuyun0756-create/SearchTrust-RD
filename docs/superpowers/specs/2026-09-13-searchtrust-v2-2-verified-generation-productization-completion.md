# SearchTrust V2.2 Verified Generation 产品化完成记录

日期：2026-09-15

里程碑：V22-093 直接正式发布前置

状态：代码、门禁、生产迁移与关闭开关部署完成；真实验收仍被外部配置阻塞

## 1. 交付结果

Verified Generation 已从内部 pipeline 扩展为完整可持久化产品路径：固定
$19 checkout 绑定到对应 Case，到账后向账户增加恰好 1 个 credit；后续执行包含
幂等确认、单次扣减、
durable job、冻结的 GSC/GA4/公开 GBP 输入、原 Prospect 版本系谱、Verified
report 原子持久化、受控失败后精确返还，以及用户显式发起的新 job 重试。

离线浏览器套件覆盖两条完整旅程：

1. 余额 1：生成后从 `queued`/`running` 到持久化 Verified report，changes 区域
   仅显示真实变化；
2. 余额 0：本地 Dodo fixture 购买并确认后余额为 1，不自动生成；
   第一次生成受控失败后精确返还，再次生成建立具有前任系谱的新 job。

所有浏览器 identity 和资源标识均为固定合成值或 `.invalid` 域名。Dodo fixture
使用同一 loopback origin；Google 与 Verified API 由 BrowserContext 本地 router
截取。测试环境不配置 PostHog key，任何 HTTP、redirect、beacon、popup 或 WebSocket
外连（包括 PostHog 与 Dodo 域名）都会被独立守卫记录、终止并使测试失败。两条旅程
未读取 Supabase/Railway，未创建真实 checkout，未付款。

## 2. 精确代码快照

- 前端产品实现起点：`45585aafac456babf8b58be1cb39199cbc926246`；
- 前端 Task 10 离线旅程与发布验证：
  `d7b3ea6ec1eb8af896bdfe478dedfbdcece217ae`；
- 前端 Task 10 外网隔离与变化真值加固：
  `47476a07088cace350192d705a4d5347f716a550`；
- 后端 Verified Worker/pipeline 产品实现快照：
  `b1ae6cb1c9094dc82f8f3920bb1cf3defb59a168`；
- 后端开发计划现行决策与完整 migration 顺序校正：
  `6151ad789b15dc855e24aa984dfe85fb1bab8ae2`。

本完成记录提交自身不能在正文中自引用；其精确提交以
`6151ad789b15dc855e24aa984dfe85fb1bab8ae2` 为 parent，并在发布交接回执中记录。
生产执行时必须以独立复核后的完整提交链为准，不能只发布上述中间快照。

## 3. 门禁证据

### 前端

- TypeScript typecheck：PASS；
- Vitest：96 个文件，980 项 PASS；
- contracts check：PASS；
- production build：PASS；
- artifact security scan：扫描 38 个文件，0 findings；
- 常规 Playwright CI：14 PASS，1 项 paused-only 用例按设计 SKIP；
- paused 专用 Playwright：1/1 PASS；
- Verified fixture contract：12/12 PASS，外网守卫单元测试：13/13 PASS；包含固定
  合成数据、无脚本付款页保护、Prospect→Verified change 交叉真值，以及注入未变化
  entry 必须失败的反例。

### 后端

- fast release pytest：1,788 PASS；
- 真实 Redis restart integration：23 PASS；
- release gate 总计：1,811 PASS。

### 数据库

在独立本地 Supabase 实例中从空库按顺序应用 29 条 migration，最后一条为
`20260914160000`。原有的本地 Supabase 实例未被停止、重置或改动。

- schema：114/114 PASS；
- rollback-only release acceptance：47/47 PASS；
- rollback residue：1/1 PASS，所有 `searchtrust_release_validation_%` fixture 聚合残留数为 0；
- 三份 pgTAP 文件合计：162/162 PASS。

47 项 release acceptance 包含新表、RPC、RLS、service-only 权限、purchase/ledger
kind、冻结输入与报告系谱，以及购买、重放、成功生成、失败返还和新 job
重试的回滚内事务验收。

## 4. 生产执行回执

2026-09-15 的独立生产执行已完成 Step 9，并完成 Step 10 中的迁移、
关闭开关部署与基础设施健康核对：

- 前端 `origin/main` 与审查后候选提交均为
  `47476a07088cace350192d705a4d5347f716a550`；
- Vercel 正式部署 `dpl_2c7AEnRyeGo7cgVZHAvvLoget9P8` 来自上述精确提交，
  实例 URL 为 `https://search-trust-32059gtb5-liuyuns-projects-9eb2d9a4.vercel.app`，
  `https://trysearchtrust.com` 已指向该 `READY` 部署并返回 HTTP 200；
- 后端应用发布提交为 `bd028729e6f7e083954d6956c41d236686811bfc`，
  Railway 正式 Web 与 Worker 均达到 `SUCCESS`/`RUNNING`，Web health 返回
  HTTP 200 与 `ok`；
- 前后端 GitHub `main` quality workflow 均为 passing；
- Vercel 最近一小时 error-level 日志为 0；Railway Web、Worker error-level
  日志各为 0，Web 5xx 为 0；
- 生产 Supabase 从 `20260912100000` 基线按审批顺序应用全部 9 条
  migration，现与本地完整 29 条一致，最后一条为 `20260914160000`；
- 生产 rollback-only release validation 47 项全部通过，residue 复核为 0；
- 三个 SerpAPI 账户使用不计入额度的 Account API 核对，均返回 HTTP 200、
  Active 且有可用容量；
- Web↔Worker callback 使用签名的无效合成 payload 做无写入握手，返回预期
  `400 INVALID_CALLBACK`，证明现行 callback URL/secret 配对正确。

已安全写入的配置名包括：

- Vercel：`GOOGLE_TOKEN_BROKER_SECRET`、`GOOGLE_OAUTH_COOKIE_SECRET`、
  `GOOGLE_OAUTH_REDIRECT_URI`、`GOOGLE_TOKEN_ENCRYPTION_KEYS`、
  `GOOGLE_TOKEN_ENCRYPTION_ACTIVE_VERSION`；
- Railway Worker：`V22_GOOGLE_BROKER_SECRET`、`V22_GOOGLE_BROKER_ORIGIN`。

两端 broker secret 在同一安全操作中产生并分别写入，本记录不包含其值。
发布记录提交触发的 Railway 文档型重部署已使 Worker 两项配置生效；
Vercel 五项配置仍等待外部必填值齐备后的最终前端重部署。
当前四个开关仍均为默认关闭：

- `GOOGLE_VERIFIED_ANALYSIS_ENABLED=false`；
- `V22_VERIFIED_ANALYSIS_ENABLED=false`；
- `GOOGLE_GBP_SYNC_ENABLED=false`；
- `V22_GBP_SYNC_ENABLED=false`。

## 5. 未完成的真实验收

本记录**不声明 Verified Generation 已对用户开放**。Step 10 还被以下精确条件
阻塞：

- `DODO_VERIFIED_CREDIT_PRODUCT_ID` 缺失。现有 `DODO_API_KEY` 是 Vercel 不可导出
  Secret，本机没有可用副本，因此未绕过平台策略读取 product 列表，也未
  臆测复用旧 product；
- `GOOGLE_OAUTH_CLIENT_ID` 与 `GOOGLE_OAUTH_CLIENT_SECRET` 在批准的 Vercel
  production/preview/development、Railway production/staging 及本地配置中均无可复用值；
- 生产聚合就绪性核对为：1 个 active Case，0 个 Prospect report，0 个
  healthy/matched GSC，0 个 healthy/matched GA4，0 个 healthy 且未过期的
  customer-public-GBP snapshot，因而没有符合条件的真实 Case。

本次没有创建 provider checkout、没有付款、没有运行真实 Verified job，也没有
执行失败返还或重试验收。只有在正式 Dodo product、Google OAuth 客户端凭据与
符合 GSC/GA4/公开 GBP 条件的 Case 齐备，且真实购买、到账不自动生成、成功生成、
受控失败精确返还和新 job 重试全部验证后，才能同时打开两个 Verified
开关。官方 GBP 两个开关继续保持关闭，不作为此阶段阻塞。

发布回执文档提交自身无法在正文中自引用；它会在交接回执中记录。
本文未记录 secret value、OAuth token、payment payload、客户 ID 或客户内容。

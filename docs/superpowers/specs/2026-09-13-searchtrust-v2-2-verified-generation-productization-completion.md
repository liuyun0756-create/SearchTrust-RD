# SearchTrust V2.2 Verified Generation 产品化完成记录

日期：2026-09-15

里程碑：V22-093 直接正式发布前置

状态：代码、离线 E2E 与本地发布门禁完成；生产执行待独立复核

## 1. 交付结果

Verified Generation 已从内部 pipeline 扩展为完整可持久化产品路径：固定
$19 购买 1 个 Case 级 credit、绑定 Dodo checkout、幂等确认、单次扣减、
durable job、冻结的 GSC/GA4/公开 GBP 输入、原 Prospect 版本系谱、Verified
report 原子持久化、受控失败后精确返还，以及用户显式发起的新 job 重试。

离线浏览器套件覆盖两条完整旅程：

1. 余额 1：生成后从 `queued`/`running` 到持久化 Verified report，changes 区域
   仅显示真实变化；
2. 余额 0：本地 Dodo fixture 购买并确认后余额为 1，不自动生成；
   第一次生成受控失败后精确返还，再次生成建立具有前任系谱的新 job。

所有浏览器 identity 和资源标识均为固定合成值或 `.invalid` 域名。Dodo、
Google、PostHog 及 Verified API 均由本地 router 截取；未匹配的外网请求会使
测试失败。两条旅程未读取 Supabase/Railway，未创建真实 checkout，未付款。

## 2. 精确代码快照

- 前端产品实现起点：`45585aafac456babf8b58be1cb39199cbc926246`；
- 前端 Task 10 离线旅程与发布验证：
  `d7b3ea6ec1eb8af896bdfe478dedfbdcece217ae`；
- 后端 Verified Worker/pipeline 产品实现快照：
  `b1ae6cb1c9094dc82f8f3920bb1cf3defb59a168`。

后端仓库在上述产品实现快照之后只新增本完成记录及两份发布设计/计划
校正；生产执行时必须以复核后的完整提交链为准，不能只发布上述中间快照。

## 3. 门禁证据

### 前端

- TypeScript typecheck：PASS；
- Vitest：96 个文件，976 项 PASS；
- contracts check：PASS；
- production build：PASS；
- artifact security scan：扫描 38 个文件，0 findings；
- 常规 Playwright CI：12 PASS，1 项 paused-only 用例按设计 SKIP；
- paused 专用 Playwright：1/1 PASS；
- Verified fixture contract：10/10 PASS，并包含固定合成数据与无脚本付款页保护。

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

## 4. 生产边界

本记录只完成产品化与发布准备，不是生产发布回执。截至本记录：

- `GOOGLE_VERIFIED_ANALYSIS_ENABLED=false`；
- `V22_VERIFIED_ANALYSIS_ENABLED=false`；
- 未写入 Vercel/Railway 正式配置；
- 未推送、未部署、未应用生产 migration，未进行生产验收；
- 未创建真实付款或 provider checkout；
- 未记录 secret value、OAuth token、payment payload 或客户数据。

后续 V22-093 按直接正式发布执行，不做灰度；但必须经过独立复核，严格按
“先迁移、后发布、关闭开关验证健康、最后显式开启”执行。

# SearchTrust v2.2 GBP 只读同步完成记录

日期：2026-09-07

范围：V22-062。

状态：官方连接器实施、回归、生产数据库迁移与代码发布完成；由于 Google GBP API
配额为 0 且当前商家资料不满足“已验证并活跃 60 天”准入条件，v2.2 已改用 SerpAPI
公开 GBP 作为默认路径，官方同步保留为可选增强并继续关闭。

## 2026-09-07 路径调整

- Railway 生产环境已配置三个 SerpAPI Key，现有轮换、限流与额度故障切换继续复用。
- 公开 GBP 可覆盖名称、网站、电话、地址/服务区、类别、营业时间、评分、评论、图片与帖子。
- 公开数据不包含 Search/Maps 曝光、电话点击、路线请求、网站点击和后台搜索词。
- Verified Core 要求 GSC 与 GA4 快照，官方 GBP 快照可为 not_connected；Full Evidence 仍要求
  GSC、官方 GBP 和 GA4 都是 healthy + matched。
- 连接页在官方 GBP 开关关闭时隐藏 GBP OAuth 选项，并明确显示“公开 GBP 已纳入、无需业主账号”。

## 完成内容

- 复用 `google_sync_jobs` 建立 GBP 的 request、claim、finish、fail 租约链路，任务只能由用户明确发起。
- 只读采集已确认的单个 `locations/[0-9]+` 资源，不包含写入、验证、回复或发布端点。
- 采集当前 90 个完整日、前 90 个完整日、七项固定日指标与月度搜索词；搜索词最多 10 页/1,000 行，保留 Google threshold 语义。
- 健康判定要求 Voice of Merchant、OPEN、名称、网站、电话、主类别、常规营业时间、地址或服务区域以及当期展示量。
- 前端增加 Case 所有权、active connection、`business.manage` scope、matched + confirmed binding 的双重校验，路由和 Worker 使用独立开关。
- Worker 只分发数据库中已存在的 GBP 任务；同步开关关闭时不创建 Google client，过期清理仍可独立运行。

## Content 保留边界

- `raw_payload` 保存商家资料、实际指标和搜索词，`expires_at` 固定为写入后不超过 30 天。
- 定时清理只能在到期后将 `raw_payload` 单向置空，并写入 `raw_content_deleted_at`；不允许恢复或提前清除。
- `normalized_payload` 只保留布尔检查、覆盖日期、可用性、阈值/截断状态和限制代码；不保留商家名、网站、电话、地址、指标数值或搜索词。
- 清理旧快照不会把已有更新快照的 binding 降级为 expired。

## 安全与验证

- 测试 fixture 全部使用 Example、`example.test` 和 555 电话；CI/本地回归没有调用真实 GBP 账号。
- 同步日志、错误响应、浏览器状态和长期 manifest 都不包含 access token 或商家实际 Content。
- 后端：`.venv/bin/python -m pytest -q`，1,489 项通过。
- 前端：`npm test -- --run`，59 个文件、530 项通过。
- 数据库：`npm run test:database`，19 项迁移回归通过。
- 静态与构建：`npm run typecheck` 和 `npm run build` 通过。项目现有 `next lint` 脚本与 Next.js 16 不兼容，直接 ESLint 又被现有循环配置阻断；这两项均未产生 GBP 代码诊断。

## 发布与验收边界

- 生产发布时 `GOOGLE_GBP_SYNC_ENABLED` 和 `V22_GBP_SYNC_ENABLED` 必须保持非 `true`。
- 先迁移 `20260907100000_add_v2_2_gbp_sync.sql`，再发布后端 Worker 和前端。
- 发布后只做健康、权限、disabled 响应和日志回归，不发起真实 Google 授权或同步。
- 真实账号验收通过后，再分别打开前端和 Worker 的 GBP 开关。
- 回滚时先关闭两端开关并回滚代码；保留数据结构和清理函数，直到所有受限 Content 已清除。

## 生产发布结果

- 数据库：`20260907100000_add_v2_2_gbp_sync.sql` 已应用，本地与远端迁移目录均列出 `20260907100000`。Supabase 在迁移成功后的本地 catalog cache 阶段出现连接超时警告，随后独立远端目录查询成功。
- 后端实施提交：`ca4e04d`。Railway 正式 API 部署 `309ef74b-3c68-4140-a8b3-1110f3be6839` 与正式 Worker 部署 `af9dd7f6-1b66-41a0-b090-51abe5e26ab6` 均为 SUCCESS / RUNNING；正式 Redis 为 SUCCESS / RUNNING。
- 前端提交：`6fe50f5`。Vercel 正式部署 `dpl_3iRUdtzFq3PVWcq196etDpLFijwe` 为 READY，耗时约 55 秒，正式别名为 `https://trysearchtrust.com`。
- 两端 GBP 同步开关均未配置/非 `true`；此次发布没有改动任何凭据或开关值。

### 发布后可观测性

- 正式首页：HTTP 200。
- 后端健康：`status=ok`。
- 队列健康：`status=ok`，Redis 已连接，Worker 存活，pending callbacks 为 0。
- 未登录私有 connections 页与 GBP sync 端点均返回 HTTP 404；中间件在进入 disabled handler 前保护私有路由，因此不以该 404 声称 handler 本身返回 503。
- Vercel 最近 10 分钟 error 级别日志扫描返回 0 条。
- 未审计 drains 和持续外部告警；本里程碑仅完成手工发布及健康验证，未创建新的监控自动化。

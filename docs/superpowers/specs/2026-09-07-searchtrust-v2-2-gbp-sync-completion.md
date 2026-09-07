# SearchTrust v2.2 GBP 只读同步完成记录

日期：2026-09-07

范围：V22-062。

状态：本地实施和回归通过；生产数据库迁移与代码发布待执行。

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
- 后端：`.venv/bin/python -m pytest -q`，1,486 项通过。
- 前端：`npm test -- --run`，58 个文件、527 项通过。
- 数据库：`npm run test:database`，19 项迁移回归通过。
- 静态与构建：`npm run typecheck` 和 `npm run build` 通过。项目现有 `next lint` 脚本与 Next.js 16 不兼容，直接 ESLint 又被现有循环配置阻断；这两项均未产生 GBP 代码诊断。

## 发布与验收边界

- 生产发布时 `GOOGLE_GBP_SYNC_ENABLED` 和 `V22_GBP_SYNC_ENABLED` 必须保持非 `true`。
- 先迁移 `20260907100000_add_v2_2_gbp_sync.sql`，再发布后端 Worker 和前端。
- 发布后只做健康、权限、disabled 响应和日志回归，不发起真实 Google 授权或同步。
- 真实账号验收通过后，再分别打开前端和 Worker 的 GBP 开关。
- 回滚时先关闭两端开关并回滚代码；保留数据结构和清理函数，直到所有受限 Content 已清除。

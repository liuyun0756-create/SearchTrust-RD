# SearchTrust v2.2 Google OAuth 安全基础完成记录

日期：2026-09-05

状态：V22-050 安全基础已完成并部署；生产功能开关保持关闭。

设计依据：[Google OAuth 安全基础设计](./2026-09-05-searchtrust-v2-2-google-oauth-foundation-design.md)

实施依据：[Google OAuth 安全基础实施计划](./2026-09-05-searchtrust-v2-2-google-oauth-foundation-implementation-plan.md)

## 1. 已完成范围

- 实现固定的 identity、GSC、GA4、GBP scope 目录和实际授权覆盖判断；
- 实现一次性 OAuth state、PKCE S256、登录 Cookie HMAC 绑定、10 分钟会话有效期和受控站内返回地址；
- 使用带版本和 AAD 绑定的 AES-256-GCM 加密 access token、refresh token 与 PKCE verifier；
- 实现首次授权、增量授权、部分 scope、刷新租约、`invalid_grant`、重新授权、撤销和删除状态；
- 实现安全 Google provider adapter，自动化测试只使用 fake provider；
- 实现用户连接、授权、回调、增量授权与删除的隐藏服务端路由；
- 实现只向 Railway Worker 返回短期 access token 的内部 broker；
- 实现时间窗、HMAC、request ID 和 nonce 摘要的持久化重放保护；
- 实现前后端共享的 canonical signature fixture，保证 TypeScript 与 Python 签名一致；
- 实现固定安全错误合同和日志脱敏，浏览器响应不返回 token 或数据库密文字段。

本轮没有增加用户可见的 Google 入口，也没有实现 GSC、GA4、GBP 资源选择或数据同步。

## 2. 数据库迁移

迁移文件：

`search-trust/supabase/migrations/20260905000000_add_v2_2_google_oauth_foundation.sql`

变更包括：

- 扩展 `google_connections` 的重新授权状态、刷新租约和 token 清除约束；
- 新增 `google_oauth_sessions`；
- 新增 `google_connection_events`；
- 新增 `google_token_broker_requests`；
- 新增过期 OAuth 会话和 broker 重放记录清理函数；
- 四张表均保持 RLS 和 service-role-only 边界。

生产迁移已在 2026-09-05 应用。随后重新读取迁移目录，远端与本地均列出版本 `20260905000000`，不再等待用户手动执行 SQL。

## 3. 自动化验证

前端仓库：

- 43 个测试文件通过；
- 396 项测试通过；
- TypeScript 类型检查通过；
- V2.2 JSON Schema 生成一致性检查通过；
- Next.js 生产构建通过，构建清单包含新的隐藏 Google 服务端路由；
- 浏览器静态 bundle 的 Google 服务端 secret 名称扫描无命中。

后端仓库：

- 1404 项 pytest 全部通过；
- Python 与 TypeScript broker canonical signature fixture 一致；
- 既有报告、竞品发现、GBP、Findings、任务队列和回调测试未回归。

## 4. 正式部署验收

前端：

- Vercel production deployment：`dpl_CyypL7ZHKRpEWJ1phcNLtaKW7xGV`；
- 前端 commit：`8a611a9`；
- 状态：Ready；
- production alias：`https://trysearchtrust.com`；
- 首页和 `/cases/new` 均返回 HTTP 200；
- 未登录访问隐藏 Google 用户路由和内部 broker 均返回 HTTP 404；
- production 环境变量清单中不存在 `GOOGLE_CONNECTIONS_ENABLED` 和任何新 Google OAuth secret，因此功能保持关闭；
- 部署后的 Vercel error log 扫描无错误。

后端：

- Railway code-bearing commit：`1de13ed`；
- production API 首次部署：`3d89a1f7-2406-4a90-b76e-a09c82b265a2`；
- production Worker 首次部署：`8bb6306d-7c86-4523-950d-c520ed27e7b9`；
- 新部署暴露出既有 Redis 持久卷 `/data` 的所有者错误。已通过 Railway SSH 把目录所有权恢复为 Redis 运行用户，并确认 RDB 再次成功落盘；
- 修复后 production Worker redeploy：`d1d8e66f-b295-40fa-9685-5fc822dc72c3`，状态 Running；
- 修复后 production API redeploy：`d4f5e8c3-4f6f-4c77-8fe4-640b07af736e`，状态 Running；
- `/api/v1/health` 返回 `ok`；
- `/api/v2/health/queue` 返回 `ok`、Redis connected、Worker alive、pending callbacks 为 0；
- 修复后的 API、Worker 和 Redis 日志未继续出现 MISCONF、permission denied、connection error 或 traceback。

## 5. 仍保持关闭的边界

生产环境没有写入真实 Google client secret、token encryption key、OAuth Cookie secret 或 broker secret，也没有启用 `GOOGLE_CONNECTIONS_ENABLED`。因此本次部署只提供经过测试的安全基础，不会让线上用户进入 OAuth 流程。

正式开启前仍需单独完成：

1. Google Cloud OAuth consent screen 和品牌配置；
2. GSC 与 GA4 scope 配置及适用的敏感 scope 验证；
3. GBP API access approval；
4. 在 Vercel Secret Store 写入生产密钥；
5. 使用专用真实 Google 测试账号验证授权、增量授权、刷新、撤销和 broker；
6. 验收通过后才把 `GOOGLE_CONNECTIONS_ENABLED` 设为 `true` 并增加用户入口。

## 6. 下一入口

下一开发项为 V22-051：复用本轮连接摘要、实际 scope 覆盖和短期 token broker，实现 GSC、GA4、GBP 资源发现与 Case 绑定。V22-051 不得增加任何 refresh token 读取路径。

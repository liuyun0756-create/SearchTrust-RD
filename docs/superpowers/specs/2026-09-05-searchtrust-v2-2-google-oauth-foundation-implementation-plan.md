# SearchTrust v2.2 Google OAuth 安全基础实施计划

日期：2026-09-05

状态：实现与自动化回归已完成；等待正式部署验收记录。

设计依据：[Google OAuth 安全基础设计](./2026-09-05-searchtrust-v2-2-google-oauth-foundation-design.md)

## 1. 实施原则

- 测试先行：每个安全边界先写失败测试，再写最小实现。
- 令牌最小暴露：refresh token 和加密主密钥只存在于 Vercel 服务端。
- 默认关闭：没有显式开启 `GOOGLE_CONNECTIONS_ENABLED` 时，用户 OAuth 接口返回稳定的 503 安全错误。
- 真实 Google 隔离：自动化测试只使用 fake provider，不访问真实 Google API。
- 小步提交：数据层、安全原语、服务编排和 HTTP 路由分别提交。
- 不改变现有 Prospect 报告、付款、分享和任务队列行为。

## 2. Task 1：数据库状态、OAuth 会话和审计事件

文件：

- 新增 `search-trust/supabase/migrations/20260905000000_add_v2_2_google_oauth_foundation.sql`
- 修改 `search-trust/supabase/tests/database/v22_schema.test.sql`
- 修改 `search-trust/src/lib/database/v22Migration.test.ts`
- 修改 `search-trust/src/types/database.ts`

步骤：

1. 增加会失败的数据库测试，覆盖 `reauth_required` 状态、刷新租约、OAuth 会话、审计事件、RLS 和 token 清除约束。
2. 新增 migration：
   - 扩展 `google_connections` 状态和 active/terminal token 约束；
   - 增加 `refresh_lease_id` 与 `refresh_lease_expires_at`；
   - 新建 `google_oauth_sessions`、`google_connection_events` 和 `google_token_broker_requests`；
   - 只授予 service role 权限，撤销 anon/authenticated；
   - 增加过期会话清理函数。
3. 更新 TypeScript 数据库类型。
4. 运行数据库测试与 `git diff --check`。
5. 提交 `feat(v2.2): add Google OAuth persistence model`。

## 3. Task 2：scope 目录、错误合同和安全脱敏

文件：

- 新增 `search-trust/src/lib/google-connections/scopes.ts`
- 新增 `search-trust/src/lib/google-connections/scopes.test.ts`
- 新增 `search-trust/src/lib/google-connections/errors.ts`
- 新增 `search-trust/src/lib/google-connections/safe-log.ts`
- 新增 `search-trust/src/lib/google-connections/safe-log.test.ts`

步骤：

1. 测试固定数据源、identity 强制包含、未知 source 拒绝、scope 覆盖和确定性排序。
2. 实现 `identity`、`gsc`、`ga4`、`gbp` 服务端 scope 目录。
3. 定义稳定 `GoogleConnectionError`，只允许批准的错误码、HTTP 状态和安全文案。
4. 测试并实现嵌套对象、header、URL query、bearer/JWT 和异常文本脱敏。
5. 运行模块测试、typecheck 和 `git diff --check`。
6. 提交 `feat(v2.2): define Google connection security contracts`。

## 4. Task 3：AES-GCM 令牌保险箱与 PKCE/state

文件：

- 新增 `search-trust/src/lib/google-connections/token-vault.ts`
- 新增 `search-trust/src/lib/google-connections/token-vault.test.ts`
- 新增 `search-trust/src/lib/google-connections/oauth-state.ts`
- 新增 `search-trust/src/lib/google-connections/oauth-state.test.ts`
- 新增 `search-trust/src/lib/google-connections/config.ts`
- 新增 `search-trust/src/lib/google-connections/config.test.ts`

步骤：

1. 测试 AES-256-GCM 往返、随机 IV、AAD 篡改、错误 key version、错误长度和密钥轮换。
2. 实现版本化密钥配置与 token/verifier 专用 AAD。
3. 测试高熵 state、SHA-256 摘要、PKCE verifier 与 S256 challenge。
4. 实现 OAuth Cookie 绑定值的 HMAC 签名和恒定时间校验。
5. 测试功能开关关闭、缺失 Google 配置和无效加密配置。
6. 运行模块测试、typecheck 和 `git diff --check`。
7. 提交 `feat(v2.2): add Google OAuth cryptographic foundation`。

## 5. Task 4：Google provider adapter

文件：

- 新增 `search-trust/src/lib/google-connections/provider.ts`
- 新增 `search-trust/src/lib/google-connections/provider.test.ts`

步骤：

1. 定义 provider 接口：authorization URL、code exchange、userinfo、refresh 和 revoke。
2. 通过 fake fetch 测试 URL 参数、请求编码、成功规范化和错误正文不泄漏。
3. 实现 Google production adapter，设置超时、禁止自动记录 body，并只抛出安全错误。
4. 确保实际 scopes 只来自提供方响应，不信任请求 scopes。
5. 运行模块测试、typecheck 和 `git diff --check`。
6. 提交 `feat(v2.2): add safe Google OAuth provider adapter`。

## 6. Task 5：仓库与连接服务

文件：

- 新增 `search-trust/src/lib/google-connections/contracts.ts`
- 新增 `search-trust/src/lib/google-connections/repository.ts`
- 新增 `search-trust/src/lib/google-connections/repository.test.ts`
- 新增 `search-trust/src/lib/google-connections/service.ts`
- 新增 `search-trust/src/lib/google-connections/service.test.ts`

步骤：

1. 定义浏览器安全连接摘要、OAuth 会话、token 密文和事件合同。
2. 实现 service-role repository：连接读写、一次性会话消费、审计事件和条件刷新租约。
3. 以 fake repository/provider/vault 测试：
   - 开始授权和 Case 所有权；
   - 回调拒绝、过期、重复消费和 subject 去重；
   - 首次无 refresh token；
   - 已有连接保留或原子替换 refresh token；
   - 部分 scopes；
   - 刷新成功、5xx、`invalid_grant` 和并发租约；
   - revoke/delete 幂等；
   - 跨用户操作拒绝。
4. 实现最小连接服务并保持 provider 可注入。
5. 运行模块测试、typecheck 和 `git diff --check`。
6. 提交 `feat(v2.2): implement Google connection lifecycle`。

## 7. Task 6：用户 OAuth 路由

文件：

- 新增 `search-trust/src/lib/google-connections/handlers.ts`
- 新增 `search-trust/src/lib/google-connections/handlers.test.ts`
- 新增 `search-trust/src/app/api/v2/google/connections/authorize/route.ts`
- 新增 `search-trust/src/app/api/v2/google/oauth/callback/route.ts`
- 新增 `search-trust/src/app/api/v2/google/connections/route.ts`
- 新增 `search-trust/src/app/api/v2/google/connections/[id]/authorize/route.ts`
- 新增 `search-trust/src/app/api/v2/google/connections/[id]/route.ts`

步骤：

1. 先测试未登录、功能关闭、错误输入、跨用户、授权重定向、Cookie 属性和 no-store 响应。
2. 实现薄 HTTP handler，将业务规则留在 service。
3. 回调 URL 只携带安全结果码并限制站内 return path。
4. 连接列表只输出安全摘要，不序列化数据库 token 字段。
5. 运行 handler 测试、typecheck 和 `git diff --check`。
6. 提交 `feat(v2.2): expose hidden Google OAuth server routes`。

## 8. Task 7：Railway token broker

文件：

- 新增 `search-trust/src/lib/google-connections/broker-signature.ts`
- 新增 `search-trust/src/lib/google-connections/broker-signature.test.ts`
- 新增 `search-trust/src/lib/google-connections/broker.ts`
- 新增 `search-trust/src/lib/google-connections/broker.test.ts`
- 新增 `search-trust/src/app/api/internal/v2/google/connections/[id]/access-token/route.ts`

步骤：

1. 测试 HMAC 正文签名、时间窗、恒定时间比较、request ID/nonce 绑定和重放拒绝。
2. 实现 broker handler：连接状态、source scope、刷新租约和 no-store token 响应。
3. 确保错误响应、日志和持久事件不含 token。
4. 为未来 Python Worker 固定签名 canonical form，并生成跨语言 fixture。
5. 运行 broker 测试、typecheck 和 `git diff --check`。
6. 提交 `feat(v2.2): add short-lived Google token broker`。

## 9. Task 8：回归、文档与默认关闭验收

文件：

- 修改 `SearchTrust-RD/docs/CHANGE_MANAGEMENT.md`
- 新增 `SearchTrust-RD/docs/superpowers/specs/2026-09-05-searchtrust-v2-2-google-oauth-foundation-completion.md`

步骤：

1. 运行前端 Google 模块测试。
2. 运行前端完整 `npm test`、`npm run typecheck`、`npm run build` 和 `npm run contracts:check`。
3. 运行后端完整 pytest，证明现有分析和 Worker 未回归。
4. 检查生产和本地配置中功能开关默认关闭，且 bundle 不含服务端 secret 名称和值。
5. 扫描日志、测试 fixture 和 git diff，确认不存在 token、授权码或真实 Google 凭证。
6. 写完成记录，包含实现范围、测试证据、数据库 migration、未接真实 Google 的边界和 V22-051 入口。
7. 提交文档；确认两个仓库工作区干净。
8. 经自动化验收通过后再推送；不启用生产功能开关，不显示用户入口。

## 10. 完成定义

- 设计中的 9 项 V22-050 验收标准全部通过；
- migration 可重复应用且数据库测试通过；
- 真实 Google provider 已存在但只有显式配置和开关才能创建；
- fake provider 覆盖全部失败路径；
- 所有现有测试、类型检查和生产构建通过；
- 两个仓库提交边界清楚，无未跟踪或未提交文件；
- 生产仍保持 Google 连接关闭和界面隐藏。

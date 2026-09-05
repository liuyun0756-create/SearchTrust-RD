# SearchTrust v2.2 Google OAuth 安全基础设计

日期：2026-09-05

状态：设计已通过对话评审，等待书面确认；尚未实施。

对应开发计划：V22-050 OAuth 加密与增量授权。

## 1. 目标

建立 Google 第一方数据连接的服务端安全基础，使后续 GSC、GA4 和 GBP 资源发现与同步能够复用同一套授权能力。

本轮完成后，系统应当能够：

- 为已登录且拥有 Case 的用户发起 Google OAuth；
- 按数据源增量申请并核对实际授予的 scopes；
- 使用一次性 state、PKCE 和登录会话绑定抵御 CSRF 与授权码注入；
- 使用 AES-256-GCM 在应用层加密 access token、refresh token 和短期 PKCE verifier；
- 安全刷新即将过期的 access token；
- 断开连接时撤销 Google token 并清除本地密文；
- 仅向 Railway Worker 提供完成一次同步所需的短期 access token；
- 确保浏览器、普通 API 响应、日志和埋点中不出现任何 token 或授权码。

## 2. 本轮不包含

本轮明确不实现：

- 用户可见的“连接 Google”入口或 Connection Center；
- GSC site、GA4 property/web stream、GBP account/location 的资源选择；
- Case 与具体 Google 资源的绑定；
- GSC、GA4、GBP 数据同步和快照；
- 第一方身份匹配、Findings、重新排序或 verified execution 报告；
- 在 CI 中调用真实 Google 或真实商家数据。

生产环境功能开关默认关闭。在 Google Cloud 项目、OAuth consent screen、敏感 scope 验证和 GBP API 权限完成前，线上用户看不到也无法启动授权流程。

## 3. 架构决策

采用“Vercel 令牌保险箱”架构：

- Next.js/Vercel 服务端负责浏览器 OAuth、token 交换、应用层加密、刷新、撤销和连接状态读取；
- Supabase 只保存密文、非敏感连接元数据、一次性 OAuth 会话和安全审计事件；
- Railway Worker 不持有 refresh token，也不持有 token 解密密钥；
- Worker 未来通过受签名保护的内部 broker 接口申请指定连接和数据源所需的短期 access token；
- 浏览器永远只能看到连接 ID、Google 账号展示信息、实际 scopes、数据源覆盖状态和安全错误码。

不采用以下方案：

1. Vercel 与 Railway 共用解密密钥：实现更直接，但扩大长期凭证和主密钥的暴露范围。
2. 全部 OAuth 逻辑放入 Railway：会把现有 Clerk 用户会话、Case 所有权和浏览器回调边界扩展到后端，改动与耦合更大。

## 4. 权限模型

权限按数据源声明，不接受调用方提交任意 scope 字符串。首版只允许服务端目录中的固定映射：

- `identity`：OpenID、email、profile；
- `gsc`：只读 Search Console scope；
- `ga4`：只读 Analytics scope；
- `gbp`：计划中批准的 Business Profile 只读 scopes。

开始授权接口接收数据源集合，服务端计算所需 scopes，并强制包含 `identity`。后续补充数据源时使用增量授权参数，并把已授予 scopes 作为提示，但最终只信任 Google token 响应和 token introspection/userinfo 返回的实际结果。

连接覆盖状态由“实际 scopes 是否包含该数据源的全部必需 scopes”计算，不由请求内容或前端状态决定。部分授权可以保存，但只能把满足范围的数据源标记为可用。

## 5. OAuth 会话与 CSRF/PKCE

### 5.1 开始授权

服务端依次执行：

1. 验证 Clerk 登录身份并映射到 `public.users`；
2. 如请求携带 Case，验证该 Case 归当前用户所有；
3. 规范化所需数据源并从固定目录计算 scopes；
4. 生成高熵随机 state、PKCE verifier、S256 challenge 和 OAuth 会话 UUID；
5. 数据库只保存 state 的 SHA-256 摘要；PKCE verifier 使用令牌保险箱加密；
6. 写入 Secure、HttpOnly、SameSite=Lax 的短期绑定 Cookie；
7. 返回 Google 授权 URL，且响应禁止缓存。

OAuth 会话有效期为 10 分钟，只允许成功或失败消费一次。return path 必须来自服务端允许列表，不接受任意外部 URL。

### 5.2 回调验证

Google 回调按固定顺序验证：

1. state 存在且摘要匹配一条未消费、未过期的 OAuth 会话；
2. 安全 Cookie 中的会话 ID、state 摘要与数据库一致；
3. 当前登录用户与 OAuth 会话用户一致；
4. Case 仍归该用户所有；
5. PKCE verifier 能通过认证解密；
6. Google token 交换成功，且 Google subject 可验证；
7. 实际 scopes 至少包含 identity scopes。

任一检查失败即消费或失效该会话，不保存 token，并返回稳定安全错误码。授权码、state 原文、PKCE verifier 和 Google 原始错误正文不得写入日志或数据库事件。

## 6. 令牌保险箱

### 6.1 加密

使用 AES-256-GCM，每次加密生成独立 96-bit IV。数据库沿用现有 ciphertext、IV、auth tag 三元组和 `encryption_key_version`。

Additional Authenticated Data 固定绑定：

- 记录类型；
- 用户 ID；
- connection/session ID；
- token/verifier 种类；
- key version。

因此密文不能在不同用户、连接、字段或环境之间替换使用。

### 6.2 密钥版本

服务端 Secret Store 保存：

- 当前写入版本；
- 当前版本的 32-byte key；
- 仍需读取的历史版本 key。

新写入始终使用当前版本。读取旧版本 token 后，在一次成功刷新或连接更新中重新加密为当前版本。未知版本、认证标签失败或密钥长度错误统一返回 `GOOGLE_TOKEN_DECRYPTION_FAILED`，同时把连接转为需要重新授权状态。

### 6.3 明文生命周期

解密后的 token 只存在于单次服务端调用内存中，不缓存到磁盘、Redis、浏览器或分析任务负载。所有 token 响应使用 `Cache-Control: no-store`。

## 7. 数据模型变更

### 7.1 扩展 `google_connections`

现有表继续保存加密 token。状态约束扩展为：

- `active`：至少有可用 access token，且存在可持续使用的 refresh token；
- `error`：可重试的提供方或网络错误；
- `reauth_required`：首次授权无 refresh token、授权被撤销、`invalid_grant` 或密文无法解密；
- `revoked`：用户已主动断开且 Google 撤销流程已执行；
- `deleted`：本地删除完成。

表新增 `refresh_lease_id uuid` 与 `refresh_lease_expires_at timestamptz`，两列必须同时为空或同时非空，用于同一连接的短期刷新租约。`active` 约束调整为 access token 与 refresh token 密文均完整。进入 `reauth_required`、`revoked` 或 `deleted` 时清除全部 token 密文、key version、过期时间和刷新租约。`last_error_message` 只允许保存本地安全文案，不保存 Google 原始响应。

### 7.2 新增 `google_oauth_sessions`

仅供 service role 使用，字段包括：

- `id`、`user_id`、可空 `case_id`；
- `state_digest`；
- 加密 PKCE verifier 三元组和 key version；
- `requested_sources`、`requested_scopes`；
- 受控 `return_path`；
- `expires_at`、`consumed_at`、`outcome_code`；
- `created_at`。

约束要求密文三元组完整、有效期不晚于创建后 10 分钟、state digest 唯一。定时清理删除超过 24 小时的已消费/过期会话。

### 7.3 新增 `google_connection_events`

仅保存非敏感审计字段：

- `id`、`user_id`、可空 `connection_id`、可空 `case_id`；
- 事件类型；
- 请求数据源、实际覆盖数据源；
- 安全结果码；
- request/trace ID；
- `created_at`。

事件类型固定为授权开始、授权成功、授权拒绝、授权失败、权限扩展、刷新成功、刷新失败、撤销和删除。表禁止 anon/authenticated 直接访问，不保存 token、授权码、state、PKCE、Cookie 或 Google 原始正文。

## 8. 服务端接口

### 8.1 用户接口

- `POST /api/v2/google/connections/authorize`
  - 输入：受控数据源集合、可空 Case ID、受控 return path；
  - 输出：授权 URL 和 10 分钟过期时间；
  - 前置条件：登录、Case 所有权、功能开关开启、Google 配置完整。

- `GET /api/v2/google/oauth/callback`
  - 处理 Google 回调；
  - 成功或失败后只重定向到允许的站内地址，并使用安全结果码；
  - URL 不携带 token、授权码或原始 Google 错误。

- `GET /api/v2/google/connections`
  - 返回当前用户的非敏感连接摘要和每个数据源的 scope 覆盖状态；
  - 不返回任何密文字段、token 过期精确值或内部错误正文。

- `POST /api/v2/google/connections/{id}/authorize`
  - 为已有连接增量申请数据源权限；
  - 只有连接所有者可操作。

- `DELETE /api/v2/google/connections/{id}`
  - 执行撤销和本地密文清除；
  - 重复请求保持幂等。

### 8.2 Worker 内部接口

- `POST /api/internal/v2/google/connections/{id}/access-token`
  - 输入：source type、request ID 和用途；
  - 校验带时间戳和 nonce 的 HMAC 签名、允许时间窗口及请求唯一性；
  - 校验连接状态和实际 scopes；
  - token 临近过期时加锁刷新；
  - 只返回短期 access token、过期时间和实际 scopes；
  - 响应禁止缓存，refresh token 永不返回。

该内部接口使用独立 `GOOGLE_TOKEN_BROKER_SECRET`，不复用 V2.2 任务回调密钥或通用内部 API token。

## 9. token 保存与刷新规则

### 9.1 首次授权

首次连接必须获得 refresh token。若 Google 只返回 access token，则不创建 active 连接，OAuth 会话记录 `GOOGLE_REFRESH_TOKEN_REQUIRED`，用户未来需明确重新授权。

### 9.2 已有连接增量授权

若同一用户、同一 Google subject 已有连接且新响应没有 refresh token：

- 保留原 refresh token 密文；
- 更新 access token、到期时间、账号展示信息和实际 scopes；
- 只有实际 scopes 完整覆盖的数据源才变为可用。

若响应包含新 refresh token，则原子替换 access/refresh token 密文和 scopes。

### 9.3 并发刷新

同一连接只允许一个刷新持有者。调用方生成 lease UUID，并通过单条条件更新仅在租约为空或已经过期时写入该 UUID 和 `now() + 30 seconds`；只有返回更新行的调用方可以请求 Google。未持有者最多进行两次带抖动的短暂等待并重新读取连接，不重复调用 Google。刷新持有者在成功或失败状态更新中原子清除租约；异常退出后的过期租约可由下一调用方接管。

刷新成功时原子更新密文、过期时间、实际 scopes、状态和 key version。`invalid_grant` 或 token 已撤销时清除全部密文并转为 `reauth_required`；网络和 Google 5xx 保留密文并转为可重试 `error`。

## 10. 断开、撤销与删除

用户断开连接时：

1. 校验连接所有权；
2. 尝试使用当前 token 调用 Google revoke；
3. 无论 Google 对“已经撤销”的 token 返回成功或已失效，都清除本地全部密文；
4. 将连接标记为 `revoked` 并记录非敏感事件；
5. 后续 token broker 请求统一拒绝。

网络暂时失败时先把连接置为不可使用，并安排有限次数撤销重试；本地密文只保留到完成重试所需的最短期限。用户选择删除时，完成或终止撤销流程后将状态设为 `deleted`，既有 Case 绑定由现有数据库约束解除或降级，不删除历史报告与不可变快照。

## 11. 错误合同与日志安全

对外仅返回固定错误码和本地安全文案，例如：

- `GOOGLE_CONNECTIONS_DISABLED`；
- `GOOGLE_OAUTH_NOT_CONFIGURED`；
- `GOOGLE_OAUTH_SESSION_INVALID`；
- `GOOGLE_OAUTH_SESSION_EXPIRED`；
- `GOOGLE_OAUTH_ACCESS_DENIED`；
- `GOOGLE_REQUIRED_SCOPES_MISSING`；
- `GOOGLE_REFRESH_TOKEN_REQUIRED`；
- `GOOGLE_REAUTH_REQUIRED`；
- `GOOGLE_CONNECTION_FORBIDDEN`；
- `GOOGLE_PROVIDER_UNAVAILABLE`。

日志允许字段：request ID、用户内部 ID、Case ID、connection ID、source type、安全错误码、HTTP 状态和耗时。日志禁止字段：authorization header、Cookie、code、state 原文、PKCE verifier、access token、refresh token、Google 原始响应体和包含上述内容的异常字符串。

公共脱敏器在日志、错误对象和测试快照写入前执行；任何疑似 bearer/JWT/OAuth token 字段均替换为 `[REDACTED]`。

## 12. 配置与功能开关

新增服务端配置：

- `GOOGLE_CONNECTIONS_ENABLED=false`，默认关闭；
- `GOOGLE_OAUTH_CLIENT_ID`；
- `GOOGLE_OAUTH_CLIENT_SECRET`；
- `GOOGLE_OAUTH_REDIRECT_URI`；
- `GOOGLE_TOKEN_ENCRYPTION_ACTIVE_VERSION`；
- 对应当前及历史版本的 32-byte 加密密钥；
- `GOOGLE_TOKEN_BROKER_SECRET`。

启动或首次调用时校验配置完整性和密钥长度。配置不完整只返回 `GOOGLE_OAUTH_NOT_CONFIGURED`，不得回显变量名对应的值。前端 bundle 和 `NEXT_PUBLIC_*` 变量中不得出现这些配置。

## 13. 模块边界

Next.js 服务端代码按以下边界拆分：

- scope catalog：数据源到 scopes 的唯一映射；
- OAuth session service：state、PKCE、Cookie 和会话消费；
- token vault：AES-GCM、AAD 和密钥版本；
- Google provider adapter：URL、token exchange、userinfo、refresh、revoke；
- connection repository：仅负责 service-role 数据访问和原子状态更新；
- connection service：所有权、增量授权、保存、刷新和撤销编排；
- token broker：内部签名、重放保护、scope 检查和短期 token 响应；
- safe errors/logger：稳定错误合同与统一脱敏。

Google provider adapter 使用接口注入。单元和集成测试使用 fake provider；生产 adapter 只在功能开关开启且配置完整时创建。

## 14. 测试计划

### 14.1 单元测试

- scope 目录只允许批准的数据源与 scopes；
- state、PKCE challenge、摘要、有效期和一次消费；
- AES-GCM 往返、AAD 篡改、错误 key version、密钥轮换和非确定密文；
- 日志脱敏覆盖嵌套对象、header、URL query 和异常文本；
- 部分 scopes 的覆盖计算；
- 首次无 refresh token 与已有连接无新 refresh token 的不同处理；
- token 过期窗口和刷新状态转换；
- revoke/delete 幂等性。

### 14.2 服务测试

- 用户拒绝 Google 授权；
- state、Cookie、PKCE、用户或 Case 不匹配；
- OAuth 会话过期和重复回调；
- 部分 scope、scope 缩减和增量授权；
- token 过期、并发刷新、Google 5xx、`invalid_grant` 和已撤销；
- 多个 Google 账号及同 subject 去重；
- 跨用户读取、授权、刷新和撤销全部拒绝；
- broker 错误签名、过期签名、nonce 重放、错误数据源和缺少 scopes；
- 所有浏览器响应、内部普通日志和错误快照均不含 token。

### 14.3 数据库测试

- 新表仅 service role 可访问；
- OAuth 会话密文三元组、摘要唯一性、10 分钟有效期和一次消费约束；
- `reauth_required`、`revoked`、`deleted` 状态均不能保留 token；
- 连接更新与并发刷新具有原子性；
- 用户删除时会话、事件和连接的外键行为符合既有 Case/报告保留规则。

测试不访问真实 Google。真实凭证就绪后的独立验收使用专用测试账号和 live demo Case，且不进入 CI。

## 15. 验收标准

V22-050 只有同时满足以下条件才算完成：

1. OAuth state、PKCE、CSRF 和一次性会话测试全部通过；
2. 数据库任何时候都不保存明文 access/refresh token 或 PKCE verifier；
3. 首次无 refresh token、部分 scope、过期、撤销和跨用户 Case 均得到预期结果；
4. 并发请求不会对同一连接重复刷新；
5. revoke/delete 后 token broker 无法再取得 token；
6. 浏览器、日志、测试输出和错误响应中不出现 token；
7. 功能开关默认关闭，线上无可见入口；
8. 前后端现有完整测试继续通过；
9. 不改变当前 Prospect 报告、付款、分享和任务队列行为。

## 16. 后续衔接

V22-051 直接复用连接摘要、scope 覆盖和 token broker，分别实现 GSC、GA4、GBP 资源发现与 Case 绑定。V22-052 在资源元数据齐备后实现高置信度自动匹配与用户确认。M6 的三个 connector 只接收短期 access token，不新增 refresh token 读取路径。

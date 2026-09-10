# SearchTrust v2.2 安全加固设计

日期：2026-09-10

状态：设计已批准，实施中。

对应开发计划：V22-081 安全。

## 1. 目标

V22-081 在不开放 Google 正式连接能力的前提下，完成以下安全加固与可验证演练：

- 建立 OAuth 威胁模型并核对既有防线；
- 建立可恢复、可重复执行的双密钥分阶段轮换流程；
- 对 V2.2 可达的用户可控 URL 请求链执行 SSRF 回归与修复；
- 固化匿名报告分享的最小权限边界；
- 阻止 PII、OAuth 凭证和应用 secret 进入日志、错误响应及浏览器构建产物；
- 补齐 Clerk `user.deleted` 处理并验证账户删除和 Google 连接断开；
- 在正式环境使用合成数据完成安全演练并清理全部临时数据。

本轮采用定向安全加固，不重构与 V2.2 无关的 V2.1 网络请求和业务模块。

## 2. 发布边界

整个实施和演练期间保持以下生产功能关闭：

- Google OAuth 用户入口；
- GSC、GA4 和官方 GBP 同步；
- Verified Generation。

本轮不会写入真实 Google 用户 token，不会以真实客户数据进行轮换或删除演练，也不会因为安全验收完成而自动打开任何功能开关。后续真实账号联调和功能开放必须单独决策。

## 3. 设计依据

本设计延续现有 Google OAuth 安全基础：一次性 state、PKCE S256、登录 Cookie 绑定、固定 scope 目录、AES-256-GCM、AAD 上下文绑定、refresh lease、短期 token broker 和服务端安全错误合同。

同时核对以下当前规范：

- OAuth 2.0 Security Best Current Practice（RFC 9700）：https://datatracker.ietf.org/doc/html/rfc9700
- Google Web Server OAuth 与撤销说明：https://developers.google.com/identity/protocols/oauth2/web-server
- Google OAuth 2.0 Policies：https://developers.google.com/identity/protocols/oauth2/policies
- Clerk webhook 数据同步和 `user.deleted` 事件：https://clerk.com/docs/guides/development/webhooks/syncing
- Node.js authenticated encryption API：https://nodejs.org/api/crypto.html

## 4. 威胁模型与信任边界

### 4.1 资产

受保护资产包括：

- Google authorization code、access token、refresh token 和 PKCE verifier；
- token 加密密钥、OAuth Cookie secret、broker secret、Clerk webhook secret；
- Case 所有权、Google 资源绑定和第一方快照；
- 报告分享 token、客户版报告与 PDF；
- 用户身份、邮箱、网站、商家和分析数据；
- 删除请求及安全操作结果。

### 4.2 信任边界

系统包含以下边界：

1. 浏览器与 Next.js/Vercel 用户 API；
2. Google authorization server 与 Next.js OAuth callback；
3. Next.js service role 与 Supabase；
4. Railway Worker 与 Next.js token broker；
5. 用户可控网址、网页重定向和 Railway 抓取器；
6. 匿名分享访问者与客户版报告解析器；
7. Clerk webhook 与账户删除编排；
8. 运维人员、轮换工具和生产 secret store。

### 4.3 必须覆盖的威胁

威胁模型至少覆盖：

- OAuth CSRF、authorization code 注入、state/PKCE 重放和会话替换；
- 增量授权时 scope 提升、跨用户连接覆盖和错误 Google subject 替换；
- broker 请求伪造、重放、过期签名和跨 source token 使用；
- 密文跨用户、连接、字段或环境替换；
- 旧密钥过早移除、轮换中断、并发刷新覆盖和部分 token 更新；
- 直接内网 URL、恶意 DNS、多地址解析、DNS 重绑定和内网重定向；
- 分享 token 枚举、跨报告读取、过期后访问、撤销后访问和管理接口越权；
- token、Cookie、授权码、密钥、邮箱、网址或商家数据进入日志和错误；
- 账户删除后 token、分享、Case、任务或快照仍可访问；
- 重复和乱序删除 webhook 导致失败或删除错误账户。

每项威胁必须在实施计划中对应既有控制、待补控制和自动化验证，不接受只记录而不验证的威胁条目。

## 5. 安全模块边界

### 5.1 OAuth 威胁模型

威胁模型是 V22-081 的验收索引，不新增第二套 OAuth 实现。它引用现有 scope catalog、OAuth session、provider adapter、token vault、connection service、broker 和 safe logger，并标明每条威胁的代码控制点及测试证据。

### 5.2 令牌轮换执行器

轮换执行器是运维工具，不提供浏览器路由。它只通过服务端环境读取 Supabase service role 和加密密钥配置，并提供：

- dry-run：按表、记录类型和 key version 汇总，不读取或输出明文；
- execute：按固定小批次轮换旧版本记录；
- resume：中断后从数据库真实版本状态继续；
- verify：确认旧版本计数归零并对新版本执行受控解密验证；
- machine-readable summary：只包含批次数量、版本、状态、固定错误码和耗时。

执行器不得输出 ciphertext、IV、auth tag、token、用户邮箱、网站或 Google 账号展示信息。

### 5.3 统一安全 URL 边界

V2.2 可达的用户可控 URL 请求必须复用同一组安全规则。允许范围为业务明确需要的 HTTP/HTTPS 网站请求；固定且由服务端声明的 Google、SerpAPI、PageSpeed、Supabase、Dify 和内部服务端点仍使用各自 allowlist，不接受调用方改写 origin。

安全 URL 边界必须执行：

- URL 规范化和协议检查；
- 拒绝 URL userinfo、无效 hostname 和不允许端口；
- 解析全部 DNS 地址并拒绝任一非公网结果；
- 拒绝回环、私网、链路本地、保留、未指定、多播、IPv4-mapped IPv6 和云元数据地址；
- 使用已验证地址发起连接，避免校验与连接之间再次解析到不同地址；
- 每次重定向重新执行完整校验；
- 限制跳转次数、响应字节数、连接时间和总耗时。

范围包括首页预检、站点 inventory、竞品公开网站抓取以及 V2.2 实际复用的旧抓取入口，不包括与 V2.2 无关的旧版后台任务全面重构。

### 5.4 分享权限解析器

所有分享页面、分享 PDF 和未来分享接口必须复用同一权限解析器。权限合同为：

- token 使用 32-byte 随机值，数据库只保存 SHA-256 hash；
- 无需登录，但只能读取客户版报告和该报告对应的 PDF；
- 同一报告同时只允许一个有效 token；
- 有效期固定为 30 天；
- 创建新链接在同一事务中撤销旧链接；
- 用户可以随时撤销；
- 无效、过期、撤销、错误 view mode 和越权访问统一表现为 404；
- 响应禁止搜索引擎索引、禁止 referrer 泄漏并禁止共享缓存；
- bearer token 只放在 URL fragment 中，实际 HTTP 路径固定为 `/share`；浏览器通过固定 POST 路由的请求体解析报告和下载 PDF；
- 分享页不初始化第三方分析 SDK，避免 fragment 被客户端 pageview 采集；
- token 不授予 Case、证据原文、Google 连接、资源绑定、任务、付款或分享管理权限。

### 5.5 敏感信息防泄漏

现有 Google safe logger 扩展为 V2.2 安全边界共用能力。应用日志和错误只允许：request ID、内部 user/case/connection/job/share ID、处理阶段、source type、固定错误码、HTTP 状态和耗时。

禁止记录：

- Authorization、Cookie 和 webhook 签名；
- OAuth code、state、PKCE verifier、access token、refresh token 和 ID token；
- client secret、加密密钥、broker secret、service role key 和第三方 API key；
- Google、Clerk 或其他提供方原始错误正文；
- 用户邮箱、完整网站 URL、搜索词、商家地址和电话；
- 请求或响应完整 body。

自动化扫描使用唯一哨兵值验证应用日志、API 响应、测试快照和浏览器构建产物。仓库中的固定测试 fixture 只允许明确标注的假 secret，且不得与任何部署 secret 相同。

### 5.6 账户删除编排器

Clerk webhook 继续在签名验证成功后处理事件。`user.deleted` 只使用已验证 payload 中的 Clerk user ID 查找本地用户，不接受 payload 提供 Supabase user ID、Case ID 或 connection ID。

新增 service-role-only 的 webhook 删除回执。回执只保存 Svix event ID 的 SHA-256 摘要、Clerk user ID 的 SHA-256 摘要、事件时间、完成时间和固定结果码，不保存邮箱、姓名或 webhook body。删除回执保留 90 天，用于拒绝重复删除和晚到的旧 `user.created` 事件；清理函数只能删除超过保留期的已完成回执。

处理顺序为：

1. 查找用户；不存在则幂等成功；
2. 立即使该用户的 Google 连接不可再通过 broker 使用；
3. 在有限超时内逐个尝试 Google token revoke；
4. 无论 Google 撤销成功、已失效或暂时不可用，都清除本地 token；
5. 在同一数据库事务中写入删除回执并删除 `public.users` 记录，由数据库约束和触发器级联清理用户图；
6. 返回成功，且不为外部撤销重试继续保存凭证。

若本地数据库删除失败，删除回执也不得提交，并返回可重试错误，使 Clerk 重试 webhook。重复删除按既有回执幂等成功；90 天内收到同一身份的旧 `user.created` 时不得重新创建用户。删除事件日志只记录内部处理状态和固定错误码。

## 6. 关键数据流

### 6.1 双密钥分阶段轮换

轮换采用以下顺序：

1. Secret Store 同时配置旧、新 key version；
2. 把新版本设为 active，使所有新写入使用新密钥；
3. dry-run 汇总旧版本记录；
4. 执行器读取一小批旧记录；
5. 使用记录原有 AAD 上下文解密；
6. 使用新密钥、新随机 IV 和相同业务上下文重新加密；
7. 以记录 ID 和旧 key version 作为 compare-and-swap 条件，原子更新同一记录内的完整密文集合；
8. 并发变化导致条件不匹配时跳过并重新读取，不覆盖刷新或授权更新；
9. 重复执行直至旧版本计数为零；
10. verify 成功后才允许从 Secret Store 移除旧密钥。

OAuth session 的 PKCE verifier 和 Google connection 的 access/refresh token 分别使用自身 AAD。一个连接的 access token、refresh token、IV、auth tag 和 version 必须在同一数据库操作中更新，不允许产生混合版本记录。

### 6.2 SSRF 请求

业务层提交原始 URL 后，安全 URL 模块返回已规范化 URL 和已验证连接目标。抓取器只能使用该结果。收到重定向时丢弃旧结果，从新的绝对 URL 重新开始校验。任一校验失败立即停止，不向目标发送下一请求，并返回固定的安全错误码和用户可理解的本地文案。

### 6.3 匿名分享访问

创建分享时返回 `/share#<token>`。浏览器只向平台请求固定 `/share` 路径，fragment 不进入 HTTP 请求、Vercel request path 或 referrer；分享页也不初始化第三方分析。页面从 fragment 读取 token 后，通过带大小上限的固定 POST 请求体提交给服务端。服务端验证固定格式，再计算 hash 并查询唯一记录。数据库和服务层同时校验过期时间、撤销状态、view mode、report/case 绑定和 V2.2 报告结构。成功时只构建 client view model；PDF 使用相同 POST token 和相同解析器，不另建旁路。

采用 fragment 是因为 Vercel Runtime Logs 会记录实际 Request Path，而不是只记录动态路由模板；bearer token 不得出现在该路径中。依据：https://vercel.com/docs/logs/runtime

### 6.4 账户删除

Clerk 的已签名删除事件是唯一身份来源。删除编排器先阻止新 token 发放，再在内存中完成有限撤销尝试，随后以原子数据库函数写入无 PII 删除回执并清除本地凭证和用户图。删除完成后，原分享 token 返回 404，broker 拒绝连接，Case 和任务查询为空。`user.created` 在写入前检查删除回执，阻止乱序旧事件恢复账户。

## 7. 错误处理

所有安全边界默认 fail closed：

- 轮换：单条失败不删除旧密钥；失败记录保持旧版本并可重试；汇总不含记录内容；
- SSRF：解析不确定、DNS 包含非公网结果、连接目标无法固定或重定向超限时拒绝请求；
- 分享：任何鉴权失败统一返回 404，不区分不存在、过期、撤销或越权；
- 删除：签名无效返回 401；事件结构无效返回 400；数据库暂时失败返回 5xx 供 webhook 重试；Google revoke 失败不阻止本地删除；
- 日志扫描：任何哨兵命中都阻止交付；扫描工具自身失败也视为验收失败。

安全错误响应不得包含异常 stack、SQL 文本、提供方响应、URL query、token 或 secret 名称对应的值。

## 8. 自动化测试

### 8.1 OAuth 与密码测试

覆盖：

- state、PKCE 和 Cookie 正常流程、过期、缺失、重放及跨用户替换；
- 错误 connection owner、Case owner、Google subject 和 scope 集合；
- broker 签名、时间窗、nonce、request ID、source scope 和重复请求；
- 新旧密钥同时读取、新写入只使用 active version；
- 错误 key、AAD、ciphertext、IV、auth tag 和未知版本；
- 每次重新加密使用不同 IV；
- 轮换中断恢复、重复执行、compare-and-swap 冲突和混合版本拒绝。

### 8.2 SSRF 测试

覆盖：

- IPv4、IPv6、IPv4-mapped IPv6、大小写和尾点 hostname；
- 回环、私网、链路本地、保留、未指定、多播和常见云元数据地址；
- URL userinfo、异常端口、非 HTTP 协议和编码混淆；
- DNS 单个非公网结果、混合公网/非公网结果和 DNS 重绑定；
- 公网 URL 跳转到内网、跨协议跳转、缺失 Location 和跳转循环；
- 超大响应、慢连接和总超时；
- 被拒绝目标没有收到请求。

### 8.3 分享权限测试

覆盖：

- token 格式、hash 存储和高熵生成；
- 30 天到期边界；
- 同报告轮换只保留一个有效链接；
- 撤销、过期、错误 report/case/view mode 和跨用户管理；
- 页面与 PDF 使用相同解析器；
- 分享 token 不出现在 HTTP pathname、query、服务端 route parameter 或第三方分析事件中；
- client view 不含内部证据、连接、任务或付款字段；
- 404、noindex、referrer policy 和 cache policy。

### 8.4 日志和构建扫描

测试向错误对象、URL、headers、body 和嵌套结构注入唯一 token、Cookie、授权码、密钥和 PII 哨兵。随后检查：

- Next.js 和 Python 捕获日志；
- API JSON、redirect URL 和响应 headers；
- 测试 snapshot/fixture 输出；
- `.next` 浏览器 bundle 和 source map（如生成）；
- 轮换和删除汇总。

### 8.5 删除和断开测试

覆盖：

- webhook 缺少 header、签名无效、结构无效和正确 `user.deleted`；
- 重复删除、用户不存在和乱序 created/deleted；
- 删除回执只含摘要、固定结果码且只能由 service role 访问；
- 90 天内旧 `user.created` 被忽略，过期回执可由受控清理函数删除；
- revoke 成功、已撤销、超时和提供方暂时失败；
- 删除开始后 broker 不再发 token；
- 用户、Case、资源绑定、快照、任务、报告分享、OAuth session、Google connection 和 token broker replay record 的级联结果；
- 删除后分享链接、Case API 和 token broker 均不可访问；
- 删除用户 A 不影响用户 B。

## 9. 生产演练

生产演练只使用唯一标记的合成数据，且 Google 功能开关保持关闭。

步骤如下：

1. 部署必要的数据库约束或函数；
2. 部署 Vercel 和 Railway 代码但不改变功能开关；
3. 创建临时用户、Case、报告、分享链接和合成 OAuth 密文；
4. 执行轮换 dry-run，确认只识别预期旧版本；
5. 设置新 active version，执行小批次轮换；
6. 人为中断后恢复，再次执行确认幂等；
7. verify 确认旧版本计数为零、新密文可解密且业务访问未变化；
8. 验证旧分享链接在轮换后仍有效，轮换分享 token 后旧链接立即失效；
9. 使用正确签名的合成 `user.deleted` 事件触发删除；
10. 验证相关数据归零、分享返回 404、broker 拒绝访问；
11. 清理残留的合成数据与临时本地配置；
12. 检查 Vercel、Railway 和演练输出没有哨兵值、secret 或非预期错误。

演练记录只保存部署 ID、commit、时间、计数、固定结果码和无敏感值的验证结论。

## 10. 发布和回滚

发布顺序：

1. 应用向后兼容的数据库变更；
2. 发布 Next.js/Vercel；
3. 发布 Railway API/Worker；
4. 运行完整前后端自动化测试；
5. 运行生产合成演练；
6. 扫描部署日志；
7. 记录完成证据。

代码回滚不得删除新数据。若轮换中途失败，保留新旧两套密钥并停止继续批处理；已经轮换的记录由新密钥读取，未轮换记录由旧密钥读取。只有 verify 明确证明旧版本计数为零后，后续独立操作才可以移除旧密钥。

若账户删除流程出现本地残留，立即停止验收并保持 Google 功能关闭；通过受控清理修复合成记录后再重新演练。

## 11. 完成标准

V22-081 只有同时满足以下条件才算完成：

- 威胁模型中的每项威胁都有控制点和测试证据；
- 双密钥轮换 dry-run、execute、resume、verify 和幂等测试通过；
- 旧 key version 在合成演练后归零，旧密钥未被过早移除；
- V2.2 可达 URL 的 SSRF 对抗测试全部通过；
- 分享页面和 PDF 的只读、单 token、30 天、撤销和 404 边界通过；
- 日志、响应和浏览器构建产物没有敏感哨兵命中；
- `user.deleted` 能幂等删除用户图，且删除后所有入口均不可访问；
- 前端测试、类型检查、合同检查和生产构建通过；
- 后端完整 pytest 回归通过；
- 正式环境合成演练和临时数据清理通过；
- Google OAuth、同步和 Verified Generation 的生产开关仍为关闭。

## 12. 明确不做

本轮不包含：

- 启用真实 Google OAuth、GSC、GA4 或官方 GBP 同步；
- 使用真实用户 token 或客户数据进行演练；
- 修改 30 天分享有效期或增加分享密码；
- 新增用户可见的账户删除页面；
- 全面重构 V2.1 的所有网络请求；
- 引入独立密钥管理服务、HSM 或新的外部安全供应商；
- 因安全验收通过而自动开放任何功能开关。

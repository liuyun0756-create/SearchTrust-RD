# V22-051 资源选择与 Case 绑定完成记录

日期：2026-09-05

状态：代码、数据库与关闭状态下的生产部署完成。真实账号的 M5 端到端验收尚未执行，等待 Google 凭据与批准。

## 已实现

- GSC sites 与访问级别；GA4 account summaries/property 及 web stream 网站线索；GBP account/location、网站、地址与服务区线索。
- 分页与空状态、不同 Google 账号选择、权限补充入口、资源预览、明确选择、原子替换、单资源解绑。
- 私有 `/cases/{caseId}/connections` 页面与 `/api/v2/cases/{id}/google-resources` 接口。
- Advisor 报告中的资源选择入口由服务端 Google 功能开关控制；Client 模式不展示该入口。
- 每次保存前重新读取资源并检查访问权，名称与元数据只信任 Google 服务端响应。
- Case/连接所有权、scope 校验、过期页面冲突、撤销连接时停用绑定、历史绑定保留。
- 明确区分“用户选择”与“身份/健康已验证”，新绑定为 needs_confirmation / not_checked。
- GBP 发现结果不持久缓存；绑定证据只保存选择方式和上级资源标识。
- 修正内部 token broker 的中间件边界，Worker 由既有 HMAC 校验认证。关闭状态下 POST 返回安全的未配置错误，不会发放 token。

## 数据库

`20260905100000_add_v2_2_google_resource_binding.sql` 已应用生产 Supabase，远端/本地迁移目录一致。无新表或字段；新增两个 service-role-only RPC 与停用绑定触发器。无需用户手工执行 SQL。

## 验证证据

- Google resource 模块 24 项测试通过：Google 分页、空列表、访问丢失、恶意路径、敏感错误隔离、跨用户访问、伪造资源、服务器重新验证、并发冲突、响应投影、RPC 单记录响应与跨域拒绝。
- 数据库回归包含真实 PostgreSQL 语义下的原子替换、权限约束、历史保留、旧绑定解绑及撤销级联。
- 前端完整回归、TypeScript 检查及 Next.js 生产构建通过。
- 本轮后端无应用代码变更；未重复前一轮已通过的 1404 项后端测试。
- 没有调用真实 Google 商家或分析数据；未执行真实登录浏览器操作和已开启功能的端到端验收。

## 部署

- Frontend commit：`3f9cc58`。
- Vercel production：`dpl_w2xHEwzKrggbC35EzMZ9nZvHVosz`，READY。
- Production alias：`https://trysearchtrust.com`。
- Production 环境变量没有 Google 功能开关及新 Google 密钥；本轮没有启用开关。
- Railway 队列检查返回 ok、redis_connected=true、worker_alive=true、pending_callbacks=0。

## 后续

V22-052 基于本轮资源线索实现 GSC domain/url-prefix、GA4 web stream/domain、GBP 网站/名称/地址/服务区身份匹配。正式对用户开放前仍须配置 Google 项目、凭据和 GBP access approval，并完成真实账号授权、资源选择、替换及解绑验收。

## API 依据

- [GSC sites.list](https://developers.google.com/webmaster-tools/v1/sites/list)
- [GA4 accountSummaries.list](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/accountSummaries/list)
- [GA4 dataStreams.list](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1beta/properties.dataStreams/list)
- [GBP accounts.locations.list](https://developers.google.com/my-business/reference/businessinformation/rest/v1/accounts.locations/list)

# V22-051 Google 资源选择与 Case 绑定

状态：实现、数据库迁移与正式部署已完成；真实 Google 联调等待凭据，功能保持关闭。沿用已批准的 M5 范围与 V22-050 安全边界。

## 交付

1. 服务端读取 GSC sites、GA4 account summaries/property/web streams、GBP accounts/locations，支持分页；仅访问固定 Google API 主机，不跟随重定向，不输出 token 或原始错误。
2. 私有 Case 资源选择页面，支持不同 Google 连接、资源线索、明确选择、替换和解绑。继承默认关闭的 Google 功能开关。
3. 绑定前重新检查 Case/连接所有权和 Google 资源访问权；只接受资源标识，展示名称与元数据来自服务器。
4. 原子绑定 RPC：锁定连接与 Case，每源至多一个活动绑定；旧绑定和历史快照保留。使用 expected_binding_id 拒绝过期页面覆盖。
5. 选择记录保留操作者与时间；identity_match_status 为 needs_confirmation，health_status 为 not_checked，V22-052 再做身份验证。GBP 地址/服务区仅用于当前响应展示，不持久缓存原始商家内容。

## 数据库变更与上线

沿用生产 Supabase 的 case_source_bindings 与 google_connections，无新增表/字段。
新增 `20260905100000_add_v2_2_google_resource_binding.sql`：service-role-only 原子替换/解绑 RPC，以及连接撤销/删除/要求重新授权时停用绑定的触发器。
迁移包含回滚说明；先迁移、验证，再部署前端。不开启生产 Google 开关。

## 验证

fake Google 响应测试分页、空列表、错误权限、超时、恶意资源路径与错误正文隔离；服务测试所有权、伪造资源、服务器重新确认和乐观并发；数据库测试原子替换、越权、撤销级联及历史保留；前端完整测试、类型检查、生产构建。真实 Google 联调需正式凭据就绪后执行。

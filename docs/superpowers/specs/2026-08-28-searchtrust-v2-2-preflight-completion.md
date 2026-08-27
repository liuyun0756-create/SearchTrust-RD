# SearchTrust v2.2 Preflight API 完成报告

日期：2026-08-28

里程碑：V22-020

状态：实现与自动化验收完成，生产功能开关保持关闭

## 1. 完成范围

本里程碑已实现内部接口 `POST /api/v2/preflight`，输入和输出严格使用冻结的 `PreflightRequest` 与 `PreflightResponse`。

已完成：

- 站点 URL 规范化、IDNA、端口限制和 SSRF 安全校验；
- DNS 安全解析与固定公网 IP 连接；
- 首页有界抓取及逐跳安全重定向；
- 商家名称、电话、服务、地区、经营模式和公开地图链接提取；
- 用户、网页和公开 GBP 候选的确定性合并；
- 单次逻辑 SerpAPI GBP 搜索/详情调用；
- Google Maps 短链安全展开；
- 八个冻结数据模块的完整可用性输出；
- 稳定数据缺口、阻断语义、预计耗时桶和覆盖摘要；
- Redis 15 分钟可选缓存；
- 内部 Bearer 鉴权和独立生产功能开关；
- API、单元、合同和部署配置测试。

未包含：

- V22-021 全站库存与深度页面采集；
- V22-022 真实 SERP 市场采集；
- V22-023 竞品发现与采集；
- PageSpeed 实际调用；
- GBP 评论、照片和帖子；
- Findings、Top Actions、评分或报告结论；
- ARQ 任务、Supabase 长期落库或扣费；
- 浏览器直接调用 Python 内部接口。

## 2. 实现结构

新增独立 `app/preflight_v22` 包：

- `urls.py`：URL 规范化、DNS 分类、SSRF 和 GBP 白名单；
- `fetcher.py`：固定公网 IP 的有界首页采集；
- `extractors.py`：无网络副作用的公开页面信号提取；
- `gbp.py`：Google Maps 短链展开和一次逻辑 GBP 查询；
- `candidates.py`：用户、网页和 GBP 候选合并；
- `cache.py`：冻结响应的可选 Redis 缓存；
- `service.py`：预检编排、模块状态、缺口和覆盖输出。

API 路由位于 `app/api/v2/preflight.py`。它与持久任务路由独立，不要求 ARQ Worker 在线，也不导入或调用 v2.1 报告管线。

## 3. 冻结合同结果

- 未修改 `PreflightRequest`；
- 未修改 `PreflightResponse`；
- 未修改 `BusinessIdentity`、`TargetMarket` 或其他报告合同；
- `competitor_candidates` 在 V22-020 返回空列表；
- `COMPETITOR_DISCOVERY_PENDING` 为非阻断型，真实候选由 V22-023 生成；
- 响应不包含 Finding、Top Action、供应商成本或原始供应商数据。

冻结合同导出检查通过，仓库内 Schema、fixture 与 manifest 无漂移。

## 4. 安全边界

### 4.1 站点访问

- 只允许 HTTP/HTTPS；
- 只允许显式端口 80/443；
- 拒绝 URL 用户信息；
- 拒绝 localhost、元数据域名和所有非全局 IP；
- 域名解析结果中只要存在一个非全局地址即整体拒绝；
- TCP 连接固定使用已经验证的 IP，Host 和 TLS SNI 保留原始域名；
- 每个重定向目标重新执行完整校验；
- 限制重定向、连接/读取/总超时、响应类型和解压后字节数；
- DNS 无结果、超时、非 HTML、响应过大和上游错误返回可解释降级结果。

### 4.2 GBP 与供应商

- `gbp_url` 只接受批准的 Google Maps / Google Business Profile URL 形式；
- 短链逐跳固定公网 IP 并执行相同安全校验；
- 危险短链重定向在第二次请求前被拒绝；
- 每次未缓存预检最多执行一次逻辑 SerpAPI 搜索或详情请求；
- 不调用 v2.1 GBP 深度 enrich；
- 不抓取评论、照片或帖子；
- 供应商密钥、原始错误和完整响应不进入响应或日志；
- 超长名称、异常坐标、非法网站和其他畸形候选会被忽略。

### 4.3 接口与日志

- 使用现有 `V22_INTERNAL_API_TOKEN`；
- 预检独立受 `V22_PREFLIGHT_ENABLED` 控制；
- 生产模板默认 `false`；
- 严格从原始 JSON 验证，拒绝未知字段；
- 结构化 `422` 保留稳定安全错误码；
- 日志仅记录规范化域名、`preflight_id`、阶段耗时、候选数量和缺口码，不记录查询参数或页面正文。

## 5. 候选与降级语义

- 用户提供的服务和市场候选优先，但不会删除不同的网页候选；
- 完整身份必须具备商家名称、完整市场和可追溯经营模式；
- 不使用占位商家、占位国家或默认经营模式；
- 网页和 GBP 的域名、名称、电话及市场独立一致时提高置信度；
- 多个相近候选统一标记 `requires_confirmation=true`；
- 网站或 GBP 不可用时保留其他来源候选；
- Redis 不可用或缓存损坏时直接旁路，不影响预检；
- 只有经过 `PreflightResponse` 再验证的站点成功结果才写入缓存。

## 6. 自动化验证

最终验证结果：

- V22-020 聚焦测试：64 passed；
- 后端全量测试：387 passed；
- `python scripts/export_v22_contracts.py --check`：通过；
- `python -m compileall -q app tests`：通过；
- `git diff --check`：通过。

测试覆盖：

- URL、IPv4/IPv6、DNS、IDNA、端口和元数据地址；
- 固定 IP 连接、危险重定向、超时、非 HTML 和响应大小；
- HTML/JSON-LD 身份、服务、市场和经营模式提取；
- GBP 精确 ID、短链、搜索、多候选、供应商故障和畸形响应；
- 候选顺序、置信度、冲突及禁止占位值；
- 缓存键、命中、坏缓存和 Redis 故障；
- 八个模块状态、缺口、耗时桶和覆盖说明；
- 功能开关、内部鉴权、严格请求、OpenAPI 和无队列运行；
- v2.1 与 V22-012 持久任务回归。

## 7. 未执行的真实环境验证

自动化测试全部使用固定 HTML、DNS、重定向、GBP 和 Redis 替身，没有消耗真实供应商额度。

本次未执行：

- 真实公网网站首页抓取烟测；
- 真实 Google Maps 短链展开烟测；
- 真实 SerpAPI 查询与多 Key failover 烟测；
- 真实 Redis 缓存命中烟测；
- Railway Web 服务环境烟测；
- Next.js 服务端代理端到端调用。

这些验证需要部署环境的真实密钥、Redis 和明确测试站点。生产功能开关保持关闭，因此不会被当前用户流量调用。

## 8. 生产配置

`.env.example` 已增加：

- `V22_PREFLIGHT_ENABLED=false`；
- `V22_PREFLIGHT_CACHE_TTL_SECONDS=900`；
- 连接、读取和总超时；
- 最大重定向次数；
- 最大响应字节数；
- 可选 `PAGESPEED_API_KEY`。

启用前必须确认：

1. 前端只从 Next.js 服务端代理调用；
2. 浏览器无法读取内部令牌；
3. Redis 和 SerpAPI 在部署环境完成烟测；
4. 选定测试站点完成 SSRF、重定向和候选人工复核；
5. V22-023 接入后正式分析仍要求三个真实竞品。

## 9. 后续里程碑

下一工程里程碑为 V22-021 全站库存：

- 最多 500 个去重 URL；
- URL 分类与覆盖统计；
- 最多 50 个重点页；
- 验证模式支持 GSC 页面优先；
- 输出站点快照供 V22-030 以后的证据和报告生成使用。

V22-021 应复用本里程碑的 URL 安全和固定连接边界，但不得把预检同步请求扩展为完整全站爬取。

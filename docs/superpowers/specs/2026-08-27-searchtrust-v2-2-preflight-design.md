# SearchTrust v2.2 Preflight API 设计

日期：2026-08-27

里程碑：V22-020

状态：已确认，待实施

## 1. 目标

实现 `POST /api/v2/preflight`，在用户提交正式分析前，以低成本公开数据确认站点身份、商家身份、主营服务、目标市场和后续数据模块的可用性。

预检只返回候选项、置信度、模块可用性、数据缺口、预计耗时桶和覆盖说明。它不返回 Finding、Top Action、诊断分数或其他付费报告核心结论。

## 2. 已确认范围

### 2.1 包含

- 站点 URL 规范化、DNS 解析和 SSRF 安全校验；
- 有界首页抓取；
- 从 HTML、JSON-LD、页面标题和公开联系信息提取商家、服务及地区候选；
- 校验用户提供或页面发现的公开 Google Maps / GBP URL；
- 最小化的 SerpAPI GBP 查询；
- 八个冻结模块的可用性判断；
- 稳定数据缺口码、预计耗时桶和确定性覆盖说明；
- Redis 15 分钟可选缓存；
- 内部服务鉴权。

### 2.2 不包含

- 全站 URL 库存、深度页面抓取或页面分类，这些属于 V22-021；
- 付费 SERP 市场采集，这属于 V22-022；
- 竞品发现、选择和竞品页面采集，这属于 V22-023；
- PageSpeed 实际采集；
- GBP 评论、照片、帖子等活动数据；
- Finding、Top Action、分数、诊断文案或报告生成；
- ARQ 持久任务、Supabase 长期落库或扣费；
- 对 v2.1 报告管线的调用或修改。

## 3. 技术方向

采用独立的 V22 预检服务和轻量采集器。预检编排不进入现有 v2.1 大型抓取/报告管线，仅复用已经测试、无外部副作用的纯提取函数。GBP 外部查询通过独立受限适配器完成，禁止调用会继续抓取评论、照片和帖子的 v2.1 完整 GBP 流程。

该结构保证 v2.1 与 v2.2 运行时隔离，并为 V22-021 至 V22-023 提供清晰的后续接入边界。

## 4. API 与鉴权

### 4.1 请求与响应

接口严格遵循已冻结的 `PreflightRequest` 和 `PreflightResponse`，不增加、删除或重命名字段。

冻结合同没有单独的 `gbp_candidates` 字段。网页和公开 GBP 识别出的商家统一映射为 `identity_candidates`，并通过以下字段表达 GBP 候选状态：

- `business.public_gbp_url`；
- `confidence`；
- `match_reasons`；
- `requires_confirmation`。

V22-020 不执行竞品搜索，因此 `competitor_candidates` 返回空列表。真实竞品候选由 V22-023 生成。

### 4.2 鉴权

`POST /api/v2/preflight` 使用现有 `V22_INTERNAL_API_TOKEN` Bearer 鉴权。浏览器不得直接持有该令牌；后续由 Next.js 服务端代理调用 Python 内部接口。

预检不受 `V22_ANALYZE_ENABLED` 控制，因为它不创建正式分析任务。部署时可以单独增加 `V22_PREFLIGHT_ENABLED` 开关，以便在端到端前端流程完成前保持默认关闭。

## 5. 处理流程

1. 从原始 JSON 严格解析 `PreflightRequest` 并验证内部令牌。
2. 规范化初始站点 URL，解析 DNS，执行 URL 和目标地址安全检查。
3. 根据合同版本和规范化请求生成缓存键；Redis 命中时验证缓存内容并直接返回。
4. 使用轻量采集器获取首页，每次重定向前重新执行安全检查。
5. 从页面内容生成商家、服务、地区、地图链接和经营模式信号。
6. 对用户提供或页面发现的 GBP URL 执行一次有界的公开 GBP 查询。
7. 合并网页、GBP 和用户输入，生成排序稳定的候选项。
8. 计算模块可用性、数据缺口、预计耗时桶和覆盖说明。
9. 使用 `PreflightResponse` 再次验证完整输出。
10. 尝试写入 15 分钟 Redis 缓存并返回响应。

预检同步完成，不创建 ARQ 任务。

## 6. URL 规范化与安全

### 6.1 规范化规则

- 只允许 `http` 和 `https`；
- 主机名转为小写并使用标准 IDNA 表示；
- 移除 URL 用户信息、查询参数和片段；
- 移除默认端口；
- 规范化为最终安全站点的根地址并保留结尾 `/`；
- 安全重定向后的最终源站可替代初始源站；
- 缓存键包含规范化 URL，而不是未经处理的用户字符串。

### 6.2 拒绝规则

- URL 含用户名或密码；
- 非 HTTP(S) 协议；
- localhost、云元数据域名、私网、回环、链路本地、组播、保留或未指定 IP；
- 任一解析地址不安全；
- 80、443 以外的显式端口；
- 任一重定向目标不安全；

安全错误返回 `422`，不转换为普通数据缺口。

### 6.3 有界抓取

- 独立设置连接、读取和总超时；
- 限制重定向次数；
- 限制最大响应字节；
- 只接收 HTML/XHTML 内容类型；
- 使用固定、可识别的 SearchTrust User-Agent；
- 不执行 JavaScript；
- 不自动访问页面中的任意子资源。

DNS 无结果、网站暂时不可达、超时、非 HTML、响应过大或上游 5xx 不属于 URL 安全错误。它们不会触发目标抓取或重定向，返回 `200` 的降级预检响应，并携带阻断型数据缺口。

## 7. 候选生成

### 7.1 站点身份

`normalized_site_url` 使用最终安全站点根地址。`normalized_domain` 使用去除 `www.` 的规范化主机名。

### 7.2 用户输入优先级

用户提供的 `primary_service` 和 `target_market` 作为高置信候选排在首位。网页提取到的不同候选仍然保留，不能静默覆盖用户输入。

### 7.3 商家名称

优先级从高到低为：

1. JSON-LD 中明确的 LocalBusiness / Organization 名称；
2. `og:site_name`、结构化品牌标题；
3. 页面可见品牌语句和可靠 Logo 文本；
4. 页面标题前后缀；
5. 域名弱提示。

仅域名弱提示不能独立形成高置信身份。

### 7.4 地区

优先级从高到低为：

1. 用户提供的目标市场；
2. 匹配 GBP 的公开地址或服务区域；
3. JSON-LD 地址；
4. 页面明确服务区域；
5. 页面散文中的弱地区提示。

不存在国家码的网页地区提示不能直接组成完整 `TargetMarket`；它可以作为低置信线索或触发需要用户补充地区的数据缺口。

### 7.5 经营模式

- 存在明确实体地址且无服务区域信号：`storefront`；
- 存在明确服务区域且无实体接待地址：`service_area`；
- 两者均有：`hybrid`；
- 无可靠信号：不猜测经营模式。

只有商家名称、完整目标市场和经营模式能够组成冻结的 `BusinessIdentity` 时，才输出 `identity_candidate`。缺项时不填充占位值，改为返回阻断型数据缺口。

### 7.6 GBP 合并

GBP 与网页域名、名称、电话、地址或地区存在独立一致信号时提高置信度。多候选分数接近、名称相同但地区不同或关键信号冲突时，保留多个候选并设置 `requires_confirmation=true`。

`gbp_url` 只允许已批准的 Google Maps / Google Business Profile 主机和路径形式。短链接展开仍须逐跳执行 SSRF 校验；任意第三方 URL 不得作为 GBP URL 获取。

GBP 适配器每次未缓存预检最多执行一个 SerpAPI 搜索或详情请求。短链接展开不计入供应商请求，但受相同的重定向、超时和响应限制。适配器只返回预检所需的标准化字段和候选决策摘要，不保留或缓存完整供应商响应。

### 7.7 服务候选

服务候选来自用户输入、JSON-LD 类型/服务字段、标题和主要标题中的明确服务短语。去重采用大小写和空白规范化，输出顺序稳定。通用营销词不能作为服务候选。

## 8. 模块可用性

每次响应必须包含全部八个 `module_key`，顺序固定：

1. `site_inventory`；
2. `site_deep_analysis`；
3. `serp_maps`；
4. `serp_local_pack`；
5. `serp_organic`；
6. `public_gbp`；
7. `competitor_analysis`；
8. `pagespeed`。

判断规则：

- `site_inventory`、`site_deep_analysis`：首页安全且可访问时可用；
- `public_gbp`：存在身份线索且 SerpAPI 已配置时可用；
- 三个 SERP 模块：主营服务、目标市场和 SerpAPI 均可用时可用；
- `competitor_analysis`：SERP 前置条件满足时可用，但候选在 V22-023 生成；
- `pagespeed`：首页可访问且 PageSpeed 采集能力已配置时可用。

`available` 表示当前输入与已配置能力允许后续采集，不表示 V22-020 已经执行该模块。

## 9. 数据缺口与降级

稳定缺口码至少覆盖：

- `SITE_UNREACHABLE`；
- `SITE_RESPONSE_TOO_LARGE`；
- `SITE_CONTENT_UNSUPPORTED`；
- `BUSINESS_IDENTITY_UNCONFIRMED`；
- `OPERATING_MODEL_MISSING`；
- `PRIMARY_SERVICE_MISSING`；
- `TARGET_MARKET_MISSING`；
- `GBP_NOT_FOUND`；
- `GBP_LOOKUP_UNAVAILABLE`；
- `SERP_PROVIDER_UNAVAILABLE`；
- `PAGESPEED_UNAVAILABLE`；
- `COMPETITOR_DISCOVERY_PENDING`。

正式分析所需的商家身份、主营服务和目标市场缺失时标记为阻断型。V22-020 返回的 `COMPETITOR_DISCOVERY_PENDING` 是非阻断型流程提示，因为系统尚未执行 V22-023；V22-023 执行后仍无法形成三个真实竞品时，改为 `COMPETITORS_INSUFFICIENT` 阻断正式分析。其他可选增强数据或尚未执行的后续模块标记为非阻断型，除非它直接导致当前步骤必要输入不完整。

SerpAPI 或 Redis 故障不使整个预检失败：

- SerpAPI 故障保留网页候选并将相关模块标记为不可用；
- Redis 读取、解析或写入失败时记录告警并继续执行；
- 不返回供应商密钥、原始错误正文或内部堆栈。

## 10. 耗时与覆盖说明

预计耗时通过确定性规则映射到冻结枚举：

- 只有站点模块，或存在阻断型身份/市场缺口：`under_5_minutes`；
- 站点加部分市场模块：`5_to_10_minutes`；
- 站点、市场、竞品和性能模块均具备前置条件：`10_to_15_minutes`。

`coverage_summary` 由模板生成，只描述当前可覆盖模块和关键缺口，不输出结论、评分、Finding 或行动建议，也不暴露供应商调用次数和成本。

## 11. 缓存

- Redis 缓存 TTL：900 秒；
- 键组成：`V22_REDIS_PREFIX`、预检缓存版本、冻结合同版本、规范化请求 SHA-256；
- 哈希输入包含规范化站点 URL、规范化 GBP URL、主营服务和完整目标市场；
- 缓存值为 `PreflightResponse` JSON；读取后必须再次通过冻结模型验证；
- 命中缓存时复用同一个 `preflight_id`；
- 不缓存 URL 安全拒绝、内部异常或无法通过响应合同验证的结果；
- Redis 不可用时预检继续执行，不依赖持久任务运行时存在。

## 12. 配置

新增或明确以下配置：

- `V22_PREFLIGHT_ENABLED`，生产默认关闭；
- `V22_PREFLIGHT_CACHE_TTL_SECONDS=900`；
- `V22_PREFLIGHT_CONNECT_TIMEOUT_SECONDS`；
- `V22_PREFLIGHT_READ_TIMEOUT_SECONDS`；
- `V22_PREFLIGHT_TOTAL_TIMEOUT_SECONDS`；
- `V22_PREFLIGHT_MAX_REDIRECTS`；
- `V22_PREFLIGHT_MAX_RESPONSE_BYTES`；
- `PAGESPEED_API_KEY` 或等价的 PageSpeed 能力配置。

SerpAPI 沿用现有多密钥和安全日志配置，但预检通过受限适配器调用。

## 13. 可观测性

结构化日志仅记录：

- `preflight_id`；
- 规范化域名；
- 当前阶段；
- 总耗时和分阶段耗时；
- 缓存命中状态；
- 候选数量；
- 稳定数据缺口码；
- 外部提供商的标准化状态码。

日志不得记录内部令牌、供应商密钥、完整页面正文、完整供应商响应或用户提交的 URL 查询参数。

## 14. 测试策略

### 14.1 单元测试

- URL 规范化、IDNA、默认端口和根地址；
- IPv4、IPv6、localhost、云元数据地址和危险端口；
- DNS 多地址及 DNS rebinding 防护；
- 每跳重定向校验；
- 超时、非 HTML、响应过大和站点不可达；
- 商家、服务、地区、经营模式候选提取与排序；
- 用户输入优先但不覆盖不同网页候选；
- GBP 匹配、冲突、模糊和提供商故障；
- 八个模块的完整顺序和可用性原因；
- 缺口阻断语义、耗时桶和覆盖模板；
- 缓存键稳定性、命中和坏缓存降级。

### 14.2 API 测试

- 缺失或错误内部令牌返回 `401`；
- 非法 JSON、额外字段和非法 URL 返回 `422`；
- SSRF 目标在任何网络请求前返回 `422`；
- 正常预检严格匹配 `PreflightResponse`；
- 网站不可达返回带阻断缺口的 `200`；
- DNS 无结果、非 HTML 和响应过大返回带稳定缺口码的降级 `200`；
- SerpAPI 故障返回降级 `200`；
- Redis 故障不影响正常响应；
- 缓存命中不重复执行网页或 GBP 外部调用；
- 响应不含 Finding、Top Action 或供应商成本字段。

### 14.3 验证

测试使用固定 HTML、JSON-LD、重定向、DNS 和 GBP fixtures，默认不访问真实外网。完成后执行：

- V22-020 新增测试；
- v2 合同检查；
- Python 编译检查；
- 后端全量测试。

## 15. 验收标准

- 安全公网网站能返回至少一个可确认候选，或返回明确、可解决的数据缺口；
- 危险 URL 无法触发服务端请求；
- 站点或提供商故障不会产生虚假候选或核心结论；
- 响应完全符合冻结合同，并始终包含八个模块状态；
- 重复预检在缓存有效期内不重复消耗外部调用；
- v2.1 运行路径和测试保持不变；
- 生产环境在前端代理和端到端验证完成前保持预检开关关闭。

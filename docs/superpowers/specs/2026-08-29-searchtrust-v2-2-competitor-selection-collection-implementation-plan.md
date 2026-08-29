# SearchTrust v2.2 竞品选择和采集实施计划

日期：2026-08-29

里程碑：V22-023

依赖设计：`2026-08-29-searchtrust-v2-2-competitor-selection-collection-design.md`

## 1. 实施原则

- 测试先行：每项任务先补失败测试，再实现最小代码并运行相关回归。
- 不修改冻结 `PreflightRequest`、`PreflightResponse`、`AnalyzeRequest` 请求体、`ReportV22` 或现有 `ApiV2ContractBundle` 导出；发现接口使用独立严格内部合同。
- 正式分析只通过 `X-SearchTrust-Discovery-ID` 关联发现结果，不向请求体添加字段。
- V22-022 市场采集、候选发现和正式竞品采集职责分离；纯身份和排序逻辑不依赖 Redis、HTTP 或任务队列。
- 发现任务拥有独立状态和结果仓储，不把候选结果塞入只允许 `ReportV22` 的现有 `JobState`。
- 24 小时共享市场快照、7 天发现任务状态、50/10 网站边界、每家 30 条评论和三家 15 次供应商实际尝试均为不可放大的硬上限。
- 自动化测试只使用固定脱敏 SERP、GBP、评论、网站和 Redis 替身，不访问真实付费服务。
- 现有 v2.1 抓取路径、V22-020 预检、V22-021 站点库存和 V22-022 市场阶段必须保持回归兼容。
- 生产 `UnavailableV22Executor`、`V22_ANALYZE_ENABLED=false` 保持不变；竞品发现新增独立默认关闭开关。
- 每项任务形成小而可回退的提交；专项测试未通过时不进入下一项。

## 2. Task 1：发现合同、配置和严格内部模型

### 文件

- 新建：`app/competitors_v22/__init__.py`
- 新建：`app/competitors_v22/models.py`
- 新建：`app/api/v2/competitor_models.py`
- 修改：`app/core/config.py`
- 修改：`.env.example`
- 新建测试：`tests/test_v22_competitor_models.py`
- 新建测试：`tests/test_v22_competitor_config.py`
- 修改测试：`tests/test_api_v2_contract.py`
- 修改测试：`tests/test_v22_deployment_config.py`

### 先写测试

1. `CompetitorDiscoveryRequest` 严格要求 Case、企业身份、服务、市场、3—5 个唯一查询、语言、设备和最多 3 个补充 URL。
2. prospect 只允许现有 `en`/`mobile` 默认搜索上下文；verified 输入可携带父报告锁定的上下文投影，但不得携带 OAuth 或第一方令牌。
3. `CompetitorDiscoveryResult` 只允许 0—6 个唯一候选，包含发现 ID、市场快照引用、校验和、输入摘要、候选摘要、时效、就绪状态和结构化缺口。
4. `ready_for_confirmation=true` 时至少有 3 个可选候选；不足 3 个时必须有阻断缺口。
5. 发现任务状态只允许设计中的状态和阶段；成功必须有结果，失败必须有安全错误，非终态不得携带终态载荷。
6. 内部候选审计、分数组成、身份信号、排除记录、公开 GBP、评论、单竞品和三竞品快照严格拒绝额外字段和超限内容。
7. 三竞品快照恰好包含 3 个不同 ID 和规范官网，网站页最多 10、评论最多 30、供应商实际尝试最多 15。
8. 评论模型不允许头像、个人主页、贡献者 ID 或未知供应商字段。
9. 新配置覆盖发现开关、24 小时快照 TTL、发现状态 TTL、竞品 HTTP 超时和响应大小；硬业务上限不提供可放大的环境配置。
10. 现有 `ApiV2ContractBundle` schema、`AnalyzeRequest` 和合同导出文件保持字节级无变化。

### 实现

1. 在独立模型模块定义发现请求、创建响应、状态响应、重试响应和结果合同。
2. 定义候选评分、聚合审计、共享市场快照封装、评论记录、公开资料、竞品采集和预算模型。
3. 复用冻结 `BusinessIdentity`、`TargetMarket`、`CompetitorCandidate`、`ConfirmedCompetitor` 和 `DataGap`，不复制或放宽字段。
4. 为时间顺序、摘要、候选唯一性、严格三个竞品、预算计数和隐私字段设置模型校验器。
5. 新增 `V22_COMPETITOR_DISCOVERY_ENABLED=false`、共享快照 TTL 固定默认 86400 秒及受限网络配置。
6. 不把新模型加入现有冻结 `ApiV2ContractBundle`；M4 前端工作如需公开导出，另行版本化评审。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_models.py tests/test_v22_competitor_config.py tests/test_api_v2_contract.py tests/test_v22_deployment_config.py -q
.venv/bin/python scripts/export_v22_contracts.py --check
```

### 提交

```text
feat(v2.2): define competitor discovery contracts
```

## 3. Task 2：确定性身份聚合、过滤和候选排序

### 文件

- 新建：`app/competitors_v22/normalization.py`
- 新建：`app/competitors_v22/candidates.py`
- 新建测试：`tests/test_v22_competitor_normalization.py`
- 新建测试：`tests/test_v22_competitor_candidates.py`
- 新建 fixture：`tests/fixtures/v22_competitors/market-same-name.json`
- 新建 fixture：`tests/fixtures/v22_competitors/market-mixed-results.json`

### 先写测试

1. Unicode NFKC、大小写、空白、企业后缀、scheme、`www`、默认端口、跟踪参数、尾斜杠和 canonical URL 规范化稳定。
2. 相同 place/data/CID 优先合并；无冲突强标识时按规范域名合并；最后才使用名称、地址和坐标。
3. 相同名称和地址合并，同名异址分离；强标识冲突不得因名称相似而合并。
4. 同名异址但共享规范官网时保持独立审计身份，只让综合排名最高门店进入可选集合。
5. 客户自身按强标识、域名、名称地址逐级排除；歧义自身匹配宁可排除也不进入候选。
6. 目录、榜单、聚合、社交、广告落地页和无本地服务证据的全国站点排除。
7. Organic 结果只有规范到直接企业官网且具备服务/地区信号时可用。
8. 查询覆盖、`1/log2(position+1)` 上下文内排名、确定性业务相关性和身份可信度分别产生 `0..1` 分数。
9. 总分严格使用 45/30/20/5 权重；缺失来源上下文保持未知而不是零分。
10. 平分按总分、覆盖数、排名分、相关性和稳定 ID 顺序打破。
11. `competitor_id` 在相同身份下稳定，以 `cp_` 开头且满足冻结模式。
12. 最多输出 6 个；不足 3 个输出阻断缺口；真实空市场快照不是异常。
13. 候选的 `query_appearance_count` 是唯一查询数，`best_position` 是所有有效记录最小正整数，理由与实际信号一致。
14. 手动补充 URL 只有命中当前市场池且通过全部资格校验时才能替换低排名候选；不得伪造出现次数。

### 实现

1. 实现纯文本、地址、域名和 URL 规范化，不发起网络请求。
2. 维护版本化不可选平台域名集合，并以直接企业正向资格校验作为必要条件。
3. 将每条 `SerpMarketResultRecord` 映射为身份观察，再按强到弱规则组成聚合实体。
4. 为客户自身排除、身份歧义、共享官网和来源资格产生稳定内部原因代码。
5. 实现确定性词元和短 n-gram 服务相关性，不调用 LLM 或外部向量服务。
6. 生成内部完整分数组成，再映射为冻结 `CompetitorCandidate`。
7. 对补充网址仅在已有聚合池中定位，允许安全首页验证在后续任务接入，但不改变市场出现门槛。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_normalization.py tests/test_v22_competitor_candidates.py tests/test_v22_serp_market_models.py -q
```

### 提交

```text
feat(v2.2): rank traceable competitor candidates
```

## 4. Task 3：通用市场上下文与 24 小时共享快照

### 文件

- 修改：`app/collectors/serp_market_models.py`
- 修改：`app/collectors/serp_market_requests.py`
- 修改：`app/jobs_v22/serp_market_stage.py`
- 新建：`app/competitors_v22/market_store.py`
- 新建：`app/competitors_v22/discovery_service.py`
- 新建测试：`tests/test_v22_competitor_market_store.py`
- 新建测试：`tests/test_v22_competitor_discovery_service.py`
- 修改测试：`tests/test_v22_serp_market_stage.py`
- 修改测试：`tests/test_v22_serp_market_requests.py`

### 先写测试

1. 新 `SerpMarketContext` 能由现有 `AnalyzeRequest` 和发现请求确定性生成，查询、市场、语言和设备摘要一致。
2. `CheckpointedSerpMarketStage.collect` 现有调用和输出不变；新增 `collect_context` 不需要构造带三个占位竞品的 `AnalyzeRequest`。
3. 相同上下文产生相同基础摘要；补充 URL 不改变基础摘要。
4. 共享快照保存严格 `SerpMarketSnapshot`、来源任务 ID、摘要、校验和、创建和过期时间。
5. 24 小时内命中不产生新市场搜索；过期、坏 JSON、schema、摘要或校验和错配拒绝。
6. Redis `NX` 并发写入时首个合法快照成为规范结果，后续读取并重新验证。
7. 共享快照键不包含完整查询、地址或秘密。
8. 发现服务先读共享快照，未命中时运行 V22-022，再聚合候选并保存结果。
9. V22-022 空结果、部分结果、限流和确定性位置错误继续保留原错误语义。
10. 正式任务可以通过发现记录取得同一市场快照 ID 和校验和，不重复采集。

### 实现

1. 增加严格 `SerpMarketContext`，集中 `AnalyzeRequest` 的语言/设备解析和请求规划输入。
2. 将 `CheckpointedSerpMarketStage` 核心改为 `collect_context(job_id, context, checkpoints)`；原 `collect` 只负责投影现有请求后委托。
3. 实现 `SharedMarketSnapshotStore`，使用 Redis 规范键、TTL、严格读取校验和安全错误。
4. 实现发现服务编排：共享命中 → V22-022 采集 → 共享原子保存 → 纯候选聚合 → 严格发现结果。
5. 补充发现复用相同市场快照，只重新执行候选定位和可选首页验证。
6. 不把共享快照存入冻结报告合同，不铸造正式证据 ID。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_market_store.py tests/test_v22_competitor_discovery_service.py tests/test_v22_serp_market_stage.py tests/test_v22_serp_market_requests.py -q
```

### 提交

```text
feat(v2.2): reuse expiring market discoveries
```

## 5. Task 4：持久竞品发现任务、API 和恢复

### 文件

- 新建：`app/competitors_v22/keys.py`
- 新建：`app/competitors_v22/store.py`
- 新建：`app/competitors_v22/queue.py`
- 新建：`app/competitors_v22/worker.py`
- 新建：`app/competitors_v22/runtime.py`
- 新建：`app/api/v2/competitors.py`
- 修改：`app/api/v2/dependencies.py`
- 修改：`app/api/v2/runtime.py`
- 修改：`app/jobs_v22/worker.py`
- 修改：`app/main.py`
- 新建测试：`tests/test_v22_competitor_discovery_store.py`
- 新建测试：`tests/test_v22_competitor_discovery_queue.py`
- 新建测试：`tests/test_v22_competitor_discovery_worker.py`
- 新建测试：`tests/test_api_v2_competitors.py`
- 修改测试：`tests/test_v22_job_worker.py`

### 先写测试

1. 发现任务登记在 Redis 原子完成请求、状态、Case 幂等映射和 active 集合；相同提交重放、不同摘要冲突。
2. 状态 revision 单调，合法迁移受限，首个终态获胜，状态和请求保留 7 天。
3. 新仓储实例可恢复任务，坏状态和坏请求安全失败。
4. ARQ 物理任务 ID 与正式任务命名空间隔离，重复入队不重复执行。
5. Worker 从 `collecting_market` 进入 `ranking_candidates` 和可选 `validating_supplements`，成功写严格结果。
6. `CancelledError` 继续抛出；可重试失败有界退避；确定性失败直接形成安全终态。
7. 重试沿用同一逻辑发现 ID、增加 run generation，只补未完成检查点且不能刷新已锁定快照时效。
8. 功能开关关闭时发现接口在 Redis 写入前返回 503。
9. POST 提交、GET 状态、SSE revision 恢复和 POST retry 严格输出独立发现合同。
10. 内部认证、缺失队列、未知任务、幂等冲突和 Redis 故障返回稳定代码。
11. API 日志和错误不包含完整查询、补充 URL、评论、上游响应或密钥。
12. 现有正式任务路由、Worker、回调和协调器无回归。

### 实现

1. 以独立 Redis key namespace 保存发现请求、状态、幂等、事件和 active 集合，共享同一 Redis 连接但不复用 `JobState.report`。
2. 复制最小持久状态机制或抽取真正通用的小型原语；不得用大量条件分支污染现有正式任务仓储。
3. 新增 `execute_v22_competitor_discovery` ARQ 函数和队列适配器，并加入现有 WorkerSettings。
4. Worker 启动时构造共享市场仓储、V22-022 阶段和发现服务；生产正式执行器仍为 `UnavailableV22Executor`。
5. Web runtime 在同一 Redis pool 上挂载独立发现 runtime；关闭时只关闭一次共享连接。
6. 新路由使用独立 parse、feature flag、鉴权、状态转换和 SSE，不把候选结果发布到正式报告回调。
7. 发现结果状态保留 7 天，数据有效性始终由结果 `expires_at` 的 24 小时门槛判断。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_discovery_store.py tests/test_v22_competitor_discovery_queue.py tests/test_v22_competitor_discovery_worker.py tests/test_api_v2_competitors.py -q
.venv/bin/python -m pytest tests/test_v22_job_store.py tests/test_v22_job_queue.py tests/test_v22_job_worker.py tests/test_api_v2_jobs.py -q
```

### 提交

```text
feat(v2.2): run durable competitor discovery
```

## 6. Task 5：手动补充验证与正式分析关联门槛

### 文件

- 新建：`app/competitors_v22/selection.py`
- 修改：`app/competitors_v22/discovery_service.py`
- 修改：`app/competitors_v22/runtime.py`
- 修改：`app/api/v2/runtime.py`
- 修改测试：`tests/test_v22_competitor_discovery_service.py`
- 新建测试：`tests/test_v22_competitor_selection.py`
- 修改测试：`tests/test_api_v2_jobs.py`

### 先写测试

1. 初始发现前 6 可默认预选前三，但没有显式用户提交不得产生已确认集合。
2. 补充 URL 命中市场池时使用相同市场快照并产生新发现 ID；未命中、客户自身、目录、无关或身份歧义时返回安全缺口。
3. 补充首页验证复用 V22-021 安全 fetcher 边界，只读取单一有界首页，不触发 50/10 完整采集。
4. 正式分析缺少发现头、发现不存在、失败、过期、Case 错配、身份错配、服务/市场/查询/语言/设备错配均在队列写入前拒绝。
5. 三个已确认竞品必须都在当前候选集合，ID 和规范官网唯一，字段值与候选规范身份一致。
6. 合法提交把发现 ID、候选集合校验和和市场快照引用写入正式任务内部请求元数据，但原 `AnalyzeRequest` 请求体保持冻结 JSON。
7. 请求重放必须使用相同发现 ID；更换发现 ID 或选择内容形成幂等冲突。
8. 用户选择候选后 `confirmation_source=user`；系统来源不能伪装用户确认。
9. 分析功能开关关闭仍优先阻断且不读取或写入 Redis。
10. 现有合同导出和 TypeScript 生成无差异。

### 实现

1. 实现纯选择校验器，比较规范候选身份和冻结 `ConfirmedCompetitor`。
2. 对补充首页使用现有安全 URL/fetch 组件，只增强名称、服务和地址信号，不改变“必须出现于市场”的门槛。
3. 在 `/api/v2/analyze` 读取可选头并由 runtime 产生稳定业务错误；避免让 FastAPI 自动 422 替代领域错误。
4. 正式任务持久请求外层增加内部 envelope，保存原冻结请求和发现引用；Worker 兼容解析旧请求只用于现有关闭状态回归，生产启用前要求新 envelope。
5. 幂等摘要覆盖冻结请求和发现引用，防止同一付费意图换候选。
6. 通过共享市场仓储验证 24 小时时效和校验和，不延长快照过期时间。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_selection.py tests/test_v22_competitor_discovery_service.py tests/test_api_v2_jobs.py -q
.venv/bin/python scripts/export_v22_contracts.py --check
```

### 提交

```text
feat(v2.2): require confirmed competitor discovery
```

## 7. Task 6：三个竞品的 50/10 网站采集阶段

### 文件

- 修改：`app/jobs_v22/site_inventory_stage.py`
- 新建：`app/competitors_v22/site_stage.py`
- 新建测试：`tests/test_v22_competitor_site_stage.py`
- 修改测试：`tests/test_v22_site_inventory_stage.py`
- 回归测试：`tests/test_v22_site_inventory_collector.py`
- 回归测试：`tests/test_v22_site_inventory_fetcher.py`

### 先写测试

1. 抽出的通用检查点采集入口保持客户站点现有 500/50、GSC 优先和输出完全不变。
2. 竞品入口对每家固定最多发现/结构检查 50 个 URL、深度采集不超过请求 `max_competitor_pages_each` 且绝不超过 10。
3. 三个竞品使用各自根域、URL 检查点和预算，不能跨站复用正文或 canonical。
4. 竞品不读取任何 `first_party_snapshots`，不应用 GSC 优先。
5. 首页、核心服务、地区、案例和关于页在竞品选择中优先，缺少类别时不以重复低价值页填充。
6. robots、无 sitemap、大站、重复 URL、重定向、超时、响应上限、Firecrawl 补充和 SSRF 回归。
7. Firecrawl 只能补足发现且不能突破 50/10；功能关闭或失败产生限制而非扩大原生抓取。
8. 网站不可访问时返回结构化来源失败，而不是直接决定整个竞品身份无效。
9. 中断后只补缺失 URL 和深度页；完成快照重跑不发网络请求。
10. 每个竞品输出真实 `analyzed_page_count`，允许 0，不生成优势或缺口。

### 实现

1. 从现有站点阶段抽出可注入的通用 `collect_inventory_for_site` 检查点编排，参数包含根 URL、服务、市场、发现/深度上限和可选 GSC 优先。
2. 现有 `CheckpointedSiteInventoryStage.collect` 通过通用入口保持原行为。
3. 新 `CheckpointedCompetitorSiteStage` 只接收已确认竞品和选择上下文，固定发现上限 50、深度硬上限 10、GSC 为空。
4. 为三个竞品按稳定 ID/域名建立独立检查点命名空间和部分失败结果。
5. 不安装到生产 `UnavailableV22Executor`，只暴露给未来真实执行器和 Task 8 聚合阶段。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_site_stage.py tests/test_v22_site_inventory_stage.py tests/test_v22_site_inventory_collector.py tests/test_v22_site_inventory_fetcher.py -q
```

### 提交

```text
feat(v2.2): checkpoint competitor site collection
```

## 8. Task 7：公开 GBP、评论采集和 15 次尝试账本

### 文件

- 新建：`app/competitors_v22/public_profile.py`
- 新建：`app/competitors_v22/public_profile_stage.py`
- 修改：`app/integrations/serpapi.py`（仅在通用调用元数据不足时）
- 新建 fixture：`tests/fixtures/v22_competitors/maps-place.json`
- 新建 fixture：`tests/fixtures/v22_competitors/maps-reviews-page-1.json`
- 新建 fixture：`tests/fixtures/v22_competitors/maps-reviews-page-2.json`
- 新建 fixture：`tests/fixtures/v22_competitors/maps-reviews-empty.json`
- 新建测试：`tests/test_v22_competitor_public_profile.py`
- 新建测试：`tests/test_v22_competitor_public_profile_stage.py`
- 回归测试：`tests/test_serpapi_client.py`
- 回归测试：`tests/test_gbp_lookup.py`

### 先写测试

1. 市场快照已有完整名称、地址、类别、评分、评论数和 URL 时不请求详情。
2. 关键资料缺失且有 `place_id` 时最多一次 `engine=google_maps&type=place`；只有 `data_id` 时使用已确认坐标构造有界详情请求。
3. 详情强身份与已确认候选冲突时停止该竞品评论采集并返回身份失败。
4. 评论固定 `engine=google_maps_reviews`、已确认 `data_id`、`sort_by=newestFirst` 和统一语言。
5. 每页最多接受 8 条并只跟随 `serpapi_pagination.next_page_token`；不解析任意 next URL。
6. 每家最多 4 页、30 条，缺少令牌、空页、重复页、达到样本或预算时停止。
7. 优先 `review_id` 去重，无 ID 时使用评分、ISO 日期和正文摘要稳定回退。
8. 正文、回复、日期、评分、来源和健康正确标准化；头像、主页、贡献者 ID、照片和未知字段不进入模型或检查点。
9. 三家合计最多 15 次实际网络尝试，每次请求、密钥切换和网络重试前先用 Redis `NX` 占槽。
10. 第 15 个槽后不发第 16 次请求；Worker 重启、人工重试和换密钥不能重置账本。
11. 成功页检查点复用不消耗新槽；坏检查点、错误分页令牌或超限载荷安全失败。
12. 限流、5xx、网络超时和预算截断保留已取得资料和评论并产生限制。
13. 共享 SerpAPI 多密钥、冷却和脱敏行为以及 v2.1/预检 GBP 回归。

### 实现

1. 实现只接受已确认强身份的 Place Details 和 Reviews 请求规划器。
2. 使用共享 `execute_serpapi_get`，不复制密钥轮换逻辑；每个实际网络尝试调用全局账本回调。
3. 以正式任务 ID、竞品 ID、引擎、强身份、排序和分页令牌摘要建立检查点。
4. 实现有界响应净化器，只保存模型允许的详情和评论字段。
5. 预算账本跨三个竞品共享，返回详情次数、评论页次数、实际尝试、检查点命中和截断状态。
6. 把来源失败分类为身份冲突、确定性响应错误、暂时供应商错误和预算截断。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_public_profile.py tests/test_v22_competitor_public_profile_stage.py tests/test_serpapi_client.py -q
.venv/bin/python -m pytest tests/test_gbp_lookup.py tests/test_v22_preflight_gbp.py -q
```

### 提交

```text
feat(v2.2): collect bounded public competitor reviews
```

## 9. Task 8：竞品采集快照聚合与执行器边界

### 文件

- 新建：`app/competitors_v22/collection_stage.py`
- 新建测试：`tests/test_v22_competitor_collection_stage.py`
- 修改测试：`tests/test_v22_job_worker.py`
- 回归测试：`tests/test_v22_job_checkpoints.py`

### 先写测试

1. 阶段输入必须包含合法、未过期且与正式请求一致的发现结果和恰好三个确认竞品。
2. 三家按同一发现快照、目标点、国家、语言、设备、查询和采集策略执行。
3. 每家网站和公开资料结果映射到对应稳定竞品 ID，不能交叉归属。
4. 单个网站失败但 SERP/GBP 可靠时保留竞品、页面数为 0 并记录限制。
5. 单个 GBP 或评论失败时保留站点/SERP 数据；缺失不变成零分或劣势。
6. 身份整体无法追溯时返回稳定确定性错误并要求更换竞品。
7. 最终严格快照恰好三家，网站最多 10 页、评论最多 30、供应商尝试最多 15。
8. 完成快照、每家来源和计费调用均有检查点；重启只补未完成来源。
9. 成本计数确定性输出站点页、深度页、GBP 详情、评论页、实际尝试、检查点命中和截断。
10. 不生成 `strengths`、`gaps`、Finding、Action、评分或正式 `evidence_id`。
11. 生产 Worker 仍装配 `UnavailableV22Executor`；新阶段只暴露给后续完整执行器。

### 实现

1. 实现 `CheckpointedCompetitorCollectionStage`，先验证发现关联，再并发或有界顺序采集三家。
2. 组合原市场候选事实、网站库存、公开 GBP、评论和来源健康为 `CompetitorCollectionSnapshot`。
3. 对可降级来源返回结构化 limitation；只有身份不可追溯、检查点损坏和合同错配抛确定性错误。
4. 暴露纯 `competitor_collection_cost_counters` 给未来真实执行器。
5. 不修改最终报告映射，V22-031 再注册证据并构造 `CompetitorSummary`。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_collection_stage.py tests/test_v22_competitor_site_stage.py tests/test_v22_competitor_public_profile_stage.py -q
.venv/bin/python -m pytest tests/test_v22_job_checkpoints.py tests/test_v22_job_worker.py -q
```

### 提交

```text
feat(v2.2): checkpoint competitor collection snapshots
```

## 10. Task 9：全量验证与完成记录

### 文件

- 新建：`docs/superpowers/specs/2026-08-29-searchtrust-v2-2-competitor-selection-collection-completion.md`

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_competitor_*.py tests/test_api_v2_competitors.py -q
.venv/bin/python -m pytest tests/test_v22_serp_market_*.py tests/test_v22_site_inventory_*.py tests/test_serpapi_client.py -q
.venv/bin/python -m pytest tests/test_gbp_lookup.py tests/test_v22_preflight_*.py tests/test_api_v2_jobs.py -q
.venv/bin/python scripts/export_v22_contracts.py --check
.venv/bin/python -m compileall -q app tests
.venv/bin/python -m pytest -q
git diff --check
```

如果前端仓库具有既有合同差异检查，在最终验证时运行只读检查；本里程碑不手工修改前端生成文件。

### 验收

1. 逐项核对设计文档第 19 节的 15 条验收标准。
2. 确认发现候选、用户选择和正式分析关联完整可追溯。
3. 确认 24 小时、7 天、最多 6 候选、恰好 3 竞品、50/10、30 条和 15 次尝试硬上限全部有边界测试。
4. 确认自动化测试没有真实 SerpAPI、Firecrawl 或任意外部网站请求。
5. 确认冻结报告/API 合同导出无计划外差异。
6. 确认 v2.1、预检、站点库存、市场采集和持久任务全量回归通过。
7. 确认日志、错误、检查点和模型无密钥、原始供应商响应或不必要个人资料。
8. 确认生产正式执行器、安全开关和未完成报告装配保持关闭。
9. 记录测试数量、命令结果、提交列表、已知限制和下一里程碑 V22-030/031 边界。

### 提交

```text
docs(v2.2): record competitor collection completion
```

## 11. 提交顺序

```text
feat(v2.2): define competitor discovery contracts
feat(v2.2): rank traceable competitor candidates
feat(v2.2): reuse expiring market discoveries
feat(v2.2): run durable competitor discovery
feat(v2.2): require confirmed competitor discovery
feat(v2.2): checkpoint competitor site collection
feat(v2.2): collect bounded public competitor reviews
feat(v2.2): checkpoint competitor collection snapshots
docs(v2.2): record competitor collection completion
```

每个提交必须在对应专项测试通过后产生。全量回归失败时停止新增提交，先修复当前里程碑引入的问题。

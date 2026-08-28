# SearchTrust v2.2 SERP 市场采集实施计划

日期：2026-08-28

里程碑：V22-022

依赖设计：`2026-08-28-searchtrust-v2-2-serp-market-design.md`

## 1. 实施原则

- 测试先行：每项任务先补失败测试，再实现最小代码并运行相关回归。
- 不修改冻结的 `AnalyzeRequest`、`TargetMarket`、`SearchResult`、`MarketSnapshot` 或共享 JSON Schema。
- V2.2 使用独立采集模型和编排，不导入 v2.1 大型报告流程。
- SerpAPI 密钥轮换抽成共享组件，v2.1 通过兼容包装继续使用原入口。
- 原始查询不改写；单报告只使用一个坐标、设备、语言和国家上下文。
- 每查询固定两个逻辑请求，报告最多 10 个；每逻辑请求最多 3 次实际网络尝试。
- 自动化测试只使用脱敏固定响应和假服务商，不访问真实 Locations API 或付费 Search API。
- 不替换生产 `UnavailableV22Executor`，不打开 `V22_ANALYZE_ENABLED`。
- 每个任务形成小而可回退的提交；任何阶段失败先修复再进入下一阶段。

## 2. Task 1：抽取共享 SerpAPI 故障切换组件

### 文件

- 新建：`app/integrations/__init__.py`
- 新建：`app/integrations/serpapi.py`
- 修改：`app/tasks/scraper.py`
- 新建测试：`tests/test_serpapi_client.py`
- 修改测试：`tests/test_gbp_lookup.py`
- 回归测试：`tests/test_v22_preflight_gbp.py` 或现有 GBP 预检测试

### 先写测试

1. 配置密钥按主、次、第三槽去重并保持顺序。
2. 认证、权限、月度配额和 429 会冷却当前密钥并切换下一槽。
3. 服务商普通 4xx/5xx、无效 JSON 和网络错误不错误地向下一账户收费。
4. 成功切换后的活动密钥在后续请求中优先。
5. 所有密钥不可用时抛出结构化、无秘密内容的异常。
6. 每次实际网络调用前触发尝试回调，可供 V2.2 持久尝试账本限流。
7. 返回成功载荷及安全调用元数据，不返回密钥、带密钥 URL 或原始秘密。
8. v2.1 `_configured_serpapi_keys`、`_serpapi_get`、`_serpapi_key_fingerprint`、共享冷却字典和活动指纹测试继续通过。
9. V2.2 预检 `LimitedGbpLookup` 结果与异常降级语义保持。

### 实现

1. 定义 `SerpApiKeysUnavailable`、故障类别、调用元数据和可变密钥池状态。
2. 将载荷错误识别、密钥指纹、失败分类、排序、冷却和请求执行移入共享模块。
3. 共享执行器接受显式 keys、base URL、时钟、logger 和可选 `before_attempt` 回调，避免依赖大型 scraper 全局。
4. scraper 保留原符号和原函数签名；包装器在调用前后同步原活动指纹，并继续使用原冷却字典。
5. 预检暂时继续通过兼容包装调用，防止本任务扩大 GBP 重构范围。

### 验证

```bash
.venv/bin/python -m pytest tests/test_serpapi_client.py tests/test_gbp_lookup.py -q
.venv/bin/python -m pytest tests/test_v22_preflight_service.py tests/test_v22_preflight_gbp.py -q
```

如果仓库不存在第二条命令中的某个测试文件，则使用 `rg --files tests | rg 'preflight.*gbp|gbp.*preflight'` 找到实际对应测试，不创建仅为命令占位的文件。

### 提交

```text
refactor(serpapi): share secret-safe key failover
```

## 3. Task 2：配置与严格内部市场模型

### 文件

- 修改：`app/core/config.py`
- 修改：`.env.example`
- 新建：`app/collectors/serp_market_models.py`
- 新建测试：`tests/test_v22_serp_market_config.py`
- 新建测试：`tests/test_v22_serp_market_models.py`
- 修改测试：`tests/test_v22_deployment_config.py`

### 先写测试

1. Locations URL、连接/读取/总超时和最大响应字节配置有安全默认值与上下界。
2. 搜索硬限制固定为 3—5 查询、每查询 2 次、报告最多 10 次、每逻辑调用最多 3 次实际尝试、每类最多 20 条。
3. 目标点要求合法坐标、国家、来源和解析审计一致。
4. 调用记录要求引擎、结果类型、请求摘要、时间、状态、尝试和检查点状态一致。
5. 市场结果严格验证结果类型、正整数排名、URL、域名、评分、评论数、字段长度和稳定 ID。
6. 查询运行必须保持一个 Maps 和一个 Google 调用；两者成功时才是完整查询组。
7. 快照必须有 3—5 个唯一查询运行，输入顺序稳定，逻辑预算不超过 10，完整查询组不少于 3。
8. 计数、结果归属、统一上下文和开始/完成时间必须与子记录一致。
9. 所有模型拒绝额外字段、超限列表、无时区时间和秘密字段。

### 实现

1. 增加 `SERPAPI_LOCATIONS_URL` 和 `V22_SERP_MARKET_` 网络边界配置；不把查询数、逻辑预算、尝试次数或结果数硬上限变成可放大的环境配置。
2. 定义 `SerpTargetPoint`、`SerpCallRecord`、`SerpMarketResultRecord`、`SerpQueryRun`、`SerpMarketBudget` 和 `SerpMarketSnapshot`。
3. 使用冻结 `StrictModel`，为所有字符串、集合、数值和字节摘要设置上限。
4. 使用模型验证器落实查询唯一性、统一上下文、调用/结果归属、预算和完成门槛。
5. 对内部错误和限制使用固定代码或受限文本，不保存服务商任意错误正文。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_serp_market_config.py tests/test_v22_serp_market_models.py tests/test_v22_deployment_config.py -q
```

### 提交

```text
feat(v2.2): define SERP market contracts
```

## 4. Task 3：单目标点解析与确定性请求规划

### 文件

- 新建：`app/collectors/serp_market_location.py`
- 新建：`app/collectors/serp_market_requests.py`
- 新建 fixture：`tests/fixtures/v22_serp_market/locations-austin.json`
- 新建 fixture：`tests/fixtures/v22_serp_market/locations-ambiguous.json`
- 新建测试：`tests/test_v22_serp_market_location.py`
- 新建测试：`tests/test_v22_serp_market_requests.py`

### 先写测试

1. 经纬度成对提供时直接使用且不调用 Locations API；只提供一个坐标时拒绝。
2. 无坐标时按邮编、城市、区域、国家和展示名构造有界定位查询。
3. 位置结果先按国家过滤，再按邮编、城市、区域和规范名确定性排序。
4. 唯一精确结果返回规范地点、位置 ID 和正确的 `[longitude, latitude]` 转换。
5. 无结果、国家不匹配、无 GPS 和并列歧义在付费搜索前失败。
6. 位置响应字节、JSON 结构、结果数和字符串长度受限。
7. 原查询只清理首尾空白，大小写和内部内容不变化。
8. 每查询生成 `google_maps` 与 `google` 两个请求，输入 3/4/5 查询分别为 6/8/10 个。
9. Maps 参数固定 `type=search`、相同中心点和 `14z`；Google 参数固定 `lat`、`lon`、`gl`、`hl`、设备。
10. 请求不包含分页、额外位置、自动追加地点或隐式查询。
11. 规范请求摘要覆盖所有影响结果的字段，相同输入稳定、任一字段变化即不同。

### 实现

1. 定义可注入的 `SerpLocationProvider` 和生产 httpx 适配器。
2. 对显式坐标和 Locations API 结果统一生成 `SerpTargetPoint`。
3. 实现纯函数位置匹配评分；并列最高分拒绝，不使用服务商“最热门”默认。
4. 定义不可变 `SerpSearchPlan` 和每调用请求描述，不含 API key。
5. 从 `AnalyzeRequest.parent_report.case_context` 获取已冻结语言/设备；prospect 或无父报告时使用 `mobile`/`en`。
6. 在规划阶段先计算全部逻辑调用数并校验硬预算，再允许任何付费提供商调用。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_serp_market_location.py tests/test_v22_serp_market_requests.py -q
.venv/bin/python -m pytest tests/test_api_v2_contract.py tests/test_report_v22_contract.py -q
```

### 提交

```text
feat(v2.2): plan single-point SERP searches
```

## 5. Task 4：三类结果标准化与受预算市场采集器

### 文件

- 新建：`app/collectors/serp_market.py`
- 新建 fixture：`tests/fixtures/v22_serp_market/google-maps.json`
- 新建 fixture：`tests/fixtures/v22_serp_market/google-search.json`
- 新建 fixture：`tests/fixtures/v22_serp_market/empty.json`
- 新建 fixture：`tests/fixtures/v22_serp_market/malformed.json`
- 新建测试：`tests/test_v22_serp_market_collector.py`

### 先写测试

1. Maps `local_results` 映射为 `maps`；Google `local_results` 映射为 `local_pack`；`organic_results` 映射为 `organic`。
2. 映射标题/商家名、位置、URL、域名、place/data/CID、地址、电话、类型、评分、评论数和摘要。
3. 优先使用合法服务商位置；缺失时按一开始的响应顺序排名并标记来源。
4. 每查询每类最多 20 条；不解析下一页链接或发起分页。
5. 空结果是成功；未知区块和坏条目只产生有界限制。
6. 相同商家跨查询、跨结果类型保持，不提前聚合。
7. 稳定记录 ID 包含调用摘要、类型、位置和稳定身份字段。
8. 采集顺序按查询输入顺序，每查询 Maps 后 Google，任何时候逻辑调用不超过计划和 10。
9. 4—5 查询允许部分失败且至少 3 个完整查询组时成功；少于 3 个时返回稳定采集错误。
10. 已成功一侧结果在另一侧失败时保留在尝试结果中，不被伪装为完整查询组。
11. 服务商搜索 ID、响应校验摘要和时间正确进入调用与结果记录。

### 实现

1. 定义 provider protocol，使采集编排不依赖 httpx 或真实密钥。
2. 编写 Maps 和 Google Search 两个有界纯解析器。
3. 规范 URL/域名、位置、数值和文本字段；拒绝危险或超限值。
4. 为每个调用生成严格 `SerpCallRecord` 和稳定结果 ID。
5. 编排查询运行、计数、预算审计、部分失败限制和快照完成门槛。
6. 不生成 `evidence_id`，也不提供会使用占位 ID 的最终报告映射器。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_serp_market_collector.py tests/test_v22_serp_market_models.py -q
```

### 提交

```text
feat(v2.2): collect bounded SERP market snapshots
```

## 6. Task 5：持久位置、尝试账本和搜索检查点阶段

### 文件

- 新建：`app/jobs_v22/serp_market_stage.py`
- 新建测试：`tests/test_v22_serp_market_stage.py`
- 修改测试：`tests/test_v22_job_worker.py`
- 回归测试：`tests/test_v22_job_checkpoints.py`

### 先写测试

1. 位置解析、每个搜索调用、每次实际尝试和终态快照使用版本化键。
2. 显式坐标不创建位置外部调用账本；无坐标位置调用最多一次并可恢复。
3. 在每次真实 provider 请求前以 `NX` 保存尝试槽，崩溃后该槽仍计入上限。
4. 同一逻辑调用跨密钥切换和任务重启累计最多 3 个尝试槽。
5. 第三个槽耗尽后不发出第四次请求，并返回稳定查询级失败。
6. 成功调用检查点在重启后直接复用，不增加逻辑用量或尝试数。
7. 请求参数、位置、语言、设备、国家、视野或 schema 变化时使用新检查点键。
8. 损坏、超限、摘要不匹配或含额外字段的检查点安全失败。
9. 只有未完成调用重试；至少三个完整查询组时快照成功，否则阶段保持可重试或稳定失败语义。
10. `cost_counters` 所需逻辑调用、provider 尝试、检查点命中和位置调用计数可从阶段结果确定性导出。
11. 生产 Worker 仍安装 `UnavailableV22Executor`，不接入未完成报告流程。

### 实现

1. 定义版本化位置、尝试声明、调用结果和终态快照检查点模型。
2. 使用 `JobCheckpoints.get/save/run_once`；尝试声明必须在网络调用前持久化，不能只在成功后记录。
3. 将尝试槽回调注入共享 SerpAPI 请求组件，每次密钥网络调用都先占用一个槽。
4. 对成功调用只保存有界标准化载荷和安全元数据；读取时重新严格验证。
5. 将位置解析器、共享 SerpAPI provider、市场采集器和检查点包装组装为 `CheckpointedSerpMarketStage`。
6. 将服务商临时错误映射为 `TransientJobError`，配置、位置、预算和坏检查点映射为 `DeterministicJobError`。
7. 暴露纯 `cost_counters` 映射供未来真实执行器使用，但不修改当前生产执行器装配。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_serp_market_stage.py tests/test_v22_job_checkpoints.py tests/test_v22_job_worker.py -q
.venv/bin/python -m pytest tests/test_serpapi_client.py tests/test_gbp_lookup.py -q
```

### 提交

```text
feat(v2.2): checkpoint SERP market collection
```

## 7. Task 6：全量验证与完成记录

### 文件

- 新建：`docs/superpowers/specs/2026-08-28-searchtrust-v2-2-serp-market-completion.md`

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_serp_market_*.py tests/test_serpapi_client.py -q
.venv/bin/python -m pytest tests/test_gbp_lookup.py tests/test_v22_preflight_service.py -q
.venv/bin/python scripts/export_v22_contracts.py --check
.venv/bin/python -m compileall -q app tests
.venv/bin/python -m pytest -q
git diff --check
```

如果前端或合同导出脚本存在独立的生成差异命令，一并运行仓库既有命令，不以手工改动生成文件代替导出。

### 验收

1. 逐项核对设计文档第 16 节的 13 条验收标准。
2. 确认全部自动化测试没有真实 SerpAPI 网络访问。
3. 确认冻结公开合同没有无计划差异。
4. 确认工作树只包含 V22-022 计划内改动。
5. 确认生产 Worker、安全开关和未完成报告装配保持关闭。
6. 记录测试数量、命令结果、提交列表、已知限制和下一里程碑边界。

### 提交

```text
docs(v2.2): record SERP market completion
```

## 8. 提交顺序

```text
refactor(serpapi): share secret-safe key failover
feat(v2.2): define SERP market contracts
feat(v2.2): plan single-point SERP searches
feat(v2.2): collect bounded SERP market snapshots
feat(v2.2): checkpoint SERP market collection
docs(v2.2): record SERP market completion
```

每个提交必须在对应专项测试通过后产生。全量回归失败时停止新增提交，先修复当前里程碑引入的问题。


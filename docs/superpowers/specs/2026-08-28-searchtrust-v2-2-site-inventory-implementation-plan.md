# SearchTrust v2.2 全站库存实施计划

日期：2026-08-28

里程碑：V22-021

依赖设计：`2026-08-28-searchtrust-v2-2-site-inventory-design.md`

## 1. 实施原则

- 测试先行：每个任务先补失败测试，再实现最小代码并运行相关回归。
- 不修改冻结的 `AnalyzeRequest`、`GenerationLimits`、`SiteInventorySummary` 或共享 JSON Schema。
- 新采集能力进入独立 `app/collectors` 包，不调用 v2.1 报告管线。
- 复用 V22-020 已验证的 URL 公网解析与 DNS 固定连接边界。
- 不增加公开同步 API，不替换生产 `UnavailableV22Executor`，不打开 `V22_ANALYZE_ENABLED`。
- 自动化测试使用固定 DNS、HTTP、robots、sitemap、GSC 和 Firecrawl 适配器，不访问真实外网。
- 每个任务形成小而可回退的提交；任何阶段失败先修复再进入下一阶段。

## 2. Task 1：配置与严格内部库存模型

### 文件

- 修改：`app/core/config.py`
- 修改：`.env.example`
- 新建：`app/collectors/__init__.py`
- 新建：`app/collectors/site_inventory_models.py`
- 新建测试：`tests/test_v22_site_inventory_config.py`
- 新建测试：`tests/test_v22_site_inventory_models.py`
- 修改测试：`tests/test_v22_deployment_config.py`

### 先写测试

1. 库存默认并发、频率、结构/深度字节、重定向、sitemap 和批次限制符合设计。
2. 所有配置有安全上下界，默认不会打开完整分析入口。
3. 内部模型严格拒绝额外字段、超过 500 页、超过 50 个重点页和非法 URL。
4. 快照校验结构成功数、深度成功数、页面类型计数和最终选择标记的一致性。
5. GSC 优先级、发现来源、检查状态、页面类型和稳定错误类别使用受限值。

### 实现

1. 增加 `V22_SITE_INVENTORY_` 配置和环境模板。
2. 定义页面记录、深度记录、尝试审计、GSC 优先级、来源统计和完整快照模型。
3. 使用冻结 `StrictModel`，所有可变集合都设置硬上限。
4. 在模型层落实 500/50、唯一规范 URL 和计数不变量。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_config.py tests/test_v22_site_inventory_models.py tests/test_v22_deployment_config.py -q
```

### 提交

```text
feat(v2.2): define site inventory contracts
```

## 3. Task 2：URL 规范化、HTML 结构提取与页面分类

### 文件

- 新建：`app/collectors/site_inventory_urls.py`
- 新建：`app/collectors/site_inventory_html.py`
- 新建：`app/collectors/site_inventory_selection.py`
- 新建测试：`tests/test_v22_site_inventory_urls.py`
- 新建测试：`tests/test_v22_site_inventory_html.py`
- 新建测试：`tests/test_v22_site_inventory_selection.py`

### 先写测试

1. 规范化 IDNA、默认端口、空路径、默认首页、尾斜杠、片段和参数顺序。
2. 删除追踪、会话、搜索、排序和筛选参数，排除静态资源、登录和副作用路径。
3. 只接受确认主机及严格 `www` 对应形式，拒绝其他子域名和站外链接。
4. 从受限 HTML 提取 title、H1、canonical、meta robots、JSON-LD 类型和站内链接。
5. 17 类页面分类使用固定优先级，无法可靠识别时返回 `other`。
6. 相同输入始终得到相同规范 URL、分类标签、分类理由和排序键。

### 实现

1. 实现无网络副作用的 URL 纯函数和 `SiteScope`。
2. 使用标准库 `HTMLParser` 提取库存所需结构，不引入浏览器或大型解析依赖。
3. canonical 只在允许主机范围内参与去重。
4. 实现基于结构化数据、路径、title/H1 和链接关系的固定分类规则。
5. 建立后续重点页选择所需的纯评分输入。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_urls.py tests/test_v22_site_inventory_html.py tests/test_v22_site_inventory_selection.py -q
.venv/bin/python -m pytest tests/test_v22_preflight_urls.py tests/test_v22_preflight_extractors.py -q
```

### 提交

```text
feat(v2.2): classify normalized site pages
```

## 4. Task 3：安全抓取、robots 与 sitemap

### 文件

- 新建：`app/collectors/site_inventory_fetcher.py`
- 新建：`app/collectors/site_inventory_discovery.py`
- 新建 fixture：`tests/fixtures/v22_site_inventory/robots.txt`
- 新建 fixture：`tests/fixtures/v22_site_inventory/sitemap.xml`
- 新建 fixture：`tests/fixtures/v22_site_inventory/sitemap-index.xml`
- 新建测试：`tests/test_v22_site_inventory_fetcher.py`
- 新建测试：`tests/test_v22_site_inventory_discovery.py`

### 先写测试

1. 每跳重定向重新执行公网地址、固定连接和允许主机校验。
2. 结构与深度读取分别遵守 256 KB、2 MB、内容类型、超时和重定向限制。
3. 不下载子资源、不携带 Cookie、不提交表单。
4. robots 专属组、通用组、禁止规则、crawl delay、404 和临时失败语义正确。
5. 解析普通 sitemap、gzip sitemap 和有限 sitemap index。
6. 拒绝外部实体、压缩超限、过深索引、站外 URL 和危险目标。
7. 单站并发和频率限制在固定时钟测试中生效。

### 实现

1. 封装库存专用有界 GET，复用预检的 DNS 固定传输实现。
2. 将 HTTP 状态、最终 URL、媒体类型、读取字节和标准错误分类为最小结果。
3. 使用 `urllib.robotparser` 与受限 sitemap 解析器生成确定性发现结果。
4. sitemap 和页面请求共享同一 `SiteScope` 与 SSRF 边界。
5. 将单站信号量和速率限制器注入抓取器，测试可替换时钟。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_fetcher.py tests/test_v22_site_inventory_discovery.py -q
.venv/bin/python -m pytest tests/test_v22_preflight_fetcher.py tests/test_v22_preflight_urls.py -q
```

### 提交

```text
feat(v2.2): discover sites through bounded fetches
```

## 5. Task 4：组合发现与 Firecrawl 补充适配器

### 文件

- 新建：`app/collectors/site_inventory_firecrawl.py`
- 新建：`app/collectors/site_inventory.py`
- 新建 fixture：`tests/fixtures/v22_site_inventory/home.html`
- 新建 fixture：`tests/fixtures/v22_site_inventory/service.html`
- 新建测试：`tests/test_v22_site_inventory_collector.py`
- 新建测试：`tests/test_v22_site_inventory_firecrawl.py`

### 先写测试

1. 按 robots sitemap、默认 sitemap、广度优先站内链接、Firecrawl 的顺序发现。
2. 无 sitemap 时仍能通过首页链接形成库存。
3. 重复 URL、canonical、追踪参数和多来源只保留一个规范页面。
4. 达到请求限制或 500 时停止扩展，并写入截断限制。
5. 原生来源达到限制时不调用 Firecrawl；不足时每任务最多调用一次。
6. Firecrawl 未配置、限流、坏响应和外部 URL 只产生限制，不终止已有库存。
7. 根站不安全、首页不可用和空库存产生稳定确定性错误。

### 实现

1. 编排站点入口、robots、sitemap、结构检查和站内链接广度优先队列。
2. 新建 V22 Firecrawl Map 适配器，只返回裁剪后的 URL 列表和标准化状态。
3. 每个候选入队前立即执行规范化、范围、robots 和过滤规则。
4. 对所有接受的库存 URL 生成结构记录，失败记录保留但不计入结构成功数。
5. 形成稳定来源计数、分类计数和覆盖限制。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_collector.py tests/test_v22_site_inventory_firecrawl.py -q
.venv/bin/python -m pytest tests/test_scraper_content_quality.py -q
```

### 提交

```text
feat(v2.2): build bounded full site inventory
```

## 6. Task 5：重点页选择、GSC 优先与冻结摘要映射

### 文件

- 修改：`app/collectors/site_inventory_selection.py`
- 修改：`app/collectors/site_inventory.py`
- 新建：`app/collectors/site_inventory_summary.py`
- 修改测试：`tests/test_v22_site_inventory_selection.py`
- 修改测试：`tests/test_v22_site_inventory_collector.py`
- 新建测试：`tests/test_v22_site_inventory_summary.py`

### 先写测试

1. 首页、主营服务和目标市场页面优先，业务页面类型保持覆盖。
2. 博客文章和产品详情不能挤满名额。
3. 健康、未过期、身份匹配且同站的 GSC 行提高同类页面优先级。
4. 不健康、过期、身份不匹配、坏指标和站外 GSC 行被忽略。
5. 分数相同时按规范 URL 排序，重复运行结果完全一致。
6. 深度抓取失败时按排名补位；候选耗尽时保留失败审计和限制。
7. 摘要严格满足发现、结构、深度和 `selected_pages` 计数约束。
8. V22-031 前 evidence ID 默认为空，传入合法关联时正确映射。

### 实现

1. 实现类型配额、业务相关性、GSC 加权和稳定并列规则。
2. 从冻结 `FirstPartySnapshotEnvelope` 的健康 GSC 行提取标准优先级输入。
3. 使用结构结果复用和有界补抓形成最多 50 个深度页。
4. 实现 `SiteInventorySnapshot -> SiteInventorySummary` 纯映射并再次严格验证。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_selection.py tests/test_v22_site_inventory_collector.py tests/test_v22_site_inventory_summary.py -q
.venv/bin/python -m pytest tests/test_report_v22_contract.py tests/test_api_v2_contract.py -q
```

### 提交

```text
feat(v2.2): select and summarize inventory pages
```

## 7. Task 6：持久检查点阶段适配器

### 文件

- 新建：`app/jobs_v22/site_inventory_stage.py`
- 新建测试：`tests/test_v22_site_inventory_stage.py`
- 修改测试：`tests/test_v22_job_worker.py`

### 先写测试

1. 入口、robots、发现来源、结构页、选择、深度页和终态快照使用版本化键。
2. 中断后重试复用已验证检查点，不重复已完成页面或 Firecrawl 调用。
3. 检查点输入摘要变化时使用新键，不错误复用旧结果。
4. 坏检查点无法通过严格模型时安全失败，不静默污染快照。
5. 阶段适配器接收冻结 `AnalyzeRequest` 并生成严格 `SiteInventorySnapshot`。
6. 生产 Worker 仍安装 `UnavailableV22Executor`，且不导入 v1 pipeline。

### 实现

1. 定义库存检查点命名器和 JSON 模型恢复函数。
2. 将 `JobCheckpoints.run_once` 注入库存采集器的外部访问边界。
3. 以稳定 URL 摘要区分页面检查点，不把原始查询参数写入键或日志。
4. 暴露供未来真实执行器调用的 `CheckpointedSiteInventoryStage`。
5. 不修改生产 Worker 的执行器装配。

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_stage.py tests/test_v22_job_checkpoints.py tests/test_v22_job_worker.py -q
```

### 提交

```text
feat(v2.2): checkpoint site inventory collection
```

## 8. Task 7：全量验证与完成记录

### 文件

- 新建：`docs/superpowers/specs/2026-08-28-searchtrust-v2-2-site-inventory-completion.md`

### 验证

```bash
.venv/bin/python -m pytest tests/test_v22_site_inventory_*.py -q
.venv/bin/python scripts/export_v22_contracts.py --check
.venv/bin/python -m compileall -q app tests
.venv/bin/python -m pytest -q
git diff --check
```

### 验收

1. 全部新增和回归测试通过。
2. 冻结 Python/JSON Schema/TypeScript 合同无漂移。
3. 自动化测试没有真实外网或真实 Firecrawl 消耗。
4. 500/50、robots、SSRF、失败补位和检查点恢复均有固定测试。
5. v2.1 管线和生产 `UnavailableV22Executor` 保持不变。
6. 完成记录列明验证结果、未执行的真实站点烟测和下一里程碑边界。

### 提交

```text
docs(v2.2): record site inventory completion
```

# SearchTrust v2.2 Preflight API 实施计划

日期：2026-08-27

里程碑：V22-020

依赖设计：`2026-08-27-searchtrust-v2-2-preflight-design.md`

## 1. 实施原则

- 测试先行：先写失败测试，再实现最小代码，最后运行相关回归。
- 不修改冻结的 `PreflightRequest`、`PreflightResponse` 或共享 JSON Schema。
- 新代码进入独立 `app/preflight_v22` 包，不调用 v2.1 报告管线。
- 只复用 v1 中无网络副作用的纯提取函数；GBP 使用独立受限适配器。
- 生产预检开关默认关闭，浏览器不直接获得内部令牌。
- 每个任务形成一个小而可回退的提交。

## 2. Task 1：配置、URL 规范化与 SSRF 安全边界

### 文件

- 修改：`app/core/config.py`
- 新建：`app/preflight_v22/__init__.py`
- 新建：`app/preflight_v22/urls.py`
- 新建测试：`tests/test_v22_preflight_config.py`
- 新建测试：`tests/test_v22_preflight_urls.py`

### 先写测试

1. 预检开关默认关闭，缓存和抓取限制使用安全默认值。
2. 规范化主机名、IDNA、默认端口、查询参数、片段和根路径。
3. 拒绝用户信息、非 HTTP(S)、80/443 以外端口和非法主机。
4. 拒绝 IPv4/IPv6 私网、回环、链路本地、组播、保留和元数据地址。
5. 域名解析存在任一不安全地址时整体拒绝。
6. DNS 无结果返回可降级的不可达分类，不误报为 SSRF。
7. GBP URL 只接受批准的 Google Maps 主机和路径。

### 实现

1. 增加预检开关、缓存、超时、重定向和响应大小配置。
2. 定义稳定的 URL 安全异常和不可达异常。
3. 将 URL 语法规范化与异步 DNS 安全解析分离。
4. 返回已验证的解析地址集合供抓取器固定使用，避免校验与连接之间再次解析。

### 验证

```bash
python -m pytest tests/test_v22_preflight_config.py tests/test_v22_preflight_urls.py -q
```

### 提交

```text
feat(v2.2): add preflight URL safety boundary
```

## 3. Task 2：有界首页采集与纯候选提取

### 文件

- 新建：`app/preflight_v22/fetcher.py`
- 新建：`app/preflight_v22/extractors.py`
- 新建 fixture：`tests/fixtures/v22_preflight/homepage_complete.html`
- 新建 fixture：`tests/fixtures/v22_preflight/homepage_ambiguous.html`
- 新建测试：`tests/test_v22_preflight_fetcher.py`
- 新建测试：`tests/test_v22_preflight_extractors.py`

### 先写测试

1. 每次重定向重新规范化并校验，危险目标在连接前失败。
2. 连接使用已安全解析的固定地址，同时保留原始 Host 和 TLS 主机名。
3. 限制重定向次数、总超时、响应字节和 HTML 内容类型。
4. DNS 无结果、超时、5xx、非 HTML 和响应过大映射为稳定降级码。
5. 从 JSON-LD、`og:site_name`、标题、正文和地图链接提取带来源的信号。
6. 商家名称、服务、地区和经营模式候选排序稳定且去重。
7. 弱域名提示不能独立产生完整身份，缺失国家码不编造市场。

### 实现

1. 实现不执行 JavaScript、不加载子资源的首页采集器。
2. 使用流式响应读取并在超过字节限制时立即停止。
3. 为网络结果定义最小内部快照，不保存页面正文到日志或缓存。
4. 封装现有纯身份提取逻辑，并补充服务、完整地区和经营模式提取。

### 验证

```bash
python -m pytest tests/test_v22_preflight_fetcher.py tests/test_v22_preflight_extractors.py -q
python -m pytest tests/test_scraper_content_quality.py tests/test_schema_summary.py -q
```

### 提交

```text
feat(v2.2): collect bounded preflight site signals
```

## 4. Task 3：受限 GBP 适配器与候选合并

### 文件

- 新建：`app/preflight_v22/gbp.py`
- 新建：`app/preflight_v22/candidates.py`
- 新建 fixture：`tests/fixtures/v22_preflight/gbp_candidates.json`
- 新建测试：`tests/test_v22_preflight_gbp.py`
- 新建测试：`tests/test_v22_preflight_candidates.py`

### 先写测试

1. 未配置 SerpAPI 时不发请求并返回标准化不可用状态。
2. 一次未缓存预检最多执行一个 SerpAPI 搜索或详情请求。
3. 精确 GBP URL、域名搜索和页面发现 URL 使用确定性优先级。
4. 供应商密钥、原始错误和完整响应不进入结果或日志。
5. 网页与 GBP 的域名、名称、电话、地区一致时提高置信度。
6. 同名不同地区、信号冲突和分数接近时保留多个候选并要求确认。
7. 用户服务和地区候选排首位，但不同网页候选仍被保留。
8. 缺少组成 `BusinessIdentity` 的必要字段时不产生占位身份。

### 实现

1. 复用现有 SerpAPI 密钥轮换和安全请求函数，不调用 v1 GBP 深度 enrich。
2. 将供应商结果立即裁剪为预检标准化候选。
3. 使用可解释的确定性评分合并用户、网页和 GBP 信号。
4. 生成冻结合同所需的 `BusinessIdentityCandidate`、`TextCandidate` 和 `MarketCandidate`。

### 验证

```bash
python -m pytest tests/test_v22_preflight_gbp.py tests/test_v22_preflight_candidates.py -q
python -m pytest tests/test_gbp_lookup.py -q
```

### 提交

```text
feat(v2.2): resolve preflight identity candidates
```

## 5. Task 4：预检编排、模块状态与可选缓存

### 文件

- 新建：`app/preflight_v22/cache.py`
- 新建：`app/preflight_v22/service.py`
- 新建测试：`tests/test_v22_preflight_cache.py`
- 新建测试：`tests/test_v22_preflight_service.py`

### 先写测试

1. 缓存键包含缓存版本、合同版本和规范化完整输入摘要。
2. 缓存命中复用 `preflight_id` 且不重复抓取或 GBP 请求。
3. Redis 读取、坏缓存和写入失败均降级继续执行。
4. 每次响应恰好包含八个固定顺序的模块状态。
5. 缺口码、阻断语义、解决提示和顺序稳定。
6. `COMPETITOR_DISCOVERY_PENDING` 非阻断，当前不生成竞品占位符。
7. 三个耗时桶根据可用模块和阻断缺口确定性生成。
8. 覆盖说明只描述范围与缺口，不出现 Finding、Action、分数或成本。

### 实现

1. 使用 SHA-256 规范摘要和 900 秒 Redis JSON 缓存。
2. 编排安全校验、首页采集、纯提取、GBP、候选合并和响应构造。
3. 将网站及提供商故障转换为数据缺口，不吞掉编程或合同错误。
4. 在返回和缓存读取时都使用 `PreflightResponse` 严格验证。

### 验证

```bash
python -m pytest tests/test_v22_preflight_cache.py tests/test_v22_preflight_service.py -q
python -m pytest tests/test_api_v2_contract.py tests/test_report_v22_contract.py -q
```

### 提交

```text
feat(v2.2): orchestrate and cache preflight results
```

## 6. Task 5：FastAPI 接口、依赖注入与应用接入

### 文件

- 修改：`app/api/v2/dependencies.py`
- 新建：`app/api/v2/preflight.py`
- 修改：`app/main.py`
- 新建测试：`tests/test_api_v2_preflight.py`
- 修改测试：`tests/test_api_v2_internal_auth.py`

### 先写测试

1. 生产默认开关关闭时返回稳定 `503`，且不执行网络访问。
2. 缺失或错误内部令牌返回 `401`。
3. 非法 JSON、未知字段和不安全 URL 返回稳定 `422`。
4. 合法请求返回严格 `PreflightResponse` 和 HTTP 200。
5. 网站、SerpAPI 或 Redis 故障返回合同内的降级 200。
6. 路由不依赖 ARQ 运行时存在，也不导入 v1 pipeline。
7. OpenAPI 响应模型仍来自冻结合同。

### 实现

1. 从原始 JSON 严格解析 `PreflightRequest`。
2. 复用内部 Bearer 鉴权并增加独立预检功能开关。
3. 通过 FastAPI dependency 注入可替换的 `PreflightService`。
4. 注册独立 v2 preflight router，不改变 durable jobs 路由行为。

### 验证

```bash
python -m pytest tests/test_api_v2_preflight.py tests/test_api_v2_internal_auth.py -q
python -m pytest tests/test_api_v2_jobs.py tests/test_api_v2_job_stream.py -q
```

### 提交

```text
feat(v2.2): expose authenticated preflight API
```

## 7. Task 6：部署配置、全量验证与完成报告

### 文件

- 修改：`.env.example`
- 修改：`railway.json` 或现有部署配置文件（仅在需要时）
- 修改测试：`tests/test_v22_deployment_config.py`
- 新建：`docs/superpowers/specs/2026-08-27-searchtrust-v2-2-preflight-completion.md`

### 验证

```bash
python -m pytest tests/test_v22_preflight_*.py tests/test_api_v2_preflight.py -q
python scripts/check_v22_contracts.py
python -m compileall -q app tests
python -m pytest -q
git diff --check
```

### 验收

1. 全部新增和回归测试通过。
2. 冻结 Python/TypeScript 合同无漂移。
3. 生产预检开关保持关闭。
4. 没有真实外网依赖的自动化测试。
5. 完成报告记录验证结果、未执行的真实供应商烟测和后续 V22-021 接口边界。

### 提交

```text
docs(v2.2): record preflight completion
```

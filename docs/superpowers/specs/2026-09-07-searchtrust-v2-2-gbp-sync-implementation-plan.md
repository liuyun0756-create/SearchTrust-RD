# SearchTrust v2.2 GBP 只读同步实施计划

日期：2026-09-07

范围：V22-062。

前置规格：`docs/superpowers/specs/2026-09-07-searchtrust-v2-2-gbp-sync-design.md`

状态：实施与本地验证完成，生产发布待执行。

## 1. 实施原则

- 按测试先行顺序执行：先增加失败测试，再实现最小通过代码，最后运行相关回归。
- 后端 provider、数据库完成 RPC、前端 handler 和控件分开实现，每个单元可独立测试。
- 不改变 GSC、GA4、OAuth、公开 GBP、竞品发现、报告购买或 v2.1 合同语义。
- 每个阶段完成后先运行局部回归；所有阶段完成后运行前后端全量验证。
- 生产迁移和代码部署时保持 GBP 同步开关关闭，不使用 CI 或生产快速检查调用真实 GBP 商家。

## 2. 阶段一：GBP 数据库生命周期

### 任务 1：先写 GBP 任务和快照失败测试

修改：

- `search-trust/src/lib/database/v22Migration.test.ts`
- `search-trust/supabase/tests/database/v22_schema.test.sql`

增加测试：

1. GBP 任务只接受当前 Case 的 active、`matched`、已确认 GBP binding。
2. connection 必须 active 且包含 `business.manage` scope。
3. `resource_id` 必须符合 `locations/[0-9]+`，`filter_hosts` 对 GBP 必须为 null。
4. 重复请求键返回同一任务；冲突键、非所有者和 active job 冲突均拒绝。
5. claim 在 Case/binding/connection/scope 变化后失败，租约过期可重新认领。
6. finish 拒绝错 schema、错 resource、错日期、非法健康码、非法 Content 形状、超 30 天 TTL 和错摘要。
7. 正确 finish 同时写入临时 Content、长期清单、supersedes 链、binding 健康和任务完成状态。
8. 到期清理只能单向删除 `raw_payload`，记录删除时间，降级最新 binding，但不降级已有更新快照的 binding。
9. anon/authenticated 不能调用请求、认领、完成、失败或清理 RPC。

先运行：

```bash
cd /Users/liuyun3/Documents/SearchTurst前后端/search-trust
npm run test:database
```

预期：新 GBP 断言因迁移尚未存在而失败，已有 GSC/GA4 测试仍通过。

### 任务 2：实现 GBP 数据库迁移

新增：

- `search-trust/supabase/migrations/20260907100000_add_v2_2_gbp_sync.sql`

实现：

1. 扩展 `google_sync_jobs_source_type_check` 为 `gsc|ga4|gbp`，并保持 GSC/GBP 的 `filter_hosts is null` 与 GA4 host 约束。
2. 再次固定 GSC/GA4 lock 的 source 过滤，防止共享表中认领错源任务。
3. 新增 `request_v22_gbp_sync`、`lock_v22_gbp_sync`、`valid_v22_gbp_sync`、`claim_v22_gbp_sync`、`finish_v22_gbp_sync`、`fail_v22_gbp_sync`。
4. finish 接收经验证的长期 manifest 和当期 Content，数据库再次检查 schema、resource、日期、数据形状、health、checksum 和 TTL，然后原子写快照。
5. 新增只有 `service_role` 可执行的 `cleanup_v22_expired_gbp_content(batch_size)`，批量上限固定且幂等。
6. 迁移末尾写明回滚顺序：先关同步，继续清理，不破坏性删除历史记录。

再运行 `npm run test:database`，预期新旧数据库测试全部通过。

## 3. 阶段二：GBP provider、规范化和健康评估

### 任务 3：建立脱敏 fixture 和失败测试

新增：

- `SearchTrust-RD/tests/fixtures/google_connections_v22/gbp/location_complete.json`
- `SearchTrust-RD/tests/fixtures/google_connections_v22/gbp/performance_180_days.json`
- `SearchTrust-RD/tests/fixtures/google_connections_v22/gbp/keywords_page_1.json`
- `SearchTrust-RD/tests/test_v22_gbp_sync.py`

fixture 仅使用明显虚构的 Example 商家、`example.test` 网站、555 电话和非真实关键词。测试先覆盖：

- 只读 URL、固定 readMask、七项指标和 180 天日期参数；
- Location 资源 ID 不一致；
- Business Information 每个核心字段的存在/缺失；
- Performance 日期、指标、零值缺省、负值、溢出、重复和越界；
- 关键词精确值/阈值互斥、空列表、重复词、分页 token 循环和第十页截断；
- 九个 unhealthy 原因和四个非致命覆盖原因；
- 401/403/404/429/5xx、超时和未知响应。

运行：

```bash
cd /Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD
python -m pytest tests/test_v22_gbp_sync.py -q
```

预期：新测试因 GBP 模块尚未存在而失败。

### 任务 4：实现 GBP 只读 provider 和健康模型

新增：

- `SearchTrust-RD/app/google_connections_v22/gbp.py`

修改：

- `SearchTrust-RD/app/google_connections_v22/__init__.py`

`gbp.py` 保持单一责任子区域：

- 安全整数/文本/日期解析器；
- Business Information 严格模型；
- daily metric/time series 严格模型；
- search keyword/threshold 严格模型；
- 不含实际 Content 的持久 manifest；
- `GbpProvider.collect(resource_id, token, coverage_end)`；
- `evaluate_health(snapshot)` 纯函数；
- HTTP 错误到共享 `SyncError` 的固定分类。

provider 只用 GET，不包含 GoogleLocations、patch、update、create、delete、verify、reply 或发布端点。

运行 `python -m pytest tests/test_v22_gbp_sync.py -q`，预期 provider/模型/健康测试通过。

## 4. 阶段三：耐久 Worker 和独立清理

### 任务 5：扩展令牌代理与存储适配器

先在 `SearchTrust-RD/tests/test_v22_gbp_sync.py` 增加失败测试：

- GBP broker 必须要求 `business.manage` scope 和 `source=gbp`；
- GBP repository 只查询 GBP jobs；
- finish 分开传入 manifest 与临时 Content；
- cleanup 使用固定批次、安全失败分类且不暴露响应正文。

修改：

- `SearchTrust-RD/app/google_connections_v22/sync_io.py`

实现：

1. 将 GBP scope 加入 `SYNC_SCOPES`。
2. 保持 GSC/GA4 现有 finish 调用完全兼容，仅 GBP 分支发送 `p_raw_payload`。
3. 新增最小清理 repository，不需要 Google token 或 broker 配置。

运行：

```bash
python -m pytest tests/test_v22_gbp_sync.py tests/test_v22_gsc_sync.py tests/test_v22_ga4_sync.py -q
```

### 任务 6：实现 GBP Worker 与调度

新增：

- `SearchTrust-RD/app/google_connections_v22/gbp_sync_worker.py`

修改：

- `SearchTrust-RD/app/core/config.py`
- `SearchTrust-RD/app/jobs_v22/worker.py`
- `SearchTrust-RD/tests/test_v22_job_worker.py`
- `SearchTrust-RD/tests/test_v22_deployment_config.py`
- `SearchTrust-RD/tests/test_v22_gbp_sync.py`

实现：

1. 新增默认 false 的 `V22_GBP_SYNC_ENABLED`。
2. 开关开启时创建 GBP HTTP client、provider、repository 和 token broker；关闭时不创建 Google 依赖。
3. `reconcile_v22_gbp_syncs` 只分发数据库已有的用户任务，不生成新任务。
4. `execute_v22_gbp_sync` 按 claim → broker → collect → health → checksum → finish 执行，总上限 240 秒。
5. `cleanup_v22_expired_gbp_content` 使用仅存储的 client 定期执行；其生命周期不依赖 GBP 同步开关。
6. shutdown 关闭 GBP 与 cleanup client；ARQ functions/cron 注册源隔离的名称与错峰秒数。

测试确保同步关闭时不调用 Google，但存储已配置时清理仍可执行。

## 5. 阶段四：前端同步边界

### 任务 7：先写 GBP service 和 handler 失败测试

新增：

- `search-trust/src/lib/google-sync/gbp-service.test.ts`
- `search-trust/src/lib/google-sync/gbp-handlers.test.ts`

覆盖：

- 所有者、active GBP binding、`matched`、确认时间、active connection 和 GBP scope；
- UUID、body 尺寸、`confirm_sync=true`、same-origin 和幂等 request key；
- 最新 job 和最新 snapshot 独立查询；
- binding 非 matched、Content 到期/已清理时的 effective status；
- 不 select `raw_payload` 或任何 GBP 实际值；
- 安全错误码映射和 `cache-control: no-store`。

先运行：

```bash
cd /Users/liuyun3/Documents/SearchTurst前后端/search-trust
npm test -- src/lib/google-sync/gbp-service.test.ts src/lib/google-sync/gbp-handlers.test.ts
```

预期：新测试因实现尚未存在而失败。

### 任务 8：实现 GBP service、handler 和 route

新增：

- `search-trust/src/lib/google-sync/gbp-service.ts`
- `search-trust/src/lib/google-sync/gbp-handlers.ts`
- `search-trust/src/app/api/v2/cases/[id]/gbp-sync/route.ts`

实现与 GSC/GA4 一致的安全边界，但只调用 GBP RPC。route 的双端开关为 `GOOGLE_GBP_SYNC_ENABLED === "true"` 且 Google connection 基础配置有效。

运行任务 7 的局部测试，预期通过。

### 任务 9：先写 GBP 控件失败测试

新增：

- `search-trust/src/components/google/gbp-sync-control.test.tsx`

覆盖：

- 未匹配时禁用；
- 只读和 90/90 口径文案；
- queued/running 轮询；
- healthy/unhealthy 原因映射；
- failed 不隐藏旧快照；
- expired 要求重新同步；
- Content 到期时间可见，但不展示原始 GBP 值。

### 任务 10：实现控件并接入资源页

新增：

- `search-trust/src/components/google/gbp-sync-control.tsx`

修改：

- `search-trust/src/components/google/google-resource-selector.tsx`
- 如组件旗标由服务端属性传递，同步修改该页的调用入口和相关测试。

控件使用新的 `/api/v2/cases/{caseId}/gbp-sync`，用新 UUID 发起每次人工同步，每 4 秒轮询 active job，页面卸载时取消请求和计时器。

运行：

```bash
npm test -- src/components/google/gbp-sync-control.test.tsx src/lib/google-sync/gbp-service.test.ts src/lib/google-sync/gbp-handlers.test.ts
```

## 6. 阶段五：集成、安全和回归

### 任务 11：跨源回归和安全审计

检查：

- `SyncRepository` 对 GSC/GA4 的 RPC body 未变；
- GSC/GA4 lock 不可认领 GBP job；
- token broker 不会在不同 source 间复用错 scope；
- GBP 关闭时路由固定返回 `SYNC_DISABLED`；
- 浏览器 payload、日志、快照 manifest 和错误响应不包含 token、商家实际字段、指标数值或关键词；
- 工作区没有与 V22-062 无关的改动。

执行局部回归：

```bash
cd /Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD
python -m pytest tests/test_v22_gbp_sync.py tests/test_v22_gsc_sync.py tests/test_v22_ga4_sync.py tests/test_v22_job_worker.py tests/test_v22_deployment_config.py -q

cd /Users/liuyun3/Documents/SearchTurst前后端/search-trust
npm test -- src/lib/google-sync src/components/google src/lib/database/v22Migration.test.ts
```

### 任务 12：全量验证

后端：

```bash
cd /Users/liuyun3/Documents/SearchTurst前后端/SearchTrust-RD
python -m pytest -q
```

前端：

```bash
cd /Users/liuyun3/Documents/SearchTurst前后端/search-trust
npm test
npm run typecheck
npx eslint src/lib/google-sync/gbp-service.ts src/lib/google-sync/gbp-handlers.ts src/app/api/v2/cases/'[id]'/gbp-sync/route.ts src/components/google/gbp-sync-control.tsx src/components/google/google-resource-selector.tsx
npm run build
```

数据库测试需要本机数据库测试环境；若当前环境不可用，必须明确记录未运行原因，不能以纯 SQL 阅读代替通过结论。

## 7. 阶段六：文档、生产迁移和发布

### 任务 13：完成文档和发布前审核

新增：

- `SearchTrust-RD/docs/superpowers/specs/2026-09-07-searchtrust-v2-2-gbp-sync-completion.md`

记录：

- 实际改动、健康原因、测试数量和命令；
- GBP Content 临时/长期字段分界和清理证据；
- 未做真实 GBP 调用的事实；
- 两端开关名称和关闭状态；
- 发布、回滚和真实凭据验收的后续步骤。

检查两个仓库 `git diff --check`、`git status --short`、迁移顺序、环境变量默认值和密钥泄露。

### 任务 14：生产迁移和部署

发布前重新读取 Vercel deployment skill，然后按项目现有的 Vercel/Railway/Supabase 流程执行：

1. 确认正式环境 `GOOGLE_GBP_SYNC_ENABLED` 和 `V22_GBP_SYNC_ENABLED` 均不为 true。
2. 应用 `20260907100000_add_v2_2_gbp_sync.sql`，验证列、约束、函数签名、权限和无待清理违规行。
3. 发布后端 API/Worker，等待 Railway 服务 SUCCESS/RUNNING。
4. 发布前端，等待 Vercel READY 且正式域名别名指向新部署。
5. 检查 API health、queue health、首页、未登录私有路由、GBP disabled 响应、GSC/GA4 回归和最近错误日志。
6. 不进行真实 Google 授权、同步、购买或报告生成。

如发布后发现回归，立即保持或重新关闭两端 GBP 开关，回滚代码部署，保留数据表和清理函数。

## 8. 完成标准

V22-062 仅在以下条件全部满足后完成：

- 设计中的只读数据边界、严格健康规则和 30 天清理全部落地；
- 新 GBP 局部测试、跨源回归、前后端全量测试、类型检查、lint 和生产构建通过；
- 数据库迁移在正式环境应用并验证；
- 前后端部署健康，GBP 同步开关保持关闭；
- 无真实 GBP Content/token 出现在 fixture、浏览器、日志或长期 payload；
- completion 文档准确区分“代码与部署验收已完成”和“真实 GBP 凭据验收尚未完成”。

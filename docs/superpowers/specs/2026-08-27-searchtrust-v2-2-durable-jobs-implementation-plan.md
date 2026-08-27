# SearchTrust v2.2 持久任务实施计划

日期：2026-08-27  
里程碑：V22-012  
依赖设计：`2026-08-27-searchtrust-v2-2-durable-jobs-design.md`

## 1. 实施原则

- 采用测试先行：每个行为先增加失败测试，再实现最小代码，最后运行相关回归。
- 后端工作在 `SearchTrust-RD` 的 `codex/v2.2-durable-jobs` 分支完成。
- 前端工作从 `search-trust` 的 `codex/v2.2-case-api` 创建同名 `codex/v2.2-durable-jobs` 分支。
- v1 文件只允许必要的应用注册改动，不重写 `app/core/task_store.py`、`app/api/v1/analyze.py` 或 `app/tasks/pipeline.py`。
- 生产功能开关始终默认为关闭。
- 每个任务完成后提交一个小而可回退的 commit。

## 2. 依赖选择

生产依赖：

- `arq==0.28.0`；
- 使用 ARQ 安装的兼容 `redis` 客户端，并在解析依赖后锁定明确版本。

测试依赖：

- `fakeredis==2.37.1`，放入独立 `requirements-dev.txt`；
- 不把测试 Redis 模拟器装入生产镜像。

本地 Python 为 3.9，生产镜像为 3.12；选择的 ARQ 版本要求 Python 3.9 及以上。实现必须在现有本地解释器和生产镜像范围内保持兼容。

## 3. Task 1：配置、内部模型和错误分类

### 文件

- 修改：`requirements.txt`
- 新建：`requirements-dev.txt`
- 修改：`app/core/config.py`
- 新建：`app/jobs_v22/__init__.py`
- 新建：`app/jobs_v22/models.py`
- 新建：`app/jobs_v22/errors.py`
- 新建测试：`tests/test_v22_job_models.py`
- 新建测试：`tests/test_v22_job_config.py`

### 先写测试

1. 验证默认 `V22_ANALYZE_ENABLED` 为 false。
2. 验证任务最大尝试次数默认 3，状态保留默认 7 天。
3. 验证内部状态模型拒绝未知字段、非法进度和非法终态负载。
4. 验证错误分类：网络/429/5xx 可重试，合同与输入错误不可重试。
5. 验证敏感配置不会出现在模型的安全诊断输出中。

### 实现

1. 增加 Redis、Worker、回调和功能开关设置。
2. 定义内部 `JobState`、`JobErrorState`、`JobCallbackEvent` 和安全健康模型。
3. 定义稳定异常类和错误分类函数。
4. 保持冻结的 `app/api/v2/models.py` 不变，使用映射函数输出公共合同。

### 验证

```bash
python -m pytest tests/test_v22_job_models.py tests/test_v22_job_config.py -q
python -m pytest tests/test_api_v2_contract.py tests/test_report_v22_contract.py -q
```

### 提交

```text
feat: add v2.2 durable job configuration and models
```

## 4. Task 2：规范请求摘要、Redis 键和持久状态仓储

### 文件

- 新建：`app/jobs_v22/digest.py`
- 新建：`app/jobs_v22/keys.py`
- 新建：`app/jobs_v22/store.py`
- 新建测试：`tests/test_v22_job_digest.py`
- 新建测试：`tests/test_v22_job_store.py`
- 新建 fixture：`tests/fixtures/v22_job_state.json`

### 先写测试

1. 相同语义的规范 JSON 产生相同 SHA-256 摘要。
2. 数组顺序变化仍被视为不同请求。
3. 同一 `case_id + idempotency_key + digest` 并发登记只创建一个任务。
4. 相同键、不同 digest 返回 `IDEMPOTENCY_CONFLICT`。
5. 同一 job ID 不能绑定另一 Case 或另一幂等身份。
6. revision 只能递增，非法状态迁移被拒绝。
7. 第一个终态获胜，后续终态和进度更新不生效。
8. 新仓储实例可以从同一个 Redis 测试服务器恢复状态，模拟 Web 重启。
9. 终态设置固定 TTL，非终态更新刷新 TTL。
10. 活跃索引和待同步索引随状态正确维护。

### 实现

1. 使用稳定 JSON 序列化计算 `sha256:<hex>`。
2. 集中生成所有 Redis 键，避免路由和 Worker 手写键名。
3. 通过 Redis WATCH/MULTI 或等价事务实现登记和状态比较写入。
4. 状态更新后发布轻量 revision 通知；通知正文不作为状态源。
5. 提供 `register_job`、`get_state`、`transition`、`mark_callback_synced`、`list_stale_jobs` 等最小接口。

### 验证

```bash
python -m pytest tests/test_v22_job_digest.py tests/test_v22_job_store.py -q
```

### 提交

```text
feat: persist v2.2 job state and idempotency in redis
```

## 5. Task 3：ARQ 队列、Worker 与执行器边界

### 文件

- 新建：`app/jobs_v22/queue.py`
- 新建：`app/jobs_v22/executor.py`
- 新建：`app/jobs_v22/worker.py`
- 新建：`app/jobs_v22/checkpoints.py`
- 新建测试：`tests/test_v22_job_queue.py`
- 新建测试：`tests/test_v22_job_worker.py`
- 新建测试：`tests/test_v22_job_checkpoints.py`

### 先写测试

1. Web 入队只产生 ARQ 任务，不直接调用执行器。
2. 物理任务 ID 包含逻辑 job ID 和 run generation。
3. 同一物理 ID 的重复入队只保留一个任务。
4. Worker 开始时进入 running 并增加 attempt count。
5. 可恢复错误使用延迟重试，最多执行三次。
6. 确定性错误第一次即进入 failed。
7. `CancelledError` 不写失败终态，并保留可重新执行状态。
8. 两个 Worker 重复执行时只有一个终态生效。
9. 人工重试复用 job ID，增加 generation 并产生新物理任务。
10. 阶段检查点重复读取不会再次调用被保护操作。
11. 生产执行器在真实 v2 管线未接入时返回明确的不可用错误，绝不导入 v1 pipeline。

### 实现

1. 封装 ARQ pool 创建和入队，不让 API 路由依赖 ARQ 细节。
2. WorkerSettings 注册执行和协调函数，配置超时、并发、三次尝试和健康键。
3. 将异常分类映射为 ARQ Retry 或结构化失败终态。
4. 提供可注入执行器协议和测试执行器。
5. 提供 Redis 阶段检查点接口供后续 M2/M3 使用。

### 验证

```bash
python -m pytest tests/test_v22_job_queue.py tests/test_v22_job_worker.py tests/test_v22_job_checkpoints.py -q
```

### 提交

```text
feat: execute v2.2 jobs with arq workers
```

## 6. Task 4：FastAPI v2 提交、状态、SSE 和重试接口

### 文件

- 新建：`app/api/v2/dependencies.py`
- 新建：`app/api/v2/runtime.py`
- 修改：`app/api/v2/__init__.py`
- 修改：`app/main.py`
- 新建测试：`tests/test_api_v2_jobs.py`
- 新建测试：`tests/test_api_v2_job_stream.py`
- 新建测试：`tests/test_api_v2_internal_auth.py`

### 先写测试

1. 功能开关关闭时 analyze 返回 503，Redis 无写入。
2. 缺少或错误内部令牌返回 401/403。
3. 缺少 job ID 或幂等键返回稳定的 4xx 错误。
4. 合法提交返回冻结的 `TaskCreateResponse`。
5. 重复提交重放相同 job ID，冲突载荷返回 409。
6. GET 状态严格输出冻结的 `TaskStatusResponse`。
7. SSE 首次发送快照、静默发送心跳、终态后关闭。
8. 订阅后读取快照的竞态测试不会漏掉更新。
9. `Last-Event-ID` 去除重复 revision 并恢复更新版本。
10. 人工重试只允许 retryable failed 状态。
11. Redis 不可用返回 `QUEUE_UNAVAILABLE`，v1 health 仍为 200。
12. 测试断言所有 v2 运行时路径都没有调用 `app.tasks.pipeline`。

### 实现

1. 使用 FastAPI dependency 校验内部 bearer token。
2. 使用 app lifespan 建立和关闭 v2 Redis/ARQ 资源；连接失败只标记 v2 degraded。
3. 注册独立 v2 runtime router。
4. SSE 使用“先订阅、再读快照、按 revision 去重”的顺序。
5. 增加队列健康接口，输出 Redis、Worker 心跳和待同步数量。
6. CORS 不开放浏览器直接携带内部认证。

### 验证

```bash
python -m pytest tests/test_api_v2_jobs.py tests/test_api_v2_job_stream.py tests/test_api_v2_internal_auth.py -q
python -m pytest tests/test_task_stream.py tests/test_api_v2_contract.py -q
```

### 提交

```text
feat: expose durable v2.2 task api and streams
```

## 7. Task 5：签名回调与协调器

### 文件

- 新建：`app/jobs_v22/callbacks.py`
- 新建：`app/jobs_v22/reconciler.py`
- 新建 fixture：`contracts/v22_job_callback_signature.json`
- 新建测试：`tests/test_v22_job_callbacks.py`
- 新建测试：`tests/test_v22_job_reconciler.py`
- 修改：`app/jobs_v22/worker.py`

### 先写测试

1. 固定时间戳和正文产生稳定 HMAC 测试向量。
2. 签名输入包含版本、时间戳和原始正文。
3. 状态推进产生回调；相同 revision 不重复确认。
4. 回调 5xx/超时不改变任务成功或失败终态。
5. 回调失败将任务加入 `sync-pending`。
6. 协调器只补送最新 revision，成功后移出待同步索引。
7. stale running 任务在可重试时重新入队。
8. 达到最大尝试次数的孤儿进入明确失败终态。
9. 协调器重复运行不会产生第二个物理任务或第二个终态。

### 实现

1. 使用 `httpx.AsyncClient` 发送短超时回调。
2. HMAC 使用原始规范 JSON 字节，日志只记录 job ID 后缀、revision 和状态码。
3. Worker 状态更新后尽力投递，不把回调可用性混入分析结果。
4. ARQ cron 或等价周期函数运行协调器。

### 验证

```bash
python -m pytest tests/test_v22_job_callbacks.py tests/test_v22_job_reconciler.py -q
```

### 提交

```text
feat: synchronize durable job state with signed callbacks
```

## 8. Task 6：前端 analysis_jobs 版本化更新

### 分支

在前端仓库执行：

```bash
git switch codex/v2.2-case-api
git switch -c codex/v2.2-durable-jobs
```

### 文件

- 新建 migration：`supabase/migrations/20260827000000_add_v2_2_job_state_revision.sql`
- 修改：`src/types/database.ts`
- 新建：`src/lib/jobs-v22/callback-contract.ts`
- 新建：`src/lib/jobs-v22/callback-signature.ts`
- 新建：`src/lib/jobs-v22/repository.ts`
- 新建：`src/lib/jobs-v22/terminal-effects.ts`
- 新建：`src/app/api/internal/v2/job-events/route.ts`
- 复制 fixture：`src/lib/jobs-v22/fixtures/v22_job_callback_signature.json`
- 新建测试：`src/lib/jobs-v22/callback-signature.test.ts`
- 新建测试：`src/lib/jobs-v22/repository.test.ts`
- 新建测试：`src/app/api/internal/v2/job-events/route.test.ts`
- 修改数据库测试：`src/lib/database/v22Migration.test.ts`
- 修改 SQL 测试：`supabase/tests/database/v22_schema.test.sql`

### 先写测试

1. Python/TypeScript HMAC fixture 完全一致。
2. 错误签名、过期时间戳和未知签名版本被拒绝。
3. 不存在的 analysis job 返回 404，不自动创建跨边界记录。
4. 更高 revision 更新任务；相同或更低 revision 幂等忽略。
5. 终态不能回退为 queued/running。
6. failed 必须有 error code 和 completed timestamp。
7. succeeded 必须 progress=100 且无 error code。
8. 终态扩展钩子对重复回调只调用一次。
9. migration 保持旧 analysis_jobs 数据兼容。

### 实现

1. 为 `analysis_jobs` 增加 `state_revision bigint not null default 0` 和终态扩展处理标记。
2. 通过数据库函数或带 revision 条件的单条更新原子应用事件。
3. 内部路由读取原始 body 后再验签，避免重新序列化导致签名不一致。
4. 映射冻结状态字段到数据库字段。
5. `terminal-effects.ts` 仅提供幂等空实现边界，不扣 credits、不退款、不保存正式 v2.2 报告。

### 验证

```bash
npm test -- src/lib/jobs-v22 src/app/api/internal/v2/job-events/route.test.ts
npm run test:database
npm run typecheck
```

### 提交

```text
feat: apply signed v2.2 job state callbacks
```

## 9. Task 7：部署与运维配置

### 后端文件

- 修改：`docker-compose.yml`
- 修改：`railway.worker.toml`
- 修改：`railway.toml`
- 修改：`.env.example`（如存在；不存在则新建安全模板）
- 新建：`docs/operations/v22-durable-jobs.md`
- 新建测试：`tests/test_v22_deployment_config.py`

### 先写测试

1. Compose 同时定义 Web、Worker 和启用 AOF 的 Redis。
2. Worker 服务启动 ARQ Worker，不启动 Uvicorn。
3. Railway Worker 配置启动正确 WorkerSettings。
4. 生产模板中功能开关为 false。
5. 配置模板不包含真实令牌、Redis 密码或回调密钥。

### 实现

1. Compose 增加 Redis volume、AOF、健康检查和 Worker。
2. Railway Worker 改为 `arq app.jobs_v22.worker.WorkerSettings`。
3. 文档记录创建 Redis、配置 Web/Worker、轮换密钥和检查健康的方法。
4. 记录 v2 功能开关开启前的明确验收清单。

### 验证

```bash
python -m pytest tests/test_v22_deployment_config.py -q
docker compose config
```

若当前机器仍无 Docker，记录 `docker compose config` 未执行，并用结构测试覆盖配置；不得声称完成本机容器启动验证。

### 提交

```text
chore: deploy v2.2 web worker and redis services
```

## 10. Task 8：全量验证、合同稳定性和完成记录

### 后端验证

```bash
python -m pytest -q
python scripts/export_v22_contracts.py --check
git diff --check
```

若导出脚本没有 `--check` 参数，使用项目现有的可重复导出命令并检查合同目录无差异。

### 前端验证

```bash
npm test
npm run typecheck
npm run contracts:check
npm run build
git diff --check
```

同时运行可用的 PGlite 数据库测试。原生 Supabase 与 Docker 不可用时如实记录未执行项。

### 手工验收矩阵

1. 提交任务后销毁并重建 Web service 对象，GET 和 SSE 仍可读取任务。
2. 执行中取消 Worker task，重启 Worker 后任务重新执行。
3. 同一幂等请求并发提交，只有一个物理任务。
4. 三次可恢复失败后得到 `JOB_RETRY_EXHAUSTED`。
5. 成功终态重复执行不会改变 revision 或触发第二次终态扩展。
6. Next.js 回调端暂时返回 500，恢复后协调器补送最新状态。
7. `/api/v1/analyze` 回归行为不变。
8. `V22_ANALYZE_ENABLED=false` 时生产提交无法入队。

### 文档

- 新建：`docs/superpowers/specs/2026-08-27-searchtrust-v2-2-durable-jobs-completion.md`
- 记录完成项、测试数量、未执行验证、已知限制、分支和提交号。

### 最终提交

```text
docs: complete v2.2 durable jobs milestone
```

## 11. 停止条件

出现以下任一情况应停止并先修复，不进入下一任务：

- v1 回归测试失败；
- 冻结合同发生未批准变化；
- Python 需要 Supabase 管理密钥才能继续；
- 重复提交产生两个逻辑任务；
- Worker 重启后任务只能永久停留在 running；
- 终态可以被旧 revision 覆盖；
- 功能开关关闭时仍能产生队列写入；
- 测试或日志暴露内部令牌、回调密钥或客户敏感载荷。

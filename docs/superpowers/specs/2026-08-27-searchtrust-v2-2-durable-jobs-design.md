# SearchTrust v2.2 持久任务设计

日期：2026-08-27  
里程碑：V22-012  
状态：已批准，待实施

## 1. 目标

为 SearchTrust v2.2 建立可恢复的异步任务基础设施，使 Web 服务重启不会永久丢失已接收任务，并为后续公开数据采集、证据生成和报告生成提供统一执行边界。

本里程碑使用 Redis 和 ARQ 替换 v2.2 付费流程中的进程内队列。FastAPI Web 只负责验证和入队，独立 Worker 负责执行，Redis 保存可恢复状态，Next.js 继续拥有 Supabase 并幂等更新 `analysis_jobs`。

## 2. 范围

### 2.1 包含

- v2.2 Redis 连接、任务状态仓储和 ARQ 队列；
- 独立 Worker 进程及其健康检查；
- `/api/v2/analyze`、任务查询、SSE 和人工重试运行时骨架；
- 请求幂等、终态幂等、自动重试和重启恢复；
- Python 到 Next.js 的签名状态回调协议；
- `analysis_jobs` 的有序、幂等状态同步；
- 本地及 Railway 的 Web、Worker、Redis 部署配置；
- 覆盖重启、重复提交、Worker 失败、SSE 重连和终态重复执行的测试。

### 2.2 不包含

- 不迁移或改变 `/api/v1` 的现有进程内任务流程；
- 不让 v2.2 请求复用 v2.1 页面级分析管线；
- 不实现 V22-020 至 V22-034 的采集、证据、规则和报告生成器；
- 不改变现有 credits 或支付行为；
- 不实现 Case 级支付、扣费或自动返还，这些属于 V22-041；
- 不增加 `cancelled` 状态或取消接口，冻结的 v2 合同没有该状态；
- Python 不直接访问 Supabase，也不持有 Supabase `service_role` 密钥。

## 3. 已确认的产品和架构决策

1. 持久队列只服务 `/api/v2`，v1 保持兼容。
2. Next.js 独占 Supabase；Python 通过签名内部回调同步状态。
3. V22-012 只建立终态扩展钩子，支付与权益副作用在 V22-041 接入。
4. 自动重试只用于网络超时、第三方限流和 Worker 意外退出等可恢复故障，最多执行三次。
5. 输入错误、合同验证错误和确定性业务错误立即失败。
6. 人工重试复用逻辑 `job_id`，增加尝试次数，不创建第二份报告。
7. v2.2 生成器未完成前，生产环境通过 `V22_ANALYZE_ENABLED=false` 关闭提交。
8. 采用 ARQ 队列、Redis 状态快照与 Redis Pub/Sub 的组合。Pub/Sub 只负责低延迟通知，快照才是真实状态源。

## 4. 系统边界

```text
Browser
  -> Next.js v2 orchestration
      -> create/reuse analysis_jobs with an explicit UUID
      -> call FastAPI with job ID and idempotency key
          -> atomically register state in Redis
          -> enqueue an ARQ physical run
              -> Worker executes
                  -> update Redis snapshot and publish revision
                  -> send signed state callback
                      -> Next.js applies ordered update to analysis_jobs
```

### 4.1 Next.js

- 验证 Clerk 用户和 Case 所有权；
- 在请求后端前创建或复用 `analysis_jobs`；
- 明确生成 `analysis_jobs.id`，该 UUID 同时作为后端逻辑 `job_id`；
- 保存 `idempotency_key`；
- 通过内部令牌调用 Python；
- 验证 Python 回调的签名、时间戳和事件版本；
- 作为将来浏览器任务查询和 SSE 的授权代理；
- 在 V22-041 将支付或权益副作用接入终态事务。

### 4.2 FastAPI Web

- 校验冻结的 `AnalyzeRequest`；
- 校验内部鉴权、`X-SearchTrust-Job-ID` 和 `Idempotency-Key`；
- 计算规范请求摘要；
- 原子创建或复用 Redis 任务；
- 只调用 ARQ 入队，不执行分析；
- 从 Redis 快照提供任务查询与 SSE；
- 当 Redis 不可用时明确返回服务不可用。

### 4.3 ARQ Worker

- 是 v2.2 任务的唯一执行方；
- 在每次物理执行开始时更新尝试次数和心跳；
- 将可恢复异常转换为有限次数的延迟重试；
- 将确定性异常转换为结构化失败终态；
- 通过 Redis 原子操作提交唯一终态；
- 不在被关闭取消时写入失败终态；
- 推送或补送 Next.js 状态回调。

### 4.4 Redis

- 保存 ARQ 队列和物理执行信息；
- 保存逻辑任务输入、状态快照、递增版本和终态结果；
- 保存活跃任务索引和待同步回调索引；
- 发布任务版本变化通知；
- 必须使用启用持久化的部署。

## 5. 身份、幂等和请求摘要

### 5.1 三种身份

- `case_id`：客户项目身份；
- `job_id`：逻辑任务身份，与 `analysis_jobs.id` 相同；
- physical run ID：ARQ 执行身份，格式为 `v22:{job_id}:run:{run_generation}`。

逻辑任务跨自动和人工重试保持同一个 `job_id`。只有人工重试增加 `run_generation` 并创建新的 ARQ 物理任务。

### 5.2 入队幂等

Next.js 发送：

- `X-SearchTrust-Job-ID: <uuid>`；
- `Idempotency-Key: <bounded stable key>`；
- `Authorization: Bearer <internal token>`。

后端使用规范 JSON 的 SHA-256 作为 `request_digest`，并以 `case_id + idempotency_key` 建立原子映射：

- 映射不存在：创建逻辑任务并入队；
- 映射存在且摘要相同：重放原始 202 接收结果，不重新入队；
- 映射存在但摘要不同：返回 `409 IDEMPOTENCY_CONFLICT`；
- 同一 `job_id` 与另一 Case、幂等键或摘要冲突：返回 409。

ARQ 的 `_job_id` 仅负责物理执行去重，不能替代业务幂等。

### 5.3 终态幂等

Redis 状态写入使用版本检查和状态迁移检查：

- 非终态不能覆盖终态；
- 第二个成功或失败写入不会改变第一个终态；
- 终态回调事件 ID 使用 `{job_id}:{revision}`；
- Next.js 只接受比数据库现有状态版本新的事件；
- V22-041 的扣费、返还和报告落库必须在同一个数据库事务中绑定该终态版本。

## 6. Redis 数据模型

推荐键前缀可通过环境变量配置，默认使用 `searchtrust:v22`。

```text
searchtrust:v22:job:{job_id}:request
searchtrust:v22:job:{job_id}:state
searchtrust:v22:idem:{case_id}:{idempotency_key_hash}
searchtrust:v22:active
searchtrust:v22:sync-pending
searchtrust:v22:events:{job_id}
```

状态快照至少包含：

```json
{
  "job_id": "uuid",
  "case_id": "uuid",
  "status": "queued|running|succeeded|failed",
  "stage": "frozen JobStage",
  "progress": 0,
  "message": "safe user-facing text",
  "attempt_count": 0,
  "run_generation": 1,
  "revision": 1,
  "request_digest": "sha256:...",
  "heartbeat_at": "ISO-8601",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "completed_at": null,
  "report": null,
  "error": null,
  "cost_counters": {},
  "callback_synced_revision": 0
}
```

状态和结果默认保留七天。每次更新刷新非终态 TTL；进入终态后设置固定 TTL。长期审计由 Supabase 保存。

## 7. 生命周期和重试

允许的主要状态迁移：

```text
queued -> running -> succeeded
                  -> failed

failed(retryable) -> queued -> running
```

### 7.1 自动重试

- 每个逻辑任务最多执行三次；
- 可恢复错误使用指数退避并增加小幅随机抖动；
- 网络超时、第三方 429/5xx、临时连接失败属于可恢复错误；
- 合同验证、非法输入、证据不满足确定性前置条件属于不可恢复错误；
- 达到上限后写入 `JOB_RETRY_EXHAUSTED`；
- Worker 的 `CancelledError` 必须继续抛出，让 ARQ 的悲观执行机制保留任务。

### 7.2 人工重试

`POST /api/v2/tasks/{job_id}/retry` 仅接受：

- 当前状态为 `failed`；
- `error.retryable` 为 true；
- 调用方通过内部鉴权；
- 没有另一物理执行正在运行。

操作原子地增加 `run_generation`，清空终态错误和完成时间，进入 `queued`，并入队新的物理 run ID。响应使用冻结的 `RetryTaskResponse`。

### 7.3 阶段检查点

ARQ 明确采用至少一次执行语义，后续采集和生成模块不能假设任务只执行一次。昂贵或有外部副作用的步骤必须通过稳定阶段键保存完成结果，重跑时先读取检查点。V22-012 提供检查点接口和测试替身，具体阶段在后续里程碑接入。

## 8. API 和错误语义

### 8.1 运行时接口

- `POST /api/v2/analyze`；
- `GET /api/v2/tasks/{job_id}`；
- `GET /api/v2/tasks/{job_id}/stream`；
- `POST /api/v2/tasks/{job_id}/retry`；
- `GET /api/v2/health/queue`。

任务正文继续严格使用 V22-003 冻结的 Pydantic 合同，不接收 Google refresh token。

### 8.2 稳定错误

- `V22_ANALYSIS_NOT_READY`：生产功能开关关闭；
- `QUEUE_UNAVAILABLE`：Redis/ARQ 不可用；
- `IDEMPOTENCY_CONFLICT`：幂等身份对应不同请求；
- `JOB_NOT_FOUND`：任务不存在或已超过保留期；
- `JOB_NOT_RETRYABLE`：任务状态不允许人工重试；
- `JOB_RETRY_EXHAUSTED`：自动重试达到上限；
- `JOB_TIMEOUT`：执行超过配置上限；
- `INTERNAL_AUTH_FAILED`：内部调用认证失败。

错误响应和日志不得包含内部令牌、Redis URL、客户完整输入或供应商原始响应。

## 9. SSE 恢复语义

SSE 不把 Pub/Sub 当作持久事件日志，客户端只需要恢复最新状态：

1. 服务端先订阅任务通知频道；
2. 再读取当前 Redis 快照；
3. 将快照按 `revision` 发送为 `event: state`；
4. 后续通知到达时重新读取快照，而不是信任通知正文；
5. 使用 `Last-Event-ID` 去除重复版本；
6. 静默期间发送注释型心跳，不增加 revision；
7. 成功或失败事件发出后关闭连接。

这个顺序避免在“读取快照”和“建立订阅”之间丢失状态变化。即使 Pub/Sub 消息丢失，断线重连仍会从快照恢复。

## 10. Python 到 Next.js 的状态同步

### 10.1 回调安全

Python 对原始请求体计算 HMAC-SHA256，发送：

- 回调事件 ID；
- Unix 时间戳；
- 签名版本；
- 签名值。

Next.js 使用常量时间比较验证签名，拒绝超过允许时钟偏差的请求，并验证 `analysis_jobs.id` 与事件 `job_id` 一致。

### 10.2 有序应用

回调载荷包含 `job_id`、`revision`、状态、阶段、进度、尝试次数、心跳、结构化错误和安全成本计数。Next.js 只应用更高 revision：

- 重复 revision 返回成功但不重复更新；
- 更低 revision 返回成功但忽略；
- 更高 revision 更新 `analysis_jobs`；
- 终态后拒绝任何非终态倒退。

数据库增加任务状态版本字段，并通过数据库函数或等价原子条件更新，避免两个无服务器请求乱序覆盖。

### 10.3 回调失败

- 分析结果不因回调暂时失败而改写为失败；
- Redis 将任务加入 `sync-pending`；
- 协调任务按退避策略补送最新快照；
- 只补送最新 revision，不重放所有中间进度；
- 终态在 Redis 保留期内持续补送，健康接口暴露待同步数量。

V22-012 的回调只更新 `analysis_jobs` 并触发空的终态扩展接口。V22-041 再把 Case 权益和正式报告事务接入该接口。

## 11. 功能开关

默认配置：

```text
V22_ANALYZE_ENABLED=false
```

关闭时：

- v2 任务查询和健康接口仍可运行；
- `POST /api/v2/analyze` 在任何 Redis 写入前返回 503；
- 不调用 v2.1 管线；
- 测试可注入确定性执行器验证完整任务生命周期。

只有 V22-020 至 V22-034 的真实生成链路通过验收后，生产环境才允许开启。

## 12. 配置

后端新增配置类别：

- Redis URL、键前缀和队列名；
- v2 功能开关；
- 内部 API 令牌；
- 回调 URL、HMAC 密钥和允许时钟偏差；
- Worker 并发数、任务超时、最大尝试次数；
- 状态保留期、心跳周期和失联阈值；
- 回调超时和补送策略。

敏感配置不提供可用于生产的默认值。v1 进程启动不依赖 Redis；Redis 故障只让 v2 健康状态降级。

## 13. 部署

### 13.1 本地

Compose 增加：

- Redis 服务并启用 AOF；
- FastAPI Web 服务；
- ARQ Worker 服务；
- Redis 和 Worker 健康检查及依赖关系。

### 13.2 Railway

- Web 服务继续使用 `railway.toml`；
- `railway.worker.toml` 改为启动 ARQ Worker，而不是第二个 Uvicorn；
- Web 和 Worker 连接同一个持久 Redis；
- Worker 心跳写入独立键；
- Web 健康与队列健康分离，避免 Redis 故障错误触发 v1 全站下线。

## 14. 故障恢复

### 14.1 Web 重启

Web 不拥有运行任务。重启后重新建立 Redis 连接，任务查询和 SSE 从状态快照恢复。

### 14.2 Worker 重启

Worker 停止领取新任务，给运行任务有限的优雅退出窗口。被取消的执行不写失败终态，ARQ 保留任务供重启后或其他 Worker 重新领取。

### 14.3 Redis 不可用

Web 对 v2 提交返回 503，Worker 由平台重启策略恢复。禁止把仅写入内存的任务作为成功响应返回。v1 继续使用现有路径。

### 14.4 协调任务

周期协调器检查：

- 心跳超过阈值的 `running` 任务；
- 活跃状态但缺少可运行物理任务的孤儿；
- 已达到最大尝试次数但未进入终态的任务；
- `callback_synced_revision < revision` 的待同步任务。

协调器本身也必须幂等，只能把任务重新排队或推进到明确失败终态，不能生成第二份报告。

## 15. 测试策略

### 15.1 后端单元测试

- 允许和拒绝的状态迁移；
- 请求规范化摘要稳定性；
- 相同幂等键重放；
- 不同载荷幂等冲突；
- 终态首次写入获胜；
- 可恢复与确定性异常分类；
- 自动与人工重试计数；
- 内部令牌校验和日志脱敏；
- 回调签名向量。

### 15.2 Redis/ARQ 集成测试

- 并发重复入队只产生一个物理任务；
- Worker 中断后任务重新领取；
- 三次失败后进入明确终态；
- Web 客户端重建后仍能读取任务；
- 回调失败进入待同步并成功补送；
- 协调器恢复孤儿或明确关闭任务。

### 15.3 SSE 测试

- 首次连接立即获得当前快照；
- 静默任务产生心跳；
- 更新通知产生递增 revision；
- `Last-Event-ID` 去重；
- 断线期间变化在重连后恢复最新状态；
- 终态事件后关闭流。

### 15.4 前端测试

- HMAC 正确、错误和过期时间戳；
- 回调重复、乱序和终态倒退；
- `analysis_jobs` 状态字段映射；
- 数据库版本条件更新；
- 跨用户任务代理拒绝；
- 未来终态副作用钩子只调用一次。

### 15.5 回归

- 后端现有 v1 和 v2.1 测试全部通过；
- v2.2 冻结合同重新导出无差异；
- 前端 Case API、数据库迁移、合同和类型检查继续通过。

## 16. 验收标准

V22-012 只有同时满足以下条件才完成：

- Web API 不直接执行 v2.2 分析；
- Web 重启不会删除排队或运行任务；
- Worker 意外退出后任务可重新执行或进入明确失败终态；
- 同一生成意图并发提交不会产生两个逻辑任务；
- 同一逻辑任务不能提交两个终态；
- SSE 重连可以恢复最新持久状态；
- Python 不持有 Supabase 管理密钥；
- `analysis_jobs` 回调更新防重复、防乱序；
- 自动重试不超过三次，确定性错误不重试；
- v1 行为与测试保持不变；
- 生产环境 v2 分析开关保持关闭；
- 本地和 Railway 均有独立 Worker 启动配置与健康信号。

## 17. 风险和缓解

### 17.1 至少一次执行导致重复外部调用

缓解：逻辑/物理任务身份分离、阶段检查点、终态锁，以及后续 provider 调用使用稳定幂等标识。

### 17.2 Redis 数据丢失

缓解：要求持久 Redis、启用 AOF、Supabase 长期审计、明确 Redis 不可用错误，并在后续加入备份和恢复演练。

### 17.3 回调乱序或 Next.js 暂时不可用

缓解：revision 条件更新、终态不可逆、仅补送最新快照、七天终态保留和待同步健康指标。

### 17.4 v2 基础设施误用 v2.1 管线

缓解：代码模块、路由、队列名和 Worker 函数完全分离；生产功能开关默认关闭；测试断言 v1 pipeline 从未被调用。

### 17.5 过早耦合支付

缓解：V22-012 只定义一次性终态扩展点，实际扣费与权益返还延迟到 V22-041。

## 18. 参考资料

- [ARQ 官方文档](https://github.com/python-arq/arq/blob/main/docs/index.rst)：悲观执行、任务唯一性、重试和健康检查。
- [ARQ enqueue_job 实现](https://github.com/python-arq/arq/blob/main/arq/connections.py)：固定 job ID 的 Redis 事务去重和任务过期语义。
- `docs/superpowers/specs/2026-08-26-searchtrust-v2-2-development-plan.md`。
- `docs/superpowers/specs/2026-08-26-searchtrust-v2-2-contract-design.md`。

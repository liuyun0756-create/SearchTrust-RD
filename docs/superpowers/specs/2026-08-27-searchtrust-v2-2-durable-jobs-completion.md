# SearchTrust v2.2 持久任务完成记录

日期：2026-08-27  
里程碑：V22-012  
状态：工程实现完成，生产功能开关保持关闭

## 1. 完成结果

V22-012 已在前后端两个仓库实现：

- FastAPI v2 Web 只验证、登记和入队，不执行分析；
- 独立 ARQ Worker 执行 v2.2 物理任务；
- Redis 保存请求、幂等身份、状态快照、revision、结果和回调同步状态；
- Worker 退出后任务可重新执行，确定性失败不会重试，可恢复失败最多执行三次；
- 同一 Case、幂等键和请求摘要只创建一个逻辑任务；
- 同一 job ID 不能绑定另一 Case、幂等键或请求摘要；
- Redis 原子终态锁防止重复成功、重复失败和终态倒退；
- `/api/v2/analyze`、任务查询、SSE、人工重试和队列健康接口已实现；
- SSE 使用“先订阅、再读取快照”和 revision 去重，断线后恢复最新状态；
- Python 使用时间戳 HMAC 回调 Next.js，不持有 Supabase 管理密钥；
- Next.js 通过数据库函数按 revision 原子更新 `analysis_jobs`，拒绝重复、乱序和终态倒退；
- 终态扩展点已经建立但保持无副作用，等待 V22-041 接入 Case 权益和报告事务；
- Compose 和 Railway 已拆分 FastAPI Web、ARQ Worker 与持久 Redis；
- v1 进程内任务代码和运行语义保持不变；
- `V22_ANALYZE_ENABLED` 默认并持续为 false，真实 v2.2 管线完成前不会生产入队。

## 2. 后端提交

分支：`codex/v2.2-durable-jobs`

- `0d3d487` `docs: design v2.2 durable jobs`
- `f899f3f` `docs: plan v2.2 durable jobs implementation`
- `cef20d4` `feat: add v2.2 durable job configuration and models`
- `2bfbed3` `feat: persist v2.2 job state and idempotency in redis`
- `ae21039` `feat: execute v2.2 jobs with arq workers`
- `5cff213` `feat: expose durable v2.2 task api and streams`
- `48e9809` `feat: synchronize durable job state with signed callbacks`
- `bc0b5e3` `chore: deploy v2.2 web worker and redis services`
- `8169d33` `fix: bind durable jobs to one idempotency identity`
- `66dfffa` `test: verify v2.2 worker restart recovery`

## 3. 前端提交

分支：`codex/v2.2-durable-jobs`

- `ffac4c5` `feat: apply signed v2.2 job state callbacks`

该提交基于已经完成的 V22-011 Case API 分支。

## 4. 主要实现位置

### 后端

- `app/jobs_v22/`：状态模型、Redis 仓储、ARQ 队列、Worker、检查点、回调和协调器；
- `app/api/v2/runtime.py`：提交、查询、SSE、重试和队列健康；
- `app/api/v2/dependencies.py`：内部鉴权、功能开关和严格 JSON 解析；
- `app/main.py`：可降级的 v2 Redis 生命周期；
- `docker-compose.yml`：本地 Redis AOF、Web 和 Worker；
- `railway.worker.toml`：独立 ARQ Worker；
- `docs/operations/v22-durable-jobs.md`：部署、健康、恢复和密钥轮换。

### 前端

- `supabase/migrations/20260827010000_add_v2_2_job_state_revision.sql`：revision 字段和原子事件函数；
- `src/app/api/internal/v2/job-events/route.ts`：签名回调入口；
- `src/lib/jobs-v22/`：回调合同、HMAC、仓储、处理器和终态扩展点；
- `src/proxy.ts`：允许 HMAC 内部回调绕过 Clerk，其他访问仍由签名拒绝；
- `src/types/database.ts`：`analysis_jobs` revision 类型。

## 5. 验证结果

### 后端

- `325 passed`：全部 pytest 测试通过；
- v2.2 合同 `--check` 通过，无 Schema/fixture 漂移；
- `compileall` 通过；
- `git diff --check` 通过；
- 覆盖并发重复提交、不同载荷冲突、同 job ID 不同幂等身份冲突；
- 覆盖 Web 状态仓储重建恢复；
- 覆盖 Worker 取消后重新执行成功；
- 覆盖三次可恢复失败、确定性失败、终态重复执行；
- 覆盖 SSE 快照、心跳、Last-Event-ID 和终态关闭；
- 覆盖签名向量、回调失败补送、失联任务协调；
- 覆盖 v2 队列不可用时 v1 health 仍正常。

### 前端

- `54 passed`：全部 Vitest 测试通过；
- 其中 PGlite v2.2 migration 场景 `10 passed`；
- TypeScript `tsc --noEmit` 通过；
- 合同重新生成无差异；
- Next.js 16.2.4 生产构建通过；
- 构建结果包含 `/api/internal/v2/job-events` 动态路由；
- 覆盖 Python/TypeScript 共享 HMAC 向量；
- 覆盖错误签名、过期请求、未知任务、重复/乱序 revision 和终态一次性扩展。

## 6. 未在当前机器执行的验证

- 当前机器没有 Docker，因此没有执行 `docker compose config` 或本地三容器启动；
- 当前机器没有原生 `redis-server`，Redis/ARQ 行为使用 fakeredis、队列适配测试和 Worker 直接恢复测试验证；
- 没有运行原生 Supabase CLI；数据库迁移使用 PGlite 完整加载全部 migration 并执行 10 个场景；
- 没有部署或连接 Railway 托管 Redis；线上持久化、网络和平台重启仍需部署环境 smoke test。

这些限制不被记录为已验证项。上线前必须按运维文档完成真实 Redis、独立 Worker 和签名回调演练。

## 7. 已知限制与后续接入

1. `UnavailableV22Executor` 会明确返回 `V22_PIPELINE_NOT_READY`；生产功能开关关闭，因此不会被真实付费请求调用。
2. V22-020 至 V22-034 必须实现真实采集、检查点和报告生成器后才能开启 v2 分析。
3. V22-041 必须在数据库终态事务中接入 Case 权益确认、技术失败返还和正式报告落库。
4. ARQ 0.28.0 当前处于维护模式，业务代码已通过 `JobQueue` 适配层隔离，后续可替换队列实现而不改变状态合同。
5. Redis 终态默认保留七天；Supabase `analysis_jobs` 是长期审计来源。
6. v2 浏览器任务代理和 Case 所有权校验将在实际 v2 生成入口实现时接入；浏览器当前不能直接获得 Python 内部令牌。

## 8. 验收结论

V22-012 的代码范围和自动化验收已经完成。它满足“处理中重启 Web 服务不会永久丢失任务”的架构要求，并且 Worker 中断能够重新执行或进入明确失败终态。

生产上线状态仍为安全关闭：必须完成真实 Redis/Railway smoke test以及后续 v2.2 生成管线后，才能批准开启 `V22_ANALYZE_ENABLED`。

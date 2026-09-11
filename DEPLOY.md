# SearchTrust V2.2 部署说明

正式环境由两个 Railway 服务组成，并共享同一个 Redis：

- Web：FastAPI HTTP 服务
- Worker：ARQ 后台任务服务

两项服务均从 GitHub `main` 部署，并开启 Railway **Wait for CI**。只有后端质量与 Redis 集成检查通过后才允许部署该提交。

## 部署前

1. 将 `.env.example` 中启用模块需要的变量配置到 Railway。
2. Web 与 Worker 使用相同的 `V22_REDIS_URL`、`V22_REDIS_PREFIX` 和 `V22_QUEUE_NAME`。
3. Web 使用 `railway.toml`；Worker 使用 `railway.worker.toml` 或等价启动命令。
4. 确认 Supabase 迁移已经人工执行并验收。部署代码不会代替数据库迁移。
5. 本地运行完整门禁：

```bash
.venv/bin/python scripts/run_v22_backend_quality.py release
```

## 启动方式

Web：

```bash
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --loop uvloop --http httptools
```

Worker：

```bash
arq app.jobs_v22.worker.WorkerSettings
```

Dockerfile 也支持通过 `SEARCHTRUST_SERVICE_ROLE=worker` 选择 Worker 角色。

## 上线验收

- GitHub Actions 的 Backend quality 与 Redis integration 均成功。
- Railway Web 与 Worker 部署的是同一个已通过检查的提交。
- 两项部署均为 `SUCCESS`，启动日志无 error 级别事件。
- `GET /api/v1/health` 返回 HTTP 200 和 `{"status":"ok","version":"1.0.0"}`。
- `GET /api/v2/health/queue` 在携带内部令牌时返回队列健康状态。
- V2.2 预检、竞品发现、分析任务、状态查询与事件流可用。
- 旧 V2.1 分析与任务接口返回 404。

## 发布与回滚

正常发布：提交并推送到 `main`，等待 GitHub Actions 完成，再确认 Railway 自动部署。不要绕过 Wait for CI 手动部署未通过测试的代码。

需要回滚时，在 Railway 的 Web 与 Worker 中选择同一个上一版成功部署，避免两个服务运行不同提交。回滚完成后重新检查健康接口和队列状态。

# SearchTrust V2.2 Backend

SearchTrust V2.2 的 FastAPI Web 服务与 Redis/ARQ Worker。Web 服务负责预检、竞品发现和任务 API；Worker 负责可恢复的数据采集、报告生成、结果保存与回调。

## 架构

- Web：`app.main:app`
- Worker：`app.jobs_v22.worker.WorkerSettings`
- 持久任务状态：Redis
- 报告结果：Supabase
- 公开商家与市场数据：SerpAPI
- 网站采集：安全直连与 Firecrawl 降级
- 报告话术：V2.2 Dify copy provider

V2.1 报告、旧分析接口和进程内任务队列已退役，不再提供兼容路径。

## 主要接口

- `GET /api/v1/health`：公开健康检查，响应固定为 `{"status":"ok","version":"1.0.0"}`。
- `POST /api/v2/preflight`：网站与公开商家信息预检。
- `POST /api/v2/competitors/discover`：竞品发现。
- `POST /api/v2/analyze`：创建 V2.2 报告任务。
- `GET /api/v2/tasks/{job_id}`：查询任务状态。
- `GET /api/v2/tasks/{job_id}/stream`：任务事件流。
- `DELETE /api/v2/tasks/{job_id}`：取消任务。
- `GET /api/v2/health/queue`：内部队列健康状态。

除公开健康检查外，V2.2 API 使用 `Authorization: Bearer <V22_INTERNAL_API_TOKEN>`。

## 本地运行

```bash
cp .env.example .env
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Worker 需要可用的 `V22_REDIS_URL`：

```bash
.venv/bin/arq app.jobs_v22.worker.WorkerSettings
```

## 质量门禁

```bash
.venv/bin/python scripts/run_v22_backend_quality.py fast
.venv/bin/python scripts/run_v22_redis_integration.py
.venv/bin/python scripts/run_v22_backend_quality.py release
```

测试会封锁非预期网络连接；Redis 集成测试只允许使用隔离的测试数据库与自动生成的键前缀，不得连接生产 Redis、Supabase 或第三方服务。

## 关键配置

完整模板见 `.env.example`。正式环境至少需要按启用模块配置：

- `V22_REDIS_URL`、`V22_REDIS_PREFIX`、`V22_QUEUE_NAME`
- `V22_INTERNAL_API_TOKEN`
- `V22_CALLBACK_URL`、`V22_CALLBACK_SECRET`
- `V22_SUPABASE_URL`、`V22_SUPABASE_SERVICE_ROLE_KEY`
- `V22_DIFY_API_KEY`、`V22_DIFY_API_URL`
- `SERPAPI_KEY`（可配置三个顺序故障转移的 Key）
- `FIRECRAWL_API_KEY`
- `V22_ANALYZE_ENABLED`、`V22_PREFLIGHT_ENABLED`、`V22_COMPETITOR_DISCOVERY_ENABLED`

Google Search Console、GA4 与官方 GBP 同步为独立可选开关；公开 GBP 证据使用 SerpAPI，不要求用户拥有官方 GBP 后台。

### P1-09 示例文案提供方

| Worker 变量 | 默认值 | 用法 |
| --- | --- | --- |
| `V22_ACTION_EXAMPLE_API_URL` | `https://api.openai.com/v1` | 填 HTTPS API 基址。Worker 会附加 `/chat/completions`，请求不会跟随重定向。 |
| `V22_ACTION_EXAMPLE_MODEL` | `gpt-6-luna` | 填该提供方的模型 ID。模型需要支持 `response_format=json_object` 和 `max_completion_tokens`。 |
| `V22_ACTION_EXAMPLE_REASONING_EFFORT` | 空 | 留空或不设时省略该请求字段。显式设置时，填模型支持的档位。 |

Comfly 使用以下配置，`V22_ACTION_EXAMPLE_REASONING_EFFORT` 留空或不设。

```dotenv
V22_ACTION_EXAMPLE_API_URL=https://ai.comfly.org/v1
V22_ACTION_EXAMPLE_MODEL=gpt-6.2-sol
```

将所选提供方配套的密钥存入 Worker 的 `OPENAI_API_KEY`。这三项配置本身不会启用例文。

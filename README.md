# SEO Trust Path Analysis Service

异步 SEO 页面诊断后端服务。输入一个页面 URL，系统自动抓取页面内容、拉取 Google Business Profile 数据，交由 Dify AI 工作流生成 SEO 诊断报告。

## 技术栈

| 层次 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 异步任务 | asyncio 内部队列（进程内，无需 Celery/Redis）|
| HTTP 连接 | httpx 全局共享连接池（Jina / Firecrawl / SerpAPI / Dify）|
| 网页抓取 | Jina Reader（首选）→ Firecrawl（降级）|
| AI 分析 | Dify Workflow（SSE 流式）|
| 商家数据 | SerpAPI → Google Business Profile + Reviews |
| 容器化 | Docker + docker-compose |

## 项目结构

```
app/
├── main.py                  # FastAPI 入口，CORS / 日志 / 异常处理 / 连接池生命周期
├── api/v1/analyze.py        # HTTP 接口层 + 内部任务队列调度
├── models/
│   ├── request.py           # AnalyzeRequest（URL / 页面类型 / 语言 / GBP URL）
│   └── response.py          # 任务状态 / 进度 / 响应模型
├── core/
│   ├── config.py            # 所有配置项（从 .env 读取）
│   └── task_store.py        # 进程内任务状态存储 + SSE 订阅队列
└── tasks/
    ├── pipeline.py          # 核心任务编排（asyncio 协程）
    ├── scraper.py           # 数据采集层（抓取 / GBP / 评论）
    └── dify_client.py       # Dify SSE 调用 + 进度回调
```

## 请求流程

```
POST /api/v1/analyze
    │
    └─ 验证请求（SSRF 防护）
       → 生成 task_id，写入进程内 task_store
       → 放入内部 asyncio.Queue（立即返回 202）

后台 dispatcher 协程：
    → 等待空闲槽位（MAX_CONCURRENT_REQUESTS）
    → 从队列取出任务，启动 run_pipeline()

run_pipeline()：
    1. [scraping]   Jina Reader / Firecrawl 抓主页 + 子页面
                    （若 gbp_url 含 data_id，与主页抓取并发执行）
    2. [scraping]   提取商家信息 → SerpAPI 查 GBP + 评论
    3. [analyzing]  调用 Dify Workflow（SSE 进度 30%→90%）
    4. [done]       写回 task_store，等待客户端查询

GET /api/v1/task/{task_id}           ← 轮询状态
GET /api/v1/task/{task_id}/stream    ← SSE 实时推送（推荐）
```

## 快速开始

### 1. 环境准备

```bash
cp .env.example .env
# 填写 .env 中的各项 API Key（见下方配置说明）
```

### 2. 本地开发（不用 Docker）

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 FastAPI（单进程）
uvicorn app.main:app --reload --port 8000
```

### 3. Docker 部署

```bash
docker-compose up --build -d
```

启动后：
- API 服务：http://localhost:8000
- Swagger 文档（DEBUG=true 时）：http://localhost:8000/docs

## 环境变量说明

复制 `.env.example` 并按下表填写：

| 变量 | 必填 | 说明 |
|------|------|------|
| `DIFY_API_KEY` | ✅ | Dify 控制台 → 应用 → API → 生成 |
| `DIFY_API_URL` | ✅ | Dify API 地址，默认 `https://api.dify.ai/v1` |
| `DIFY_WORKFLOW_ID` | ✅ | Dify 工作流 ID |
| `SERPAPI_KEY` | ✅ | [serpapi.com](https://serpapi.com/manage-api-key) 获取，用于 GBP 数据和评论 |
| `FIRECRAWL_API_KEY` | 推荐 | [firecrawl.dev](https://www.firecrawl.dev/app/api-keys)，JS 渲染抓取（降级用）|
| `JINA_API_KEY` | 可选 | 留空使用免费版 Jina Reader（主抓取器）|
| `MAX_CONCURRENT_REQUESTS` | 可选 | 最大同时运行任务数，默认 10，超出自动排队 |
| `DIFY_RETRY` | 可选 | Dify 失败重试次数，默认 2 |
| `SCRAPER_RETRY` | 可选 | 抓取失败重试次数，默认 2 |
| `DEBUG` | 可选 | `true` 时开启 Swagger 文档和 DEBUG 日志 |
| `CORS_ORIGINS` | 可选 | 允许的前端域名，JSON 数组格式 |

## API 接口

### 提交分析任务

```
POST /api/v1/analyze
```

```json
{
  "url": "https://example.com/",
  "page_type": "本地服务落地页",
  "language": "English",
  "gbp_url": "https://www.google.com/maps/search/?api=1&query_place_id=0x..."
}
```

`gbp_url` 支持含 `query_place_id` 的 Google Maps 链接，系统自动提取 data_id 直接查询 GBP，精度最高且与主页抓取并发执行。

`page_type` 支持 21 种类型，包括：实体目的地、场馆页、活动日历、菜单、商品、本地服务落地页、关于我们、联系我们、博客、文章、FAQ 等。

`language` 支持：`中文` / `English` / `Both`

### 查询任务状态（轮询）

```
GET /api/v1/task/{task_id}
```

返回任务状态（`queued` → `scraping` → `analyzing` → `done` / `failed`）和进度百分比。

### 实时进度推送（SSE，推荐）

```
GET /api/v1/task/{task_id}/stream
```

建立 Server-Sent Events 连接，实时接收任务状态更新，无需轮询。

### 删除 / 取消任务

```
DELETE /api/v1/task/{task_id}
```

取消正在运行或排队中的任务，并从内存中删除其状态。

### 健康检查

```
GET /api/v1/health
```

## 抓取策略

页面内容抓取按以下顺序降级：

1. **Jina Reader**（首选）— 返回干净 Markdown，免费，速度快
2. **Firecrawl**（降级）— 支持 JS 渲染、滚动加载、动态内容

同时会并发抓取 `contact` / `about` 子页面并拼入正文。

## GBP 数据获取优先级

1. `gbp_url` 含 `data_id`（如 `query_place_id=0x...`）→ 直接查 place details，**与主页抓取并发执行**（最精准、最快）
2. 页面域名 → Google Maps 搜索 + 域名匹配
3. 商家名称 + 城市 → Google Maps 搜索 + 城市匹配

## 并发与排队说明

- `MAX_CONCURRENT_REQUESTS` 控制最大同时运行任务数（默认 10）
- 超过上限时任务**自动进入内部队列排队等待**，不返回 429 报错
- 槽位空闲后 dispatcher 自动取出下一个任务开始执行，用户无感知
- 任务状态存储在进程内存，**不支持多实例横向扩容**
- 部署时请确保使用 `--workers 1`

> **如需水平扩容**，需引入 Redis 替换 `task_store.py` 中的内存存储，并用 Redis Pub/Sub 替换内部队列。

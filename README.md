# SEO Trust Path Analysis Service

异步 SEO 页面诊断后端服务。输入一个页面 URL，系统自动抓取页面内容、拉取 Google Business Profile 数据，交由 Dify AI 工作流生成 SEO 诊断报告。

## 技术栈

| 层次 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 异步任务队列 | Celery 5 + Redis |
| 网页抓取 | Firecrawl → httpx → Jina Reader（三级降级）|
| AI 分析 | Dify Workflow（SSE 流式）|
| 商家数据 | SerpAPI → Google Business Profile + Reviews |
| 缓存 / 限流 | Redis（Upstash）|
| 容器化 | Docker + docker-compose |

## 项目结构

```
app/
├── main.py                  # FastAPI 入口，CORS / 限流 / Redis 生命周期
├── api/v1/analyze.py        # HTTP 接口层
├── models/
│   ├── request.py           # AnalyzeRequest（URL / 页面类型 / 语言 / GBP URL）
│   └── response.py          # 任务状态 / 进度 / 响应模型
├── core/
│   ├── config.py            # 所有配置项（从 .env 读取）
│   ├── redis_client.py      # Redis 连接池封装
│   ├── rate_limiter.py      # Dify 全局 RPM 限流（Token Bucket）
│   └── ip_rate_limiter.py   # 每 IP 请求限流中间件
└── tasks/
    ├── celery_app.py        # Celery 配置
    ├── pipeline.py          # 核心任务编排
    ├── scraper.py           # 数据采集层（抓取 / GBP / 评论）
    └── dify_client.py       # Dify SSE 调用
```

## 请求流程

```
POST /api/v1/analyze
    │
    ├─ 命中报告缓存 → 直接返回
    └─ 未命中 → 写 Redis 初始状态 → 投递 Celery 任务 → 返回 task_id

GET /api/v1/task/{task_id}   ← 前端轮询

Celery Worker：
    1. [scraping]   Firecrawl / httpx / Jina 三级抓主页 + 子页面
    2. [scraping]   提取商家信息 → SerpAPI 查 GBP + 评论
    3. [analyzing]  调用 Dify Workflow（SSE 进度 30%→90%）
    4. [done]       写报告缓存 → 更新任务状态
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

# 启动 FastAPI
uvicorn app.main:app --reload --port 8000

# 另开终端启动 Celery Worker
celery -A app.tasks.celery_app worker --loglevel=info --queues=seo_analysis
```

### 3. Docker 部署

```bash
docker-compose up --build -d
```

启动后：
- API 服务：http://localhost:8000
- Swagger 文档（DEBUG=true 时）：http://localhost:8000/docs
- Flower 监控面板：http://localhost:5555

## 环境变量说明

复制 `.env` 并按下表填写：

| 变量 | 必填 | 说明 |
|------|------|------|
| `REDIS_URL` | ✅ | Redis 连接串，支持 `redis://` 和 `rediss://`（TLS）|
| `DIFY_API_KEY` | ✅ | Dify 控制台 → 应用 → API → 生成 |
| `DIFY_API_URL` | ✅ | Dify API 地址，默认 `https://api.dify.ai/v1` |
| `DIFY_WORKFLOW_ID` | ✅ | Dify 工作流 ID |
| `SERPAPI_KEY` | ✅ | [serpapi.com](https://serpapi.com/manage-api-key) 获取，用于 GBP 数据和评论 |
| `FIRECRAWL_API_KEY` | 推荐 | [firecrawl.dev](https://www.firecrawl.dev/app/api-keys)，JS 渲染抓取 |
| `JINA_API_KEY` | 可选 | 留空使用免费版 Jina Reader |
| `DEBUG` | 可选 | `true` 时开启 Swagger 文档和 DEBUG 日志 |
| `CORS_ORIGINS` | 可选 | 允许的前端域名，JSON 数组格式 |
| `FLOWER_USER` / `FLOWER_PASSWORD` | 可选 | Flower 面板登录账号 |

### 缓存 TTL

| 变量 | 默认 | 说明 |
|------|------|------|
| `SCRAPER_CACHE_TTL` | 600s | 页面抓取结果缓存时长 |
| `REPORT_CACHE_TTL` | 600s | SEO 报告缓存时长 |
| `TASK_RESULT_TTL` | 1800s | 任务状态保留时长 |
| `TASK_DONE_TTL` | 3600s | 已完成任务状态保留时长 |

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
  "gbp_url": "https://www.google.com/maps/place/..."  // 可选，提高 GBP 匹配精度
}
```

`page_type` 支持 21 种类型，包括：实体目的地、场馆页、活动日历、菜单、商品、本地服务落地页、关于我们、联系我们、博客、文章、FAQ 等。

`language` 支持：`中文` / `English` / `Both`

### 查询任务状态

```
GET /api/v1/task/{task_id}
```

返回任务状态（`queued` → `scraping` → `analyzing` → `done` / `failed`）和进度百分比。

### 删除任务

```
DELETE /api/v1/task/{task_id}
```

### 健康检查

```
GET /api/v1/health
```

## 抓取策略

页面内容抓取按以下顺序降级：

1. **Firecrawl**（首选）— 支持 JS 渲染、滚动加载、动态内容
2. **httpx direct**（次选）— 直接 GET，适合静态页面，无外部依赖
3. **Jina Reader**（兜底）— 返回干净 Markdown，需要开放网络

同时会并发抓取 `contact` / `about` 子页面并拼入正文。

## GBP 数据获取优先级

1. `gbp_url` 含 `data_id` → 直接查 place details（最精准）
2. 页面域名 → Google Maps 搜索 + 域名匹配
3. 商家名称 + 城市 → Google Maps 搜索 + 城市匹配

## 查看爬取内容（调试）

爬取结果缓存在 Redis，key 格式：`scraper:cache:<md5(url)>`

```python
import hashlib
url = "https://example.com/"
key = "scraper:cache:" + hashlib.md5(url.encode()).hexdigest()
# redis-cli GET <key>  → JSON，content 字段为完整抓取文本
```

注意：`SCRAPER_CACHE_TTL` 默认 600 秒，需在过期前查询。

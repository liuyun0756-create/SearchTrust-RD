# 部署与迁移说明

> 本文档适用于将项目从任意平台迁移到新服务器/平台。
> 无论目标平台是 Railway、Render、Fly.io、阿里云还是自建服务器，流程均相同。

---

## 一、项目架构说明

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   FastAPI Web   │────▶│  Upstash Redis  │◀────│  Celery Worker  │
│  (HTTP 入口)    │     │  (队列 + 缓存)   │     │  (任务执行)      │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │                                                 │
        ▼                                                 ▼
   接收请求                                        调用外部服务
   返回 task_id                               Dify / SerpAPI / Jina
   查询任务状态                                    Firecrawl
```

| 组件 | 说明 | 是否需要迁移 |
|------|------|------------|
| FastAPI Web | HTTP API 服务 | ✅ 需要部署 |
| Celery Worker | 异步任务处理 | ✅ 需要部署 |
| Redis | 队列 + 缓存 | ❌ 用 Upstash，无需迁移 |
| Dify / SerpAPI 等 | 外部 API | ❌ 云端服务，无需迁移 |

> **迁移本质上只需要：在新平台重新运行 Web 和 Worker 两个进程，填入同一份环境变量即可。**

---

## 二、迁移前准备

### 1. 确认环境变量清单

迁移时需要填入以下所有变量（从现有平台的环境变量面板复制）：

```env
# 应用基础
APP_NAME=SEO Trust Path Analysis Service
APP_VERSION=1.0.0
DEBUG=false

# Redis（Upstash，迁移时不变）
REDIS_URL=rediss://default:密码@host:6379/0

# Dify
DIFY_API_KEY=app-xxx
DIFY_API_URL=https://api.dify.ai/v1
DIFY_WORKFLOW_ID=xxx
DIFY_STREAM_TIMEOUT=120
DIFY_RETRY=3

# SerpAPI
SERPAPI_KEY=xxx

# 并发与限流
MAX_CONCURRENT_REQUESTS=10
RATE_LIMIT_PER_MINUTE=10

# 抓取器
SCRAPER_TIMEOUT=30
SCRAPER_RETRY=3
SCRAPER_MIN_CONTENT_LENGTH=300
JINA_API_KEY=
FIRECRAWL_API_KEY=fc-xxx
FIRECRAWL_API_URL=https://api.firecrawl.dev/v1

# Dify 限流
DIFY_RPM_CAPACITY=60
DIFY_RPM_REFILL=60
DIFY_RPM_INTERVAL=60

# 缓存 TTL
TASK_RESULT_TTL=86400
SCRAPER_CACHE_TTL=3600
REPORT_CACHE_TTL=86400

# CORS
CORS_ORIGINS=["*"]
```

### 2. 确认代码已推送到 GitHub

```bash
git status          # 确认没有未提交的修改
git log --oneline   # 确认最新代码已提交
git push            # 推送到 GitHub
```

---

## 三、各平台迁移步骤

---

### 方案 A：Railway（推荐）

**前提：** 需要 Visa/Mastercard 信用卡 或 WildCard 虚拟卡

#### 启动命令

**Web Service（使用 `railway.toml`）：**
```
/bin/sh -c "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 2 --loop uvloop --http httptools"
```

**Worker Service（使用 `railway.worker.toml`）：**
```
celery -A app.tasks.celery_app worker --loglevel=info --pool=solo --concurrency=1 --queues=seo_analysis --hostname=worker@%h --max-tasks-per-child=100
```

#### 部署步骤

1. 打开 [railway.com](https://railway.com) → GitHub 登录
2. **New Project** → **Deploy from GitHub repo** → 选择仓库
3. 授权仓库：点 **Configure GitHub App** → 勾选仓库 → Save → Refresh
4. Web Service 部署完成后：
   - 进入 **设置 → Networking** → Generate Domain → 填写日志中实际端口（查日志找 `Uvicorn running on http://0.0.0.0:XXXX`）
   - 进入 **变量 → RAW Editor** → 粘贴环境变量 → Deploy
5. 添加 Worker Service：
   - 项目主页点 **+ New** → **GitHub Repo** → 同一仓库
   - 进入新 Service → **设置 → Config-as-code → Add File Path** → 填 `railway.worker.toml`
   - **变量 → RAW Editor** → 粘贴同样的环境变量 → Deploy
6. 验证：两个 Service 均显示 **Online** ✅

#### 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 健康检查失败 | 域名端口填错 | 查日志找实际端口重新生成域名 |
| `$PORT` 不是整数 | shell 变量未展开 | 启动命令用 `/bin/sh -c "..."` 包裹 |
| Worker 不处理任务 | 环境变量未填 | 检查 Worker Service 变量是否完整 |
| 找不到仓库 | GitHub 未授权 | 重新 Configure GitHub App |

---

### 方案 B：阿里云香港 ECS（支持支付宝）

**适用场景：** 需要开发票报销、或无法使用境外信用卡

**推荐配置：** 2核 4GB，香港节点

#### 部署步骤

**1. 服务器初始化**

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 安装 Docker Compose
sudo apt install docker-compose-plugin -y

# 重新登录使权限生效
```

**2. 拉取代码**

```bash
git clone https://github.com/你的用户名/seo-backend.git
cd seo-backend
```

**3. 配置环境变量**

```bash
cp .env.example .env
vim .env    # 填入所有真实配置值
```

**4. 启动服务**

```bash
docker compose up -d app celery_worker
```

**5. 验证**

```bash
# 查看运行状态
docker compose ps

# 查看日志
docker compose logs -f app
docker compose logs -f celery_worker

# 测试健康检查
curl http://localhost:8000/api/v1/health
```

**6. 配置反向代理（Nginx + HTTPS）**

```bash
sudo apt install nginx certbot python3-certbot-nginx -y

# 配置域名解析后申请证书
sudo certbot --nginx -d 你的域名.com
```

---

### 方案 C：Render

**适用场景：** 有免费额度，操作简单

#### 部署步骤

1. 打开 [render.com](https://render.com) → GitHub 登录
2. **New → Web Service** → 连接 GitHub 仓库
3. 配置：
   - **Environment:** Docker
   - **Start Command:** 留空（使用 Dockerfile 默认）
   - 填入所有环境变量
4. 添加 Worker：**New → Background Worker** → 同一仓库
   - **Start Command:**
     ```
     celery -A app.tasks.celery_app worker --loglevel=info --pool=solo --concurrency=1 --queues=seo_analysis --hostname=worker@%h
     ```
   - 填入同样的环境变量
5. 验证健康检查接口

---

### 方案 D：Fly.io

**适用场景：** Docker 原生支持，按量付费，性价比高

#### 部署步骤

**1. 安装 flyctl**

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
```

**2. 初始化 Web App**

```bash
fly launch --name seo-backend-web --region lax
# 选择：不使用 Postgres，不使用 Redis（用 Upstash）
```

**3. 设置环境变量**

```bash
fly secrets set REDIS_URL="rediss://..." DIFY_API_KEY="app-xxx" ...
# 逐个设置，或使用 .env 文件批量导入：
cat .env | fly secrets import
```

**4. 部署 Web**

```bash
fly deploy
```

**5. 部署 Worker（新建独立 App）**

```bash
fly launch --name seo-backend-worker --region lax

# 修改 fly.toml，设置启动命令：
# [processes]
#   worker = "celery -A app.tasks.celery_app worker --loglevel=info --pool=solo --concurrency=1 --queues=seo_analysis"

fly secrets set REDIS_URL="rediss://..." ...   # 同样的变量
fly deploy
```

---

## 四、迁移验证清单

部署完成后，逐项确认：

```
□ 健康检查接口返回 {"status":"ok","redis":true}
□ POST /api/v1/analyze 能正常返回 task_id
□ GET /api/v1/task/{task_id} 状态从 queued → scraping → analyzing → done
□ Worker 日志中出现 "Celery worker is ready"
□ Worker 日志中出现任务开始执行的记录
□ Redis 连接正常（健康检查 redis: true）
```

---

## 五、Worker 并发扩容说明

项目使用 `--pool=solo` 模式，**每个 Worker 进程同时只处理 1 个任务**。

> ⚠️ 不要改成 `prefork` 或 `gevent`，与项目的 `asyncio` 任务不兼容。

### 提高并发的正确方式：增加 Worker 进程数

| 平台 | 方法 | 同时处理任务数 |
|------|------|-------------|
| Railway Hobby | 只能 1 个副本 | 1 个 |
| Railway Pro | 增加 Worker Service 副本数 | 副本数 × 1 |
| docker-compose | `--scale celery_worker=N` | N 个 |
| 云服务器 | 启动多个 Worker 进程 | N 个 |

**docker-compose 扩容示例：**
```bash
docker compose up -d --scale celery_worker=4
```

**估算所需 Worker 数：**
```
每个任务平均耗时：约 90 秒
目标日活：X 次
所需 Worker 数 = (X ÷ 86400 × 90) × 高峰系数(3)
```

| 目标日活 | 建议 Worker 数 |
|---------|--------------|
| 1,000   | 1 个          |
| 5,000   | 2 个          |
| 10,000  | 4 个          |
| 50,000  | 10 个         |
| 100,000 | 20 个         |

---

## 六、数据迁移说明

| 数据类型 | 存储位置 | 迁移时需要操作 |
|---------|---------|-------------|
| 任务状态 | Upstash Redis | ❌ 无需操作，自动继承 |
| 分析报告缓存 | Upstash Redis | ❌ 无需操作，自动继承 |
| 环境变量/密钥 | 各平台配置 | ✅ 手动填入新平台 |
| 代码 | GitHub | ✅ 新平台连接同一仓库 |

> **核心优势：** 所有业务数据都在 Upstash，平台只跑计算，迁移零数据损失。

---

## 七、回滚说明

如果新平台出现问题，立即回滚：

1. 旧平台重新启动服务（环境变量不变）
2. 修改前端 API 地址指向旧平台域名
3. 新平台可继续调试，不影响线上

> 因为 Redis 是共享的，新旧平台可以同时运行，不会产生数据冲突。

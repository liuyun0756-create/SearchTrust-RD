# 部署与迁移说明

> 本文档适用于将项目从任意平台迁移到新服务器/平台。
> 无论目标平台是 Railway、Render、Fly.io、阿里云还是自建服务器，流程均相同。

---

## 一、项目架构说明

```
┌──────────────────────────────────────────┐
│            FastAPI Web Service            │
│                                           │
│  HTTP 入口 + asyncio 后台任务             │
│  任务状态：进程内内存（task_store）        │
│  并发控制：MAX_CONCURRENT_REQUESTS        │
└──────────────────────────────────────────┘
        │
        ▼
   调用外部服务
Dify / SerpAPI / Jina / Firecrawl
```

| 组件 | 说明 | 是否需要部署 |
|------|------|------------|
| FastAPI Web | HTTP API + 后台任务（单进程） | ✅ 需要部署 |
| Dify / SerpAPI 等 | 外部 API | ❌ 云端服务，无需部署 |

> **架构说明：** 本项目使用 asyncio 进程内任务队列，**不依赖 Redis 或 Celery**。
> 所有任务状态存储在进程内存中，服务重启后任务状态清空（正常行为）。
> 如需持久化或多实例部署，需引入 Redis 扩展。

---

## 二、迁移前准备

### 1. 确认环境变量清单

迁移时需要填入以下所有变量（从现有平台的环境变量面板复制）：

```env
# 应用基础
APP_NAME=SEO Trust Path Analysis Service
APP_VERSION=1.0.0
DEBUG=false

# Dify
DIFY_API_KEY=app-xxx
DIFY_API_URL=https://api.dify.ai/v1
DIFY_WORKFLOW_ID=xxx
DIFY_STREAM_TIMEOUT=120
DIFY_RETRY=3

# SerpAPI
SERPAPI_KEY=xxx

# 并发控制
MAX_CONCURRENT_REQUESTS=10

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

## 三、各平台部署步骤

---

### 方案 A：Railway（推荐）

**前提：** 需要 Visa/Mastercard 信用卡 或 WildCard 虚拟卡

#### 启动命令

```
/bin/sh -c "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"
```

#### 部署步骤

1. 打开 [railway.com](https://railway.com) → GitHub 登录
2. **New Project** → **Deploy from GitHub repo** → 选择仓库
3. 授权仓库：点 **Configure GitHub App** → 勾选仓库 → Save → Refresh
4. 部署完成后：
   - 进入 **设置 → Networking** → Generate Domain → 填写日志中实际端口（查日志找 `Uvicorn running on http://0.0.0.0:XXXX`）
   - 进入 **变量 → RAW Editor** → 粘贴环境变量 → Deploy
5. 验证：Service 显示 **Online** ✅

> **注意：** 当前架构只需要部署一个 Web Service，无需额外的 Worker Service。

#### 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 健康检查失败 | 域名端口填错 | 查日志找实际端口重新生成域名 |
| `$PORT` 不是整数 | shell 变量未展开 | 启动命令用 `/bin/sh -c "..."` 包裹 |
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
docker compose up -d app
```

**5. 验证**

```bash
# 查看运行状态
docker compose ps

# 查看日志
docker compose logs -f app

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
4. 验证健康检查接口

---

### 方案 D：Fly.io

**适用场景：** Docker 原生支持，按量付费，性价比高

#### 部署步骤

**1. 安装 flyctl**

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
```

**2. 初始化 App**

```bash
fly launch --name seo-backend --region lax
# 选择：不使用 Postgres，不使用 Redis
```

**3. 设置环境变量**

```bash
# 批量导入 .env 文件
cat .env | fly secrets import
```

**4. 部署**

```bash
fly deploy
```

---

## 四、验证清单

部署完成后，逐项确认：

```
□ 健康检查接口返回 {"status":"ok","version":"1.0.0"}
□ POST /api/v1/analyze 能正常返回 task_id
□ GET /api/v1/task/{task_id} 状态从 queued → scraping → analyzing → done
□ GET /api/v1/task/{task_id}/stream SSE 连接正常推送进度
□ 服务日志无异常报错
```

---

## 五、并发扩容说明

当前架构通过 `MAX_CONCURRENT_REQUESTS` 控制并发任务数：

| 配置值 | 说明 | 适用场景 |
|--------|------|---------|
| `MAX_CONCURRENT_REQUESTS=5` | 最多同时 5 个分析任务 | 轻量使用 |
| `MAX_CONCURRENT_REQUESTS=10` | 默认值 | 一般生产 |
| `MAX_CONCURRENT_REQUESTS=20` | 最多同时 20 个 | 高并发（需确认 Dify/SerpAPI 配额） |

**重要：** 当前架构使用单进程，**不支持通过增加实例数来扩容**。
若多实例部署，不同实例的任务状态无法共享，会导致状态查询 404。

> **如需水平扩容**，需引入 Redis 替换 `task_store.py` 中的内存存储，
> 并用 Redis Pub/Sub 替换 asyncio.Queue 来支持跨实例 SSE 推送。

**估算最大并发量：**

```
每个任务平均耗时：约 90 秒
每分钟可处理任务数 = MAX_CONCURRENT_REQUESTS × (60 ÷ 90) ≈ MAX_CONCURRENT_REQUESTS × 0.67
```

| MAX_CONCURRENT_REQUESTS | 约每分钟处理任务数 |
|------------------------|-----------------|
| 5 | ~3 个 |
| 10 | ~7 个 |
| 20 | ~13 个 |

---

## 六、迁移说明

由于任务状态存储在进程内存中，迁移时：

| 数据类型 | 存储位置 | 迁移时需要操作 |
|---------|---------|-------------|
| 进行中的任务状态 | 进程内存 | ❌ 不可迁移，重启后清空 |
| 环境变量/密钥 | 各平台配置 | ✅ 手动填入新平台 |
| 代码 | GitHub | ✅ 新平台连接同一仓库 |

> **建议迁移策略：** 在非高峰期切换。新平台启动并验证健康检查后，修改前端 API 地址指向新平台。

---

## 七、回滚说明

如果新平台出现问题，立即回滚：

1. 旧平台重新启动服务（环境变量不变）
2. 修改前端 API 地址指向旧平台域名

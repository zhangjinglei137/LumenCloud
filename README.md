<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-green" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3.5-brightgreen" alt="Vue">
  <img src="https://img.shields.io/badge/PostgreSQL-16-blue" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED" alt="Docker">
</p>

# 拾光云映 LumenCloud

智能影视管理系统 — 让补全媒体库像拾起一缕光一样简单。

从 TMDB 搜索订阅 → Emby 缺集比对 → CloudSaver 网盘搜源 → Aria2 高速下载 → NasTools 入库整理，全流程自动化。

## 架构

```
┌─────────────────────────────┐
│    LumenCloud 单容器         │
│  ┌───────────┐ ┌──────────┐ │
│  │ FastAPI   │ │ ARQ      │ │
│  │ (REST API)│ │ Worker   │ │
│  └─────┬─────┘ └──────────┘ │
│        │                    │
│  ┌─────▼─────┐              │
│  │ Vue 3 SPA │              │
│  │ (静态文件)  │              │
│  └───────────┘              │
└────────┬────────────────────┘
         │
    ┌────┼────┬──────────────────┐
    ▼    ▼    ▼                  ▼
┌──────┐┌──────┐┌──────────┐ ┌──────────┐
│ TMDB ││ Emby││CloudSaver│ │PostgreSQL│
│(搜索) ││(比对)││(搜源)    │ │+ Redis   │
└──────┘└──────┘└──────────┘ │(外部服务) │
         │                   └──────────┘
         ▼
   ┌──────────┐    ┌──────────┐
   │  Aria2   │◀───│  AList   │
   │ (下载器)  │    │ (直链)    │
   └────┬─────┘    └────┬─────┘
        │               │
        ▼               ▼
   ┌──────────┐   ┌──────────┐
   │ NasTools │   │  Quark   │
   │(刮削整理) │   │(夸克网盘) │
   └────┬─────┘   └──────────┘
        ▼
   ┌──────────┐
   │  Emby    │
   │ (媒体库)  │
   └──────────┘
```

## 功能

### 用户端
- **影视广场** — TMDB 搜索、浏览影视，查看观看次数和订阅热度
- **一键订阅** — 追新剧、标记想看，上映后自动通知
- **投票协作** — 给想看的影视 +1，热度越高的越容易被管理员审批下载
- **个人片单** — 管理订阅、打分（1-10）、标记"不想看"
- **剧集追踪** — 按季/集查看 Emby 入库状态，缺了哪集一目了然

### 管理端
- **订阅审批** — 查看所有订阅和票数，一键通过开始追踪
- **影视管理** — 增删影视，设置扫描频率（热播剧每小时扫、老片每天扫）
- **任务监控** — 实时查看下载队列、进度、状态
- **通知中心** — 上映提醒、下载完成通知

### 自动化流水线
- **每天定时扫描** — 自动比对 Emby 缺集 → CloudSaver 搜夸克资源 → Aria2 下载 → NasTools 刮削入库
- **每周清理网盘** — 已入库文件自动从夸克网盘删除，释放空间
- **PushPlus 通知** — 下载完成微信推送

## 快速开始

### 前置条件

- Docker & Docker Compose
- 外部 PostgreSQL 16 和 Redis 7（可复用已有实例）
- 已有服务：Emby、CloudSaver、Aria2、NasTools、AList（在同一内网）
- TMDB API Key（https://www.themoviedb.org/settings/api）

### 1. 克隆项目

```bash
git clone https://github.com/thntime/LumenCloud.git
cd LumenCloud
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的密钥和外部服务地址：

```env
DATABASE_URL=postgresql+asyncpg://lumen:your_password@192.168.3.31:5432/lumencloud
REDIS_URL=redis://192.168.3.31:6379/0
TMDB_API_KEY=your_tmdb_api_key
EMBY_API_KEY=your_emby_api_key
CLOUDSAVER_PASSWORD=your_password
ARIA2_SECRET=your_aria2_token
NASTOOLS_PASSWORD=your_password
ALIST_TOKEN=your_alist_token
PUSHPLUS_TOKEN=your_pushplus_token
JWT_SECRET=openssl_rand_hex_32
```

### 3. 启动

```bash
docker compose up -d
```

访问：http://localhost:8000

> 单容器同时提供前端页面和后端 API。前端 SPA 由 FastAPI 直出，无需额外 Nginx 容器。

## 本地开发

前后端分离运行，支持热更新：

```bash
# 1. 安装依赖
cd backend && pip install -r requirements.txt
cd ../frontend && npm install

# 2. 复制并编辑配置
cp .env.example .env
# 编辑 .env，填入 DATABASE_URL / REDIS_URL / JWT_SECRET
# 其他服务配置（TMDB、Emby等）在系统启动后通过管理后台设置

# 3. 启动后端 (终端1)
cd backend
uvicorn app.main:app --reload --port 8000

# 4. 启动前端 (终端2)
cd frontend
npm run dev
# → http://localhost:5173 (Vite 自动代理 /api 到 localhost:8000)

# 5. 启动 Worker (终端3，需要测试下载流水线时)
cd backend
arq app.tasks.worker.WorkerSettings
```

### 4. 首次设置管理员

```bash
# 先用你的 Emby 账号登录一次 LumenCloud，然后执行：
psql -h 192.168.3.31 -U lumen -d lumencloud -c "UPDATE users SET is_admin = true WHERE username = '你的Emby用户名';"
```

## 生产部署

### 通过 Docker Hub 镜像部署

GitHub Actions 会在每次推送 `v*.*.*` tag 时自动构建并推送镜像到 Docker Hub。

```bash
# 1. 配置 GitHub Secrets
#    Settings → Secrets → Actions → 添加:
#    DOCKERHUB_USERNAME = thntime
#    DOCKERHUB_TOKEN = (Docker Hub Access Token)

# 2. 打 tag 触发构建
git tag v1.0.0 && git push origin v1.0.0

# 3. 在服务器上部署
cp .env.example .env.prod  # 填入生产环境配置
IMAGE_TAG=1.0.0 docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

## 技术栈

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI (Python 3.12) |
| 异步任务 | ARQ + Redis |
| 数据库 | PostgreSQL 16 + SQLAlchemy 2.0 (async) |
| 前端 | Vue 3 + Element Plus + Pinia（FastAPI 静态直出） |
| 认证 | Emby /Users/AuthenticateByName → JWT |
| 部署 | Docker Compose (dev) / Docker Hub 镜像 (prod) |
| CI/CD | GitHub Actions → Docker Hub |

## 外部服务依赖

| 服务 | 用途 | 默认地址 |
|------|------|----------|
| PostgreSQL | 主数据库 | 外部（默认 192.168.3.31:5432） |
| Redis | 任务队列、缓存 | 外部（默认 192.168.3.31:6379） |
| Emby | 媒体库、用户认证、播放统计 | 192.168.3.31:8096 |
| CloudSaver | 网盘资源搜索、夸克转存 | 192.168.3.31:8008 |
| Aria2 | 下载引擎 | 192.168.3.31:6800 |
| NasTools | 媒体刮削、目录同步 | 192.168.3.31:3000 |
| AList | 网盘文件管理、直链生成 | 192.168.3.31:5244 |
| TMDB | 影视元数据 | api.themoviedb.org |
| PushPlus | 微信通知推送 | pushplus.plus |

## 项目结构

```
lumen-cloud/
├── Dockerfile              # 多阶段构建（前端 → Python 运行时）
├── supervisord.conf        # 进程管理（api + worker）
├── docker-compose.yml      # 开发环境（单服务）
├── docker-compose.prod.yml # 生产环境（Docker Hub 镜像）
├── .github/workflows/      # CI/CD
├── backend/
│   ├── app/
│   │   ├── api/            # REST API 路由
│   │   ├── models/         # SQLAlchemy 数据模型
│   │   ├── schemas/        # Pydantic 请求/响应模型
│   │   ├── services/       # 外部服务客户端
│   │   └── tasks/          # ARQ 异步任务
│   └── alembic/            # 数据库迁移
└── frontend/
    └── src/
        ├── views/          # 页面组件
        ├── components/     # 可复用组件
        ├── stores/         # Pinia 状态管理
        ├── api/            # Axios API 调用
        └── router/         # Vue Router 路由
```

## License

MIT

# 拾光云映 (LumenCloud) Implementation Plan

> **For agentic workers:** Use subagent-driven-development to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-stack media management system that replaces N8n workflows for automated media download pipeline (TMDB search → Emby gap detection → CloudSaver resource search → Aria2 download → NasTools organization), with multi-user collaboration features including subscriptions, voting, ratings, and watch history.

**Architecture:** FastAPI backend with ARQ task queue for async download pipelines, Vue 3 + Element Plus frontend, PostgreSQL for persistence, Redis for caching/task broker. Users authenticate via Emby credentials; LumenCloud acts as an Emby client, caching user access tokens. Docker Compose deployment.

**Tech Stack:** Python 3.12+ / FastAPI / SQLAlchemy 2.0 (async) / ARQ / PostgreSQL 16 / Redis 7 / Vue 3 / Element Plus / Vite / Docker Compose

## Global Constraints

- Python >= 3.12, Node >= 20
- All backend code uses async/await (FastAPI + asyncpg + ARQ)
- Emby-based authentication (no separate user registration)
- Admin users designated via DB manual update of `is_admin` flag
- CloudSaver: only Quark (夸克) cloud supported
- External services on internal network: `192.168.3.31`
- External services on public network: `thntime.fun`
- Docker Compose only manages LumenCloud services (pg, redis, backend, worker, frontend)
- Weekly cleanup of Quark cloud files via AList API
- Hard delete for media (cascades to all related user data)
- System notification center (no external push except PusPlus for download complete)
- N8n workflows fully replaced — N8n to be decommissioned after migration

---

## File Structure Map

```
lumen-cloud/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   └── app/
│       ├── main.py                    # FastAPI app entry, lifespan, CORS
│       ├── config.py                  # Settings from .env via pydantic-settings
│       ├── database.py                # Async engine, session factory, Base
│       ├── models/
│       │   ├── __init__.py
│       │   ├── user.py                # User, UserToken
│       │   ├── media.py               # Media, Season, Episode
│       │   └── interaction.py         # Subscription, Vote, Rating, Notification, DownloadTask, CleanupRecord
│       ├── schemas/
│       │   ├── __init__.py
│       │   ├── auth.py                # LoginRequest, TokenResponse
│       │   ├── media.py               # MediaSearch, MediaDetail, MediaList
│       │   ├── user.py                # UserProfile, UserInteraction
│       │   └── task.py                # DownloadTaskStatus, NotificationItem
│       ├── api/
│       │   ├── __init__.py
│       │   ├── deps.py                # get_current_user, get_admin_user, get_db
│       │   ├── auth.py                # POST /api/auth/login, /api/auth/me
│       │   ├── media.py               # GET /api/media/search, /api/media/{id}, /api/media/{id}/seasons
│       │   ├── subscription.py        # POST/GET/DELETE /api/subscriptions, POST /api/subscriptions/{id}/vote
│       │   ├── interaction.py         # POST/PUT /api/interactions/rating, /api/interactions/skip
│       │   ├── admin.py               # GET /api/admin/subscriptions, POST /api/admin/approve, DELETE /api/admin/media/{id}
│       │   ├── task.py                # GET /api/tasks, GET /api/tasks/{id}
│       │   ├── notification.py        # GET /api/notifications, PUT /api/notifications/{id}/read
│       │   └── webhook.py             # POST /api/webhook/aria2
│       ├── services/
│       │   ├── __init__.py
│       │   ├── tmdb.py                # TMDB API client (search, detail, season/episodes)
│       │   ├── emby.py                # Emby API client (auth, items, missing, playcount)
│       │   ├── cloudsaver.py          # CloudSaver API client (search, quark/save, login)
│       │   ├── aria2.py               # Aria2 JSON-RPC client (addUri, tellStatus)
│       │   ├── nastools.py            # NasTools HTTP client (login, directory_sync, restart)
│       │   ├── alist.py               # AList HTTP client (list files, delete)
│       │   └── pushplus.py            # PushPlus notification sender
│       └── tasks/
│           ├── __init__.py
│           ├── worker.py              # ARQ worker entry point
│           ├── scan.py                # Scheduled full scan task
│           ├── download.py            # Single media download pipeline task
│           └── cleanup.py             # Weekly Quark cleanup task
├── frontend/
│   ├── Dockerfile
│   ├── nginx.conf
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.ts
│       ├── App.vue
│       ├── router/
│       │   └── index.ts
│       ├── stores/
│       │   ├── auth.ts                # Pinia auth store (login, token, user)
│       │   └── notification.ts        # Pinia notification store
│       ├── api/
│       │   └── index.ts               # Axios instance + all API calls
│       ├── views/
│       │   ├── LoginView.vue
│       │   ├── MediaSquareView.vue    # 影视广场 (search, browse, subscribe)
│       │   ├── MediaDetailView.vue    # 影视详情 (seasons, episodes, status)
│       │   ├── MyList.vue             # 我的片单
│       │   ├── AdminDashboard.vue     # 管理后台 (approval, media mgmt)
│       │   ├── TaskMonitor.vue        # 任务监控 (download queue)
│       │   └── NotificationCenter.vue # 通知中心
│       └── components/
│           ├── MediaCard.vue           # 影视卡片 (poster, title, year, rating, watch count)
│           ├── MediaSearch.vue         # TMDB 搜索组件
│           ├── SeasonEpisodeList.vue   # 季/集列表 + 状态展示
│           ├── RatingStars.vue         # 星级打分
│           ├── SubscribeButton.vue     # 订阅/投票按钮
│           ├── AdminSidebar.vue        # 管理员侧边栏
│           └── NotificationBell.vue   # 通知铃铛
```

---

## Phase 1: Project Scaffolding

### Task 1: Project Skeleton & Docker Compose

**Files:**
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `backend/requirements.txt`
- Create: `backend/Dockerfile`
- Create: `frontend/package.json`
- Create: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`

**Interfaces:**
- Produces: Docker services `db`, `redis`, `backend`, `worker`, `frontend` all defined with healthchecks

- [ ] **Step 1: Create .gitignore**

```gitignore
__pycache__/
*.pyc
.env
node_modules/
dist/
.venv/
*.egg-info/
```

- [ ] **Step 2: Create .env.example**

```env
# Database
POSTGRES_USER=lumen
POSTGRES_PASSWORD=change_me
POSTGRES_DB=lumencloud
DATABASE_URL=postgresql+asyncpg://lumen:change_me@db:5432/lumencloud

# Redis
REDIS_URL=redis://redis:6379/0

# JWT
JWT_SECRET=change_me_to_random_string
JWT_ALGORITHM=HS256
JWT_EXPIRE_HOURS=168

# TMDB
TMDB_API_KEY=your_tmdb_api_key

# Emby
EMBY_BASE_URL=http://192.168.3.31:8096
EMBY_API_KEY=c98decfb1c9d4d12acf49de6eafe90c0

# CloudSaver
CLOUDSAVER_BASE_URL=http://192.168.3.31:8008
CLOUDSAVER_USERNAME=admin
CLOUDSAVER_PASSWORD=54pwd@2022

# Aria2
ARIA2_RPC_URL=http://192.168.3.31:6800/jsonrpc
ARIA2_SECRET=zhangxiaolei521
ARIA2_DOWNLOAD_DIR=/downloads

# NasTools
NASTOOLS_BASE_URL=http://192.168.3.31:3000
NASTOOLS_USERNAME=admin
NASTOOLS_PASSWORD=54pwd@2022

# AList
ALIST_BASE_URL=http://192.168.3.31:5244
ALIST_TOKEN=your_alist_token

# PushPlus
PUSHPLUS_TOKEN=your_pushplus_token

# App
APP_NAME=LumenCloud
DEBUG=false
```

- [ ] **Step 3: Create docker-compose.yml**

```yaml
version: "3.8"

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-lumen}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change_me}
      POSTGRES_DB: ${POSTGRES_DB:-lumencloud}
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-lumen}"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  backend:
    build: ./backend
    env_file: .env
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  worker:
    build: ./backend
    env_file: .env
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    command: arq app.tasks.worker.WorkerSettings --watch

  frontend:
    build: ./frontend
    ports:
      - "3000:80"
    depends_on:
      - backend

volumes:
  pgdata:
```

- [ ] **Step 4: Create backend/requirements.txt**

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
sqlalchemy[asyncio]==2.0.36
asyncpg==0.30.0
alembic==1.14.1
pydantic-settings==2.7.1
python-jose[cryptography]==3.3.0
passlib[bcrypt]==1.7.4
httpx==0.28.1
arq==0.26.0
redis==5.2.1
python-multipart==0.0.20
```

- [ ] **Step 5: Create backend/Dockerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 6: Create frontend/package.json**

```json
{
  "name": "lumencloud-frontend",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "vue": "^3.5.0",
    "vue-router": "^4.5.0",
    "pinia": "^2.3.0",
    "element-plus": "^2.9.0",
    "@element-plus/icons-vue": "^2.3.1",
    "axios": "^1.7.0"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.2.0",
    "typescript": "^5.7.0",
    "vue-tsc": "^2.2.0",
    "vite": "^6.0.0",
    "unplugin-auto-import": "^0.18.0",
    "unplugin-vue-components": "^0.27.0"
  }
}
```

- [ ] **Step 7: Create frontend/vite.config.ts**

```typescript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  server: {
    proxy: { '/api': 'http://localhost:8000' }
  }
})
```

- [ ] **Step 8: Create frontend/index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>拾光云映 LumenCloud</title>
</head>
<body>
  <div id="app"></div>
  <script type="module" src="/src/main.ts"></script>
</body>
</html>
```

- [ ] **Step 9: Create frontend/Dockerfile**

```dockerfile
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
```

- [ ] **Step 10: Create frontend/nginx.conf**

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;
    location / {
        try_files $uri $uri/ /index.html;
    }
    location /api/ {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "feat: project scaffolding with Docker Compose"
```

---

### Task 2: Backend Core — Config & Database

**Files:**
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Create: `backend/app/database.py`
- Create: `backend/app/main.py`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`

**Interfaces:**
- Produces: `Settings` class, async `get_db()` session generator, FastAPI app with CORS, Alembic migration framework

- [ ] **Step 1: Create backend/app/__init__.py** (empty file)

- [ ] **Step 2: Create backend/app/config.py**

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    POSTGRES_USER: str = "lumen"
    POSTGRES_PASSWORD: str = "change_me"
    POSTGRES_DB: str = "lumencloud"
    DATABASE_URL: str = "postgresql+asyncpg://lumen:change_me@db:5432/lumencloud"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT
    JWT_SECRET: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168

    # TMDB
    TMDB_API_KEY: str = ""

    # Emby
    EMBY_BASE_URL: str = "http://192.168.3.31:8096"
    EMBY_API_KEY: str = ""

    # CloudSaver
    CLOUDSAVER_BASE_URL: str = "http://192.168.3.31:8008"
    CLOUDSAVER_USERNAME: str = "admin"
    CLOUDSAVER_PASSWORD: str = ""

    # Aria2
    ARIA2_RPC_URL: str = "http://192.168.3.31:6800/jsonrpc"
    ARIA2_SECRET: str = ""
    ARIA2_DOWNLOAD_DIR: str = "/downloads"

    # NasTools
    NASTOOLS_BASE_URL: str = "http://192.168.3.31:3000"
    NASTOOLS_USERNAME: str = "admin"
    NASTOOLS_PASSWORD: str = ""

    # AList
    ALIST_BASE_URL: str = "http://192.168.3.31:5244"
    ALIST_TOKEN: str = ""

    # PushPlus
    PUSHPLUS_TOKEN: str = ""

    # App
    APP_NAME: str = "LumenCloud"
    DEBUG: bool = False

settings = Settings()
```

- [ ] **Step 3: Create backend/app/database.py**

```python
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 4: Create backend/app/main.py**

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine, Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 5: Create backend/alembic.ini**

```ini
[alembic]
script_location = alembic
sqlalchemy.url = postgresql+asyncpg://lumen:change_me@localhost:5432/lumencloud

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 6: Create backend/alembic/env.py**

```python
import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context
from app.database import Base
from app.models import *  # noqa: ensure all models loaded

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 7: Create backend/alembic/script.py.mako**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}

def upgrade() -> None:
    ${upgrades if upgrades else "pass"}

def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 8: Commit**

```bash
git add backend/
git commit -m "feat: backend core — config, database, FastAPI app, Alembic"
```

---

## Phase 2: Data Models

### Task 3: SQLAlchemy Models

**Files:**
- Create: `backend/app/models/__init__.py`
- Create: `backend/app/models/user.py`
- Create: `backend/app/models/media.py`
- Create: `backend/app/models/interaction.py`

**Interfaces:**
- Produces: `User`, `UserToken`, `Media`, `Season`, `Episode`, `Subscription`, `Vote`, `Rating`, `UserMediaStatus`, `Notification`, `DownloadTask`, `CleanupRecord` ORM models

- [ ] **Step 1: Create backend/app/models/user.py**

```python
import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    emby_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    tokens: Mapped[list["UserToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class UserToken(Base):
    __tablename__ = "user_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    emby_access_token: Mapped[str] = mapped_column(String(512), nullable=False, comment="Cached Emby access token")
    lumen_jwt: Mapped[str] = mapped_column(String(1024), nullable=True, comment="Current LumenCloud JWT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="tokens")
```

- [ ] **Step 2: Create backend/app/models/media.py**

```python
import uuid
from datetime import datetime, date
from sqlalchemy import String, Integer, Boolean, DateTime, Date, Text, Float, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
import enum

class MediaType(str, enum.Enum):
    MOVIE = "movie"
    TV = "tv"

class MediaStatus(str, enum.Enum):
    UPCOMING = "upcoming"       # 待上映
    TRACKING = "tracking"        # 追踪中 (已审批，等待下载)
    DOWNLOADING = "downloading"  # 下载中
    COMPLETED = "completed"      # 已入库
    PAUSED = "paused"            # 暂停追踪

class Media(Base):
    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    original_title: Mapped[str] = mapped_column(String(512), nullable=True)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False)
    overview: Mapped[str] = mapped_column(Text, nullable=True)
    poster_path: Mapped[str] = mapped_column(String(512), nullable=True)
    backdrop_path: Mapped[str] = mapped_column(String(512), nullable=True)
    release_date: Mapped[date] = mapped_column(Date, nullable=True)
    vote_average: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[MediaStatus] = mapped_column(Enum(MediaStatus), default=MediaStatus.UPCOMING)
    scan_frequency_hours: Mapped[int] = mapped_column(Integer, default=24, comment="扫描频率(小时)")
    last_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True, comment="硬删除前的时间标记")

    seasons: Mapped[list["Season"]] = relationship(back_populates="media", cascade="all, delete-orphan")

class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    season_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=True)
    episode_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    media: Mapped["Media"] = relationship(back_populates="seasons")
    episodes: Mapped[list["Episode"]] = relationship(back_populates="season", cascade="all, delete-orphan")

class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    season_id: Mapped[str] = mapped_column(String(36), ForeignKey("seasons.id"), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=True)
    air_date: Mapped[date] = mapped_column(Date, nullable=True)
    in_emby: Mapped[bool] = mapped_column(Boolean, default=False, comment="Emby中是否已存在")
    emby_item_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    season: Mapped["Season"] = relationship(back_populates="episodes")
```

- [ ] **Step 3: Create backend/app/models/interaction.py**

```python
import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Boolean, DateTime, Float, Text, ForeignKey, Enum, func, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base
import enum

class UserMediaStatus(str, enum.Enum):
    WANT_TO_WATCH = "want_to_watch"
    WATCHING = "watching"
    WATCHED = "watched"
    SKIP = "skip"  # 不想看

class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    auto_download: Mapped[bool] = mapped_column(Boolean, default=False, comment="上映后自动下载")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Vote(Base):
    __tablename__ = "votes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Rating(Base):
    __tablename__ = "ratings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False, comment="1-10分")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class UserMediaInteraction(Base):
    """用户对影视的个人状态 (想看/在看/已看/不想看)"""
    __tablename__ = "user_media_interactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    status: Mapped[UserMediaStatus] = mapped_column(Enum(UserMediaStatus), default=UserMediaStatus.WANT_TO_WATCH)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    related_media_id: Mapped[str] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class DownloadTask(Base):
    __tablename__ = "download_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    media_id: Mapped[str] = mapped_column(String(36), ForeignKey("media.id"), nullable=False)
    aria2_gid: Mapped[str] = mapped_column(String(64), nullable=True, comment="Aria2 task GID")
    quark_file_id: Mapped[str] = mapped_column(String(256), nullable=True, comment="夸克文件ID，用于后续清理")
    quark_share_code: Mapped[str] = mapped_column(String(128), nullable=True)
    episode_range: Mapped[str] = mapped_column(String(256), nullable=True, comment="下载的集数范围，如S01E01-S01E05")
    file_name: Mapped[str] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", comment="pending/downloading/completed/failed/cleaned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

class CleanupRecord(Base):
    """夸克网盘清理记录"""
    __tablename__ = "cleanup_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    download_task_id: Mapped[str] = mapped_column(String(36), ForeignKey("download_tasks.id"), nullable=False)
    quark_file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=True)
    cleaned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

- [ ] **Step 4: Create backend/app/models/__init__.py**

```python
from app.models.user import User, UserToken
from app.models.media import Media, Season, Episode, MediaType, MediaStatus
from app.models.interaction import (
    Subscription, Vote, Rating, UserMediaInteraction, UserMediaStatus,
    Notification, DownloadTask, CleanupRecord,
)
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/
git commit -m "feat: SQLAlchemy models — user, media, interaction"
```

---

## Phase 3: Backend Services

### Task 4: Pydantic Schemas

**Files:**
- Create: `backend/app/schemas/__init__.py`
- Create: `backend/app/schemas/auth.py`
- Create: `backend/app/schemas/media.py`
- Create: `backend/app/schemas/user.py`
- Create: `backend/app/schemas/task.py`

**Interfaces:**
- Produces: All request/response Pydantic models for the API

- [ ] **Step 1: Create backend/app/schemas/auth.py**

```python
from pydantic import BaseModel

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserInfo"

class UserInfo(BaseModel):
    id: str
    username: str
    emby_user_id: str
    is_admin: bool
```

- [ ] **Step 2: Create backend/app/schemas/media.py**

```python
from pydantic import BaseModel
from datetime import date, datetime
from typing import Optional

class MediaSearchRequest(BaseModel):
    keyword: str
    page: int = 1

class TMDBMediaResult(BaseModel):
    tmdb_id: int
    title: str
    original_title: Optional[str] = None
    media_type: str  # "movie" | "tv"
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    release_date: Optional[str] = None
    vote_average: Optional[float] = None
    in_library: bool = False
    library_status: Optional[str] = None
    subscription_count: int = 0

class MediaDetail(BaseModel):
    id: str
    tmdb_id: int
    title: str
    original_title: Optional[str] = None
    media_type: str
    overview: Optional[str] = None
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    release_date: Optional[date] = None
    vote_average: Optional[float] = None
    status: str
    subscription_count: int = 0
    watch_count: int = 0
    seasons: list["SeasonDetail"] = []

class SeasonDetail(BaseModel):
    id: str
    season_number: int
    name: Optional[str] = None
    episodes: list["EpisodeDetail"] = []

class EpisodeDetail(BaseModel):
    id: str
    episode_number: int
    name: Optional[str] = None
    air_date: Optional[date] = None
    in_emby: bool = False

class MediaListItem(BaseModel):
    id: str
    tmdb_id: int
    title: str
    media_type: str
    poster_path: Optional[str] = None
    release_date: Optional[date] = None
    vote_average: Optional[float] = None
    status: str
    subscription_count: int = 0
    watch_count: int = 0
    created_at: datetime
```

- [ ] **Step 3: Create backend/app/schemas/user.py**

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class SubscriptionInfo(BaseModel):
    id: str
    media_id: str
    media_title: str
    media_poster: Optional[str] = None
    voted: bool = False
    created_at: datetime

class RatingInfo(BaseModel):
    media_id: str
    score: int
    updated_at: datetime

class RatingRequest(BaseModel):
    score: int  # 1-10

class UserInteractionStatus(BaseModel):
    media_id: str
    status: str  # want_to_watch/watching/watched/skip

class AdminSubscriptionItem(BaseModel):
    id: str
    user_id: str
    username: str
    media_id: str
    media_title: str
    media_type: str
    vote_count: int
    created_at: datetime
```

- [ ] **Step 4: Create backend/app/schemas/task.py**

```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class DownloadTaskInfo(BaseModel):
    id: str
    media_id: str
    media_title: Optional[str] = None
    episode_range: Optional[str] = None
    file_name: Optional[str] = None
    status: str
    created_at: datetime
    completed_at: Optional[datetime] = None

class NotificationItem(BaseModel):
    id: str
    type: str
    title: str
    body: Optional[str] = None
    is_read: bool
    related_media_id: Optional[str] = None
    created_at: datetime

class ApproveRequest(BaseModel):
    media_id: str
    scan_frequency_hours: int = 24
```

- [ ] **Step 5: Create backend/app/schemas/__init__.py**

```python
from app.schemas.auth import LoginRequest, TokenResponse, UserInfo
from app.schemas.media import (
    MediaSearchRequest, TMDBMediaResult, MediaDetail, MediaListItem,
    SeasonDetail, EpisodeDetail,
)
from app.schemas.user import (
    SubscriptionInfo, RatingInfo, RatingRequest, UserInteractionStatus,
    AdminSubscriptionItem,
)
from app.schemas.task import DownloadTaskInfo, NotificationItem, ApproveRequest
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/
git commit -m "feat: Pydantic schemas for API"
```

---

### Task 5: External Service Clients

**Files:**
- Create: `backend/app/services/__init__.py`
- Create: `backend/app/services/tmdb.py`
- Create: `backend/app/services/emby.py`
- Create: `backend/app/services/cloudsaver.py`
- Create: `backend/app/services/aria2.py`
- Create: `backend/app/services/nastools.py`
- Create: `backend/app/services/alist.py`
- Create: `backend/app/services/pushplus.py`

**Interfaces:**
- Produces: Service classes with async methods for each external API

- [ ] **Step 1: Create backend/app/services/tmdb.py**

```python
import httpx
from app.config import settings

class TMDBService:
    def __init__(self):
        self.base_url = "https://api.tmdb.org/3"
        self.api_key = settings.TMDB_API_KEY

    async def search_multi(self, keyword: str, page: int = 1) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/search/multi", params={
                "api_key": self.api_key,
                "query": keyword,
                "language": "zh-CN",
                "page": page,
            })
            resp.raise_for_status()
            return resp.json()

    async def get_movie_detail(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/movie/{tmdb_id}", params={
                "api_key": self.api_key,
                "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_tv_detail(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/tv/{tmdb_id}", params={
                "api_key": self.api_key,
                "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_tv_season(self, tmdb_id: int, season_number: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/tv/{tmdb_id}/season/{season_number}", params={
                "api_key": self.api_key,
                "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

tmdb_service = TMDBService()
```

- [ ] **Step 2: Create backend/app/services/emby.py**

```python
import httpx
from app.config import settings

class EmbyService:
    def __init__(self):
        self.base_url = settings.EMBY_BASE_URL
        self.api_key = settings.EMBY_API_KEY

    async def authenticate_user(self, username: str, password: str) -> dict:
        """Authenticate user via Emby, return user info + access token."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={"X-Emby-Authorization": 'Emby UserId="", Client="LumenCloud", Device="Web", DeviceId="lumen", Version="1.0"'}
            )
            return resp.json() if resp.status_code == 200 else None

    async def get_items_by_provider(self, tmdb_id: int) -> dict:
        """Get Emby items matching a TMDB ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/Items", params={
                "api_key": self.api_key,
                "Recursive": "true",
                "HasTmdbId": "true",
                "Fields": "ProviderIds",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_missing_episodes(self, parent_id: str) -> dict:
        """Get missing episodes for a series."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/emby/Shows/Missing", params={
                "ParentId": parent_id,
                "api_key": self.api_key,
                "IncludeUnaired": "true",
                "IncludeSpecials": "false",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_user_episodes(self, user_id: str, parent_id: str) -> dict:
        """Get all episodes for a series under a user context."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/Users/{user_id}/Items", params={
                "ParentId": parent_id,
                "IncludeItemTypes": "Episode",
                "Recursive": "true",
                "api_key": self.api_key,
            })
            resp.raise_for_status()
            return resp.json()

    async def get_user_items(self, user_id: str, tmdb_id: int) -> dict:
        """Get user's play data for a specific TMDB item."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/Users/{user_id}/Items", params={
                "api_key": self.api_key,
                "Recursive": "true",
                "Fields": "ProviderIds,UserData",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_play_count(self, tmdb_id: int) -> int:
        """Aggregate play count across all users for a media item."""
        # Use admin API key to query all items with this TMDB ID
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/Items", params={
                "api_key": self.api_key,
                "Recursive": "true",
                "HasTmdbId": "true",
                "Fields": "UserData",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            data = resp.json()
            total = 0
            for item in data.get("Items", []):
                total += item.get("UserData", {}).get("PlayCount", 0)
            return total

emby_service = EmbyService()
```

- [ ] **Step 3: Create backend/app/services/cloudsaver.py**

```python
import httpx
from app.config import settings

class CloudSaverService:
    def __init__(self):
        self.base_url = settings.CLOUDSAVER_BASE_URL
        self.username = settings.CLOUDSAVER_USERNAME
        self.password = settings.CLOUDSAVER_PASSWORD
        self._token: str | None = None

    async def _login(self) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/user/login", json={
                "username": self.username,
                "password": self.password,
            })
            data = resp.json()
            self._token = data["data"]["token"]
            return self._token

    async def _get_token(self) -> str:
        if not self._token:
            await self._login()
        return self._token

    async def search(self, keyword: str) -> dict:
        """Search for resources by keyword."""
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/api/search", params={
                "keyword": keyword,
            }, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()

    async def quark_save(self, payload: dict) -> dict:
        """Save files to Quark cloud."""
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/quark/save", json=payload, headers={
                "Authorization": f"Bearer {token}",
            })
            resp.raise_for_status()
            return resp.json()

cloudsaver_service = CloudSaverService()
```

- [ ] **Step 4: Create backend/app/services/aria2.py**

```python
import uuid
import httpx
from app.config import settings

class Aria2Service:
    def __init__(self):
        self.rpc_url = settings.ARIA2_RPC_URL
        self.secret = settings.ARIA2_SECRET

    async def _rpc_call(self, method: str, params: list = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": str(uuid.uuid4()),
            "params": [f"token:{self.secret}"] + (params or []),
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.rpc_url, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def add_uri(self, uris: list[str], dir_path: str = None, filename: str = None) -> str:
        """Add download task, returns GID."""
        options = {}
        if dir_path:
            options["dir"] = dir_path
        if filename:
            options["out"] = filename
        result = await self._rpc_call("aria2.addUri", [uris, options])
        return result.get("result", "")

    async def tell_status(self, gid: str) -> dict:
        result = await self._rpc_call("aria2.tellStatus", [gid])
        return result.get("result", {})

    async def get_global_stat(self) -> dict:
        result = await self._rpc_call("aria2.getGlobalStat")
        return result.get("result", {})

    async def tell_active(self) -> list:
        result = await self._rpc_call("aria2.tellActive", [["gid", "files", "status"]])
        return result.get("result", [])

    async def tell_waiting(self, offset: int = 0, num: int = 1000) -> list:
        result = await self._rpc_call("aria2.tellWaiting", [offset, num, ["gid", "files", "status"]])
        return result.get("result", [])

    async def remove(self, gid: str) -> str:
        result = await self._rpc_call("aria2.remove", [gid])
        return result.get("result", "")

aria2_service = Aria2Service()
```

- [ ] **Step 5: Create backend/app/services/nastools.py**

```python
import httpx
from app.config import settings

class NasToolsService:
    def __init__(self):
        self.base_url = settings.NASTOOLS_BASE_URL
        self.username = settings.NASTOOLS_USERNAME
        self.password = settings.NASTOOLS_PASSWORD

    async def _login(self) -> str:
        """Login to NasTools and return session cookie."""
        form_data = {
            "next": "",
            "username": self.username,
            "password": self.password,
            "remember": "on",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/",
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            cookies = resp.headers.get("set-cookie", "")
            session_cookie = ""
            for c in cookies.split(","):
                if "session=" in c:
                    session_cookie = c.split(";")[0].strip()
                    break
            return session_cookie

    async def restart(self) -> bool:
        """Restart NasTools."""
        cookie = await self._login()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/do",
                json={"cmd": "restart", "data": {}},
                headers={"cookie": cookie},
            )
            return resp.status_code == 200

    async def directory_sync(self) -> bool:
        """Trigger directory sync in NasTools."""
        # NasTools needs a fresh login after restart
        import asyncio
        await asyncio.sleep(30)  # Wait for restart
        cookie = await self._login()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/do",
                json={"cmd": "run_directory_sync", "data": {"sid": []}},
                headers={"cookie": cookie},
            )
            return resp.status_code == 200

nastools_service = NasToolsService()
```

- [ ] **Step 6: Create backend/app/services/alist.py**

```python
import httpx
from app.config import settings

class AListService:
    def __init__(self):
        self.base_url = settings.ALIST_BASE_URL
        self.token = settings.ALIST_TOKEN

    async def list_files(self, path: str = "/quark", refresh: bool = False) -> dict:
        """List files in a directory."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/fs/list", json={
                "path": path,
                "refresh": refresh,
            }, headers={"Authorization": self.token})
            resp.raise_for_status()
            return resp.json()

    async def delete_file(self, path: str, file_names: list[str]) -> dict:
        """Delete files from AList."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/fs/remove", json={
                "dir": path,
                "names": file_names,
            }, headers={"Authorization": self.token})
            resp.raise_for_status()
            return resp.json()

    async def get_file_info(self, path: str) -> dict:
        """Get file info (for generating download links)."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/fs/get", json={
                "path": path,
            }, headers={"Authorization": self.token})
            resp.raise_for_status()
            return resp.json()

    async def get_download_link(self, path: str) -> str:
        """Get raw download link for a file."""
        info = await self.get_file_info(path)
        raw_url = info.get("data", {}).get("raw_url", "")
        return raw_url

alist_service = AListService()
```

- [ ] **Step 7: Create backend/app/services/pushplus.py**

```python
import httpx
from app.config import settings

class PushPlusService:
    def __init__(self):
        self.token = settings.PUSHPLUS_TOKEN
        self.url = "http://www.pushplus.plus/send"

    async def send(self, title: str, content: str, template: str = "html") -> bool:
        if not self.token:
            return False
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.url, params={
                "token": self.token,
                "title": title,
                "content": content,
                "template": template,
            })
            return resp.status_code == 200

pushplus_service = PushPlusService()
```

- [ ] **Step 8: Create backend/app/services/__init__.py**

```python
from app.services.tmdb import tmdb_service
from app.services.emby import emby_service
from app.services.cloudsaver import cloudsaver_service
from app.services.aria2 import aria2_service
from app.services.nastools import nastools_service
from app.services.alist import alist_service
from app.services.pushplus import pushplus_service
```

- [ ] **Step 9: Commit**

```bash
git add backend/app/services/
git commit -m "feat: external service clients — TMDB, Emby, CloudSaver, Aria2, NasTools, AList, PushPlus"
```

---

## Phase 4: API Routes & Auth

### Task 6: Auth API

**Files:**
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/deps.py`
- Create: `backend/app/api/auth.py`

**Interfaces:**
- Consumes: `User`, `UserToken` models, `emby_service`, schemas
- Produces: `POST /api/auth/login`, `GET /api/auth/me`, `get_current_user` dependency

- [ ] **Step 1: Create backend/app/api/deps.py**

```python
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError, jwt
from app.config import settings
from app.database import get_db
from app.models import User

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

def create_jwt(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
```

- [ ] **Step 2: Create backend/app/api/auth.py**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import User
from app.schemas.auth import LoginRequest, TokenResponse, UserInfo
from app.api.deps import create_jwt, get_current_user
from app.services.emby import emby_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    # Verify with Emby
    emby_result = await emby_service.authenticate_user(req.username, req.password)
    if emby_result is None:
        raise HTTPException(status_code=401, detail="Emby authentication failed")

    emby_user = emby_result.get("User", {})
    emby_user_id = emby_user.get("Id")
    emby_username = emby_user.get("Name", req.username)
    emby_token = emby_result.get("AccessToken")

    # Find or create local user
    result = await db.execute(select(User).where(User.emby_user_id == emby_user_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(emby_user_id=emby_user_id, username=emby_username, is_admin=False)
        db.add(user)
        await db.flush()

    # Issue JWT
    jwt_token = create_jwt(user.id)

    return TokenResponse(
        access_token=jwt_token,
        user=UserInfo(
            id=user.id,
            username=user.username,
            emby_user_id=user.emby_user_id,
            is_admin=user.is_admin,
        )
    )

@router.get("/me", response_model=UserInfo)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        emby_user_id=current_user.emby_user_id,
        is_admin=current_user.is_admin,
    )
```

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/
git commit -m "feat: auth API — Emby-based login with JWT"
```

---

### Task 7: Media & Subscription API

**Files:**
- Create: `backend/app/api/media.py`
- Create: `backend/app/api/subscription.py`
- Create: `backend/app/api/interaction.py`

**Interfaces:**
- Consumes: Models, schemas, service clients, deps
- Produces: Media search/browse, subscription CRUD, rating, user status APIs

- [ ] **Step 1: Create backend/app/api/media.py**

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import Media, Season, Episode, MediaType, MediaStatus, Subscription, Vote
from app.schemas.media import MediaDetail, MediaListItem, TMDBMediaResult, SeasonDetail, EpisodeDetail
from app.services.tmdb import tmdb_service
from app.services.emby import emby_service

router = APIRouter(prefix="/api/media", tags=["media"])

@router.get("/search")
async def search_media(keyword: str, page: int = Query(1, ge=1)):
    """Search TMDB and overlay local status."""
    tmdb_data = await tmdb_service.search_multi(keyword, page)
    results = []
    for item in tmdb_data.get("results", []):
        media_type = item.get("media_type")
        if media_type not in ("movie", "tv"):
            continue
        tmdb_id = item["id"]
        tmdb_result = TMDBMediaResult(
            tmdb_id=tmdb_id,
            title=item.get("title") or item.get("name", ""),
            original_title=item.get("original_title") or item.get("original_name"),
            media_type=media_type,
            overview=item.get("overview"),
            poster_path=item.get("poster_path"),
            release_date=item.get("release_date") or item.get("first_air_date"),
            vote_average=item.get("vote_average"),
        )
        # Check local status
        # ponytail: inline check, no separate query service for now
        results.append(tmdb_result)
    return {"results": results, "total_results": tmdb_data.get("total_results", 0), "page": page}

@router.get("/{media_id}", response_model=MediaDetail)
async def get_media_detail(media_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Media).where(Media.id == media_id))
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")

    # Count subscriptions
    sub_count_result = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.media_id == media_id)
    )
    sub_count = sub_count_result.scalar() or 0

    # Get watch count from Emby
    watch_count = await emby_service.get_play_count(media.tmdb_id)

    # Get seasons
    season_result = await db.execute(
        select(Season).where(Season.media_id == media_id).order_by(Season.season_number)
    )
    seasons = []
    for s in season_result.scalars():
        ep_result = await db.execute(
            select(Episode).where(Episode.season_id == s.id).order_by(Episode.episode_number)
        )
        episodes = [
            EpisodeDetail(id=e.id, episode_number=e.episode_number, name=e.name, air_date=e.air_date, in_emby=e.in_emby)
            for e in ep_result.scalars()
        ]
        seasons.append(SeasonDetail(id=s.id, season_number=s.season_number, name=s.name, episodes=episodes))

    return MediaDetail(
        id=media.id, tmdb_id=media.tmdb_id, title=media.title,
        original_title=media.original_title, media_type=media.media_type.value,
        overview=media.overview, poster_path=media.poster_path, backdrop_path=media.backdrop_path,
        release_date=media.release_date, vote_average=media.vote_average,
        status=media.status.value, subscription_count=sub_count, watch_count=watch_count,
        seasons=seasons,
    )

@router.get("/", response_model=list[MediaListItem])
async def list_media(
    status: str = None, media_type: str = None,
    db: AsyncSession = Depends(get_db),
):
    """Browse media in library."""
    query = select(Media)
    if status:
        query = query.where(Media.status == status)
    if media_type:
        query = query.where(Media.media_type == media_type)
    query = query.order_by(Media.created_at.desc()).limit(100)
    result = await db.execute(query)
    items = []
    for m in result.scalars():
        sub_count_result = await db.execute(
            select(func.count(Subscription.id)).where(Subscription.media_id == m.id)
        )
        sub_count = sub_count_result.scalar() or 0
        items.append(MediaListItem(
            id=m.id, tmdb_id=m.tmdb_id, title=m.title, media_type=m.media_type.value,
            poster_path=m.poster_path, release_date=m.release_date, vote_average=m.vote_average,
            status=m.status.value, subscription_count=sub_count, watch_count=0,
            created_at=m.created_at,
        ))
    return items
```

- [ ] **Step 2: Create backend/app/api/subscription.py**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, Media, Subscription, Vote
from app.api.deps import get_current_user
from app.schemas.user import SubscriptionInfo

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

@router.post("/{media_id}")
async def subscribe_media(
    media_id: str,
    auto_download: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(Subscription).where(Subscription.user_id == current_user.id, Subscription.media_id == media_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already subscribed")

    sub = Subscription(user_id=current_user.id, media_id=media_id, auto_download=auto_download)
    db.add(sub)
    await db.flush()
    return {"id": sub.id, "message": "Subscribed"}

@router.delete("/{media_id}")
async def unsubscribe_media(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == current_user.id, Subscription.media_id == media_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail="Not subscribed")
    await db.delete(sub)
    return {"message": "Unsubscribed"}

@router.get("/", response_model=list[SubscriptionInfo])
async def my_subscriptions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == current_user.id).order_by(Subscription.created_at.desc())
    )
    subs = []
    for s in result.scalars():
        media_result = await db.execute(select(Media).where(Media.id == s.media_id))
        media = media_result.scalar_one_or_none()
        subs.append(SubscriptionInfo(
            id=s.id, media_id=s.media_id,
            media_title=media.title if media else "Unknown",
            media_poster=media.poster_path if media else None,
            created_at=s.created_at,
        ))
    return subs

@router.post("/{media_id}/vote")
async def vote_media(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(Vote).where(Vote.user_id == current_user.id, Vote.media_id == media_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already voted")

    vote = Vote(user_id=current_user.id, media_id=media_id)
    db.add(vote)
    await db.flush()

    # Count votes
    count_result = await db.execute(
        select(func.count(Vote.id)).where(Vote.media_id == media_id)
    )
    return {"vote_count": count_result.scalar()}

@router.delete("/{media_id}/vote")
async def unvote_media(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Vote).where(Vote.user_id == current_user.id, Vote.media_id == media_id)
    )
    vote = result.scalar_one_or_none()
    if vote is None:
        raise HTTPException(status_code=404, detail="Not voted")
    await db.delete(vote)
    return {"message": "Vote removed"}
```

- [ ] **Step 3: Create backend/app/api/interaction.py**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import User, Rating, UserMediaInteraction, UserMediaStatus
from app.api.deps import get_current_user
from app.schemas.user import RatingRequest, RatingInfo, UserInteractionStatus

router = APIRouter(prefix="/api/interactions", tags=["interactions"])

@router.post("/rating/{media_id}")
async def rate_media(
    media_id: str, req: RatingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.score < 1 or req.score > 10:
        raise HTTPException(status_code=400, detail="Score must be 1-10")

    result = await db.execute(
        select(Rating).where(Rating.user_id == current_user.id, Rating.media_id == media_id)
    )
    rating = result.scalar_one_or_none()
    if rating:
        rating.score = req.score
    else:
        rating = Rating(user_id=current_user.id, media_id=media_id, score=req.score)
        db.add(rating)
    await db.flush()
    return RatingInfo(media_id=media_id, score=rating.score, updated_at=rating.updated_at)

@router.get("/rating/{media_id}", response_model=RatingInfo)
async def get_my_rating(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Rating).where(Rating.user_id == current_user.id, Rating.media_id == media_id)
    )
    rating = result.scalar_one_or_none()
    if rating is None:
        raise HTTPException(status_code=404, detail="Not rated")
    return RatingInfo(media_id=media_id, score=rating.score, updated_at=rating.updated_at)

@router.put("/status/{media_id}")
async def set_media_status(
    media_id: str, status: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if status not in [s.value for s in UserMediaStatus]:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    result = await db.execute(
        select(UserMediaInteraction).where(
            UserMediaInteraction.user_id == current_user.id,
            UserMediaInteraction.media_id == media_id,
        )
    )
    interaction = result.scalar_one_or_none()
    if interaction:
        interaction.status = UserMediaStatus(status)
    else:
        interaction = UserMediaInteraction(
            user_id=current_user.id, media_id=media_id, status=UserMediaStatus(status)
        )
        db.add(interaction)
    await db.flush()
    return UserInteractionStatus(media_id=media_id, status=status)

@router.get("/status/{media_id}", response_model=UserInteractionStatus)
async def get_my_status(
    media_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserMediaInteraction).where(
            UserMediaInteraction.user_id == current_user.id,
            UserMediaInteraction.media_id == media_id,
        )
    )
    interaction = result.scalar_one_or_none()
    if interaction is None:
        return UserInteractionStatus(media_id=media_id, status="none")
    return UserInteractionStatus(media_id=media_id, status=interaction.status.value)
```

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/media.py backend/app/api/subscription.py backend/app/api/interaction.py
git commit -m "feat: media, subscription, interaction APIs"
```

---

### Task 8: Admin & Notification API

**Files:**
- Create: `backend/app/api/admin.py`
- Create: `backend/app/api/task.py`
- Create: `backend/app/api/notification.py`
- Modify: `backend/app/main.py` (register all routers)

**Interfaces:**
- Consumes: Models, schemas, deps
- Produces: Admin endpoints, task monitoring, notification endpoints, all routers registered on app

- [ ] **Step 1: Create backend/app/api/admin.py**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import User, Media, Subscription, Vote, MediaStatus
from app.api.deps import get_admin_user
from app.schemas.user import AdminSubscriptionItem
from app.schemas.task import ApproveRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])

@router.get("/subscriptions", response_model=list[AdminSubscriptionItem])
async def list_subscriptions(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """List all subscriptions with vote counts for admin approval."""
    result = await db.execute(select(Subscription).order_by(Subscription.created_at.desc()))
    items = []
    for s in result.scalars():
        user_result = await db.execute(select(User).where(User.id == s.user_id))
        user = user_result.scalar_one_or_none()
        media_result = await db.execute(select(Media).where(Media.id == s.media_id))
        media = media_result.scalar_one_or_none()
        vote_count_result = await db.execute(
            select(func.count(Vote.id)).where(Vote.media_id == s.media_id)
        )
        vote_count = vote_count_result.scalar() or 0
        items.append(AdminSubscriptionItem(
            id=s.id, user_id=s.user_id, username=user.username if user else "Unknown",
            media_id=s.media_id, media_title=media.title if media else "Unknown",
            media_type=media.media_type.value if media else "unknown",
            vote_count=vote_count, created_at=s.created_at,
        ))
    return items

@router.post("/approve")
async def approve_subscription(
    req: ApproveRequest,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve a media for tracking, set its status."""
    result = await db.execute(select(Media).where(Media.id == req.media_id))
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    media.status = MediaStatus.TRACKING
    media.scan_frequency_hours = req.scan_frequency_hours
    await db.flush()
    # ponytail: trigger immediate scan via ARQ — enqueue task
    return {"message": f"{media.title} approved for tracking"}

@router.delete("/media/{media_id}")
async def delete_media(
    media_id: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard delete media and all related data."""
    result = await db.execute(select(Media).where(Media.id == media_id))
    media = result.scalar_one_or_none()
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    # Cascade deletion through relationships
    await db.delete(media)
    return {"message": f"{media.title} deleted"}
```

- [ ] **Step 2: Create backend/app/api/task.py**

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import User, DownloadTask, Media
from app.api.deps import get_admin_user
from app.schemas.task import DownloadTaskInfo

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

@router.get("/", response_model=list[DownloadTaskInfo])
async def list_tasks(
    status: str = None,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(DownloadTask).order_by(DownloadTask.created_at.desc())
    if status:
        query = query.where(DownloadTask.status == status)
    query = query.limit(50)
    result = await db.execute(query)
    tasks = []
    for t in result.scalars():
        media_result = await db.execute(select(Media).where(Media.id == t.media_id))
        media = media_result.scalar_one_or_none()
        tasks.append(DownloadTaskInfo(
            id=t.id, media_id=t.media_id,
            media_title=media.title if media else None,
            episode_range=t.episode_range, file_name=t.file_name,
            status=t.status, created_at=t.created_at, completed_at=t.completed_at,
        ))
    return tasks

@router.get("/{task_id}", response_model=DownloadTaskInfo)
async def get_task(
    task_id: str,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DownloadTask).where(DownloadTask.id == task_id))
    t = result.scalar_one_or_none()
    if t is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")
    media_result = await db.execute(select(Media).where(Media.id == t.media_id))
    media = media_result.scalar_one_or_none()
    return DownloadTaskInfo(
        id=t.id, media_id=t.media_id,
        media_title=media.title if media else None,
        episode_range=t.episode_range, file_name=t.file_name,
        status=t.status, created_at=t.created_at, completed_at=t.completed_at,
    )
```

- [ ] **Step 3: Create backend/app/api/notification.py**

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.database import get_db
from app.models import User, Notification
from app.api.deps import get_current_user
from app.schemas.task import NotificationItem

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

@router.get("/", response_model=list[NotificationItem])
async def list_notifications(
    unread_only: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Notification).where(Notification.user_id == current_user.id)
    if unread_only:
        query = query.where(Notification.is_read == False)
    query = query.order_by(Notification.created_at.desc()).limit(50)
    result = await db.execute(query)
    return [
        NotificationItem(
            id=n.id, type=n.type, title=n.title, body=n.body,
            is_read=n.is_read, related_media_id=n.related_media_id, created_at=n.created_at,
        )
        for n in result.scalars()
    ]

@router.put("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == current_user.id)
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True
    await db.flush()
    return {"message": "Marked as read"}

@router.put("/read-all")
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        update(Notification).where(Notification.user_id == current_user.id, Notification.is_read == False).values(is_read=True)
    )
    await db.flush()
    return {"message": "All marked as read"}
```

- [ ] **Step 4: Update backend/app/main.py to register all routers**

Modify `backend/app/main.py` — add router registrations before `return app`:

```python
from app.api.auth import router as auth_router
from app.api.media import router as media_router
from app.api.subscription import router as subscription_router
from app.api.interaction import router as interaction_router
from app.api.admin import router as admin_router
from app.api.task import router as task_router
from app.api.notification import router as notification_router

app.include_router(auth_router)
app.include_router(media_router)
app.include_router(subscription_router)
app.include_router(interaction_router)
app.include_router(admin_router)
app.include_router(task_router)
app.include_router(notification_router)
```

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/admin.py backend/app/api/task.py backend/app/api/notification.py backend/app/main.py
git commit -m "feat: admin, task, notification APIs — register all routers"
```

---

## Phase 5: Background Tasks (ARQ)

### Task 9: ARQ Worker & Download Pipeline

**Files:**
- Create: `backend/app/tasks/__init__.py`
- Create: `backend/app/tasks/worker.py`
- Create: `backend/app/tasks/scan.py`
- Create: `backend/app/tasks/download.py`
- Create: `backend/app/tasks/cleanup.py`
- Create: `backend/app/api/webhook.py`

**Interfaces:**
- Consumes: All models, all service clients
- Produces: ARQ worker with download pipeline, scheduled scan, cleanup tasks, Aria2 webhook endpoint

- [ ] **Step 1: Create backend/app/tasks/worker.py**

```python
from arq.connections import RedisSettings
from app.config import settings

async def startup(ctx):
    pass

async def shutdown(ctx):
    pass

class WorkerSettings:
    functions = [
        "app.tasks.scan.scan_all_media",
        "app.tasks.scan.scan_single_media", 
        "app.tasks.download.run_download_pipeline",
        "app.tasks.cleanup.cleanup_quark_files",
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    on_startup = startup
    on_shutdown = shutdown
```

- [ ] **Step 2: Create backend/app/tasks/scan.py**

```python
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import async_session
from app.models import Media, MediaStatus, MediaType
from app.services.emby import emby_service

async def scan_all_media(ctx):
    """Scheduled full scan: check all TRACKING media for missing episodes and trigger downloads."""
    async with async_session() as db:
        result = await db.execute(
            select(Media).where(Media.status == MediaStatus.TRACKING)
        )
        media_list = result.scalars().all()

    for media in media_list:
        # Check if due for scan based on frequency
        if media.last_scanned_at:
            hours_since = (datetime.now(timezone.utc) - media.last_scanned_at).total_seconds() / 3600
            if hours_since < media.scan_frequency_hours:
                continue

        # ponytail: enqueue individual media scan
        await ctx["redis"].enqueue_job("app.tasks.scan.scan_single_media", media.id)

async def scan_single_media(ctx, media_id: str):
    """Scan a single media for missing episodes and trigger download if needed."""
    async with async_session() as db:
        result = await db.execute(select(Media).where(Media.id == media_id))
        media = result.scalar_one_or_none()
        if media is None:
            return

        # 1. Check Emby for this media
        emby_items = await emby_service.get_items_by_provider(media.tmdb_id)
        emby_item_list = emby_items.get("Items", [])

        if not emby_item_list:
            media.last_scanned_at = datetime.now(timezone.utc)
            await db.commit()
            return

        emby_item = emby_item_list[0]
        parent_id = emby_item.get("Id")

        # 2. Get missing episodes
        if media.media_type == MediaType.TV and parent_id:
            missing = await emby_service.get_missing_episodes(parent_id)
            missing_items = missing.get("Items", [])

            if missing_items:
                missing_codes = []
                for ep in missing_items:
                    season = ep.get("ParentIndexNumber", 1)
                    episode = ep.get("IndexNumber", 1)
                    missing_codes.append(f"S{season:02d}E{episode:02d}")

                # Trigger download pipeline
                await ctx["redis"].enqueue_job(
                    "app.tasks.download.run_download_pipeline",
                    media_id, missing_codes,
                )

        media.last_scanned_at = datetime.now(timezone.utc)
        await db.commit()
```

- [ ] **Step 3: Create backend/app/tasks/download.py**

```python
import re
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import async_session
from app.models import Media, DownloadTask, MediaStatus, Notification, CleanupRecord
from app.services.cloudsaver import cloudsaver_service
from app.services.aria2 import aria2_service
from app.services.alist import alist_service
from app.services.pushplus import pushplus_service

async def run_download_pipeline(ctx, media_id: str, missing_codes: list[str]):
    """Full download pipeline for a media's missing episodes."""
    async with async_session() as db:
        result = await db.execute(select(Media).where(Media.id == media_id))
        media = result.scalar_one_or_none()
        if media is None:
            return

        media.status = MediaStatus.DOWNLOADING
        await db.commit()

    # 1. Search CloudSaver
    search_result = await cloudsaver_service.search(media.title)
    data_list = search_result.get("data", [])

    # 2. Extract Quark resources matching missing episodes
    quark_resources = []
    for data_item in data_list:
        for list_item in data_item.get("list", []):
            title = list_item.get("title", "")
            cloud_links = list_item.get("cloudLinks", [])
            for link in cloud_links:
                if link.get("cloudType") == "quark" and link.get("link"):
                    quark_resources.append({
                        "title": title,
                        "link": link["link"],
                    })

    if not quark_resources:
        media.status = MediaStatus.TRACKING
        await db.commit()
        return

    # 3. Match episodes via CloudSaver save + AList download
    for ep_code in missing_codes:
        # Find matching resource
        matched = None
        for res in quark_resources:
            if _match_episode(res["title"], ep_code):
                matched = res
                break

        if not matched:
            continue

        # Extract share code
        share_match = re.search(r'https://pan\.quark\.cn/s/([^&/]+)', matched["link"])
        if not share_match:
            continue
        share_code = share_match.group(1)

        # 4. Save to Quark via CloudSaver
        save_result = await cloudsaver_service.quark_save({
            "shareCode": share_code,
            "folderId": "",  # ponytail: default folder
        })

        if not save_result.get("success"):
            continue

        # 5. Get file from AList for download
        try:
            await asyncio.sleep(3)
            file_list = await alist_service.list_files("/quark", refresh=True)
            files = file_list.get("data", {}).get("content", [])

            # Find matching file
            for f in files:
                fname = f.get("name", "")
                if _match_episode(fname, ep_code):
                    # Get download link
                    download_link = await alist_service.get_download_link(f"/quark/{fname}")
                    if download_link:
                        # 6. Add to Aria2
                        gid = await aria2_service.add_uri(
                            [download_link],
                            dir_path="/downloads",
                            filename=fname,
                        )

                        # Create download task record
                        task = DownloadTask(
                            media_id=media.id,
                            aria2_gid=gid,
                            quark_file_id=f.get("name", ""),
                            quark_share_code=share_code,
                            episode_range=ep_code,
                            file_name=fname,
                            status="downloading",
                        )
                        db.add(task)
                    break
        except Exception:
            continue

    await db.commit()

def _match_episode(filename: str, ep_code: str) -> bool:
    """Check if filename matches episode code like S01E05."""
    name = filename.lower()
    code_lower = ep_code.lower()
    if code_lower in name:
        return True
    # Also try Chinese format: 第5集
    match = re.match(r'S(\d+)E(\d+)', ep_code, re.IGNORECASE)
    if match:
        season, episode = match.groups()
        cn_pattern = f"第{int(episode)}集"
        if cn_pattern in name:
            return True
    return False
```

- [ ] **Step 4: Create backend/app/tasks/cleanup.py**

```python
from datetime import datetime, timezone
from sqlalchemy import select
from app.database import async_session
from app.models import DownloadTask, CleanupRecord
from app.services.alist import alist_service

async def cleanup_quark_files(ctx):
    """Weekly task: clean up Quark files that have been confirmed as downloaded + organized."""
    async with async_session() as db:
        result = await db.execute(
            select(DownloadTask).where(
                DownloadTask.status == "completed",
                DownloadTask.quark_file_id.isnot(None),
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            try:
                await alist_service.delete_file("/quark", [task.quark_file_id])

                # Record cleanup
                record = CleanupRecord(
                    download_task_id=task.id,
                    quark_file_id=task.quark_file_id,
                    file_name=task.file_name,
                )
                db.add(record)

                task.status = "cleaned"
            except Exception:
                continue

        await db.commit()
```

- [ ] **Step 5: Create backend/app/api/webhook.py**

```python
from fastapi import APIRouter, Request
from sqlalchemy import select
from app.database import async_session
from app.models import DownloadTask, Media, MediaStatus
from app.services.nastools import nastools_service
from app.services.pushplus import pushplus_service

router = APIRouter(prefix="/api/webhook", tags=["webhook"])

@router.post("/aria2")
async def aria2_callback(request: Request):
    """Aria2 on-download-complete webhook."""
    body = await request.json()
    gid = body.get("gid", "")

    if not gid:
        return {"status": "ignored"}

    async with async_session() as db:
        result = await db.execute(
            select(DownloadTask).where(DownloadTask.aria2_gid == gid)
        )
        task = result.scalar_one_or_none()
        if task is None:
            return {"status": "ignored"}

        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)

        # Get media info
        media_result = await db.execute(select(Media).where(Media.id == task.media_id))
        media = media_result.scalar_one_or_none()

        # Check if all episodes for this media are downloaded
        pending_result = await db.execute(
            select(DownloadTask).where(
                DownloadTask.media_id == task.media_id,
                DownloadTask.status == "downloading",
            )
        )
        if not pending_result.scalar_one_or_none():
            media.status = MediaStatus.COMPLETED

        await db.commit()

    # Trigger NasTools directory sync
    try:
        await nastools_service.restart()
        import asyncio
        await asyncio.sleep(45)
        await nastools_service.directory_sync()
    except Exception:
        pass

    # Send PushPlus notification
    media_title = media.title if media else "未知资源"
    await pushplus_service.send(
        title=f"🎬 {media_title}",
        content=f"<b>下载完成</b><br>文件: {task.file_name}<br>集数: {task.episode_range}",
    )

    return {"status": "ok"}
```

- [ ] **Step 6: Register webhook router in main.py**

Add to `backend/app/main.py`:
```python
from app.api.webhook import router as webhook_router
app.include_router(webhook_router)
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/tasks/ backend/app/api/webhook.py backend/app/main.py
git commit -m "feat: ARQ worker — scan, download pipeline, cleanup, Aria2 webhook"
```

---

## Phase 6: Frontend

### Task 10: Frontend Shell & Auth

**Files:**
- Create: `frontend/src/main.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/router/index.ts`
- Create: `frontend/src/stores/auth.ts`
- Create: `frontend/src/api/index.ts`
- Create: `frontend/src/views/LoginView.vue`

- [ ] **Step 1: Create frontend/src/main.ts**

```typescript
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}
app.mount('#app')
```

- [ ] **Step 2: Create frontend/src/App.vue**

```vue
<template>
  <router-view />
</template>
```

- [ ] **Step 3: Create frontend/src/router/index.ts**

```typescript
import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('../views/LoginView.vue') },
    { path: '/', name: 'square', component: () => import('../views/MediaSquareView.vue'), meta: { requiresAuth: true } },
    { path: '/media/:id', name: 'media-detail', component: () => import('../views/MediaDetailView.vue'), meta: { requiresAuth: true } },
    { path: '/my-list', name: 'my-list', component: () => import('../views/MyList.vue'), meta: { requiresAuth: true } },
    { path: '/notifications', name: 'notifications', component: () => import('../views/NotificationCenter.vue'), meta: { requiresAuth: true } },
    { path: '/admin', name: 'admin', component: () => import('../views/AdminDashboard.vue'), meta: { requiresAuth: true, requiresAdmin: true } },
    { path: '/admin/tasks', name: 'tasks', component: () => import('../views/TaskMonitor.vue'), meta: { requiresAuth: true, requiresAdmin: true } },
  ]
})

router.beforeEach((to, _from, next) => {
  const token = localStorage.getItem('token')
  if (to.meta.requiresAuth && !token) {
    next('/login')
  } else {
    next()
  }
})

export default router
```

- [ ] **Step 4: Create frontend/src/api/index.ts**

```typescript
import axios from 'axios'

const api = axios.create({ baseURL: '/api' })

api.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  res => res,
  err => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export default api

export const authAPI = {
  login: (username: string, password: string) => api.post('/auth/login', { username, password }),
  me: () => api.get('/auth/me'),
}

export const mediaAPI = {
  search: (keyword: string, page = 1) => api.get('/media/search', { params: { keyword, page } }),
  detail: (id: string) => api.get(`/media/${id}`),
  list: (params?: any) => api.get('/media/', { params }),
}

export const subscriptionAPI = {
  subscribe: (mediaId: string) => api.post(`/subscriptions/${mediaId}`),
  unsubscribe: (mediaId: string) => api.delete(`/subscriptions/${mediaId}`),
  list: () => api.get('/subscriptions/'),
  vote: (mediaId: string) => api.post(`/subscriptions/${mediaId}/vote`),
  unvote: (mediaId: string) => api.delete(`/subscriptions/${mediaId}/vote`),
}

export const interactionAPI = {
  rate: (mediaId: string, score: number) => api.post(`/interactions/rating/${mediaId}`, { score }),
  getRating: (mediaId: string) => api.get(`/interactions/rating/${mediaId}`),
  setStatus: (mediaId: string, status: string) => api.put(`/interactions/status/${mediaId}?status=${status}`),
  getStatus: (mediaId: string) => api.get(`/interactions/status/${mediaId}`),
}

export const adminAPI = {
  subscriptions: () => api.get('/admin/subscriptions'),
  approve: (mediaId: string, scanFrequency = 24) => api.post('/admin/approve', { media_id: mediaId, scan_frequency_hours: scanFrequency }),
  deleteMedia: (mediaId: string) => api.delete(`/admin/media/${mediaId}`),
}

export const taskAPI = {
  list: (status?: string) => api.get('/tasks/', { params: { status } }),
  detail: (id: string) => api.get(`/tasks/${id}`),
}

export const notificationAPI = {
  list: (unreadOnly = false) => api.get('/notifications/', { params: { unread_only: unreadOnly } }),
  markRead: (id: string) => api.put(`/notifications/${id}/read`),
  markAllRead: () => api.put('/notifications/read-all'),
}
```

- [ ] **Step 5: Create frontend/src/stores/auth.ts**

```typescript
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authAPI } from '../api'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<any>(null)
  const token = ref<string | null>(localStorage.getItem('token'))

  async function login(username: string, password: string) {
    const { data } = await authAPI.login(username, password)
    token.value = data.access_token
    user.value = data.user
    localStorage.setItem('token', data.access_token)
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('token')
  }

  async function fetchMe() {
    if (!token.value) return
    try {
      const { data } = await authAPI.me()
      user.value = data
    } catch {
      logout()
    }
  }

  return { user, token, login, logout, fetchMe }
})
```

- [ ] **Step 6: Create frontend/src/views/LoginView.vue**

```vue
<template>
  <div class="login-container">
    <el-card class="login-card">
      <h1>拾光云映</h1>
      <p class="subtitle">使用 Emby 账号登录</p>
      <el-form @submit.prevent="handleLogin">
        <el-form-item>
          <el-input v-model="username" placeholder="Emby 用户名" prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="Emby 密码" prefix-icon="Lock" show-password />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" style="width:100%">登录</el-button>
      </el-form>
      <p v-if="error" class="error">{{ error }}</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')
const router = useRouter()
const auth = useAuthStore()

async function handleLogin() {
  loading.value = true
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    router.push('/')
  } catch (e: any) {
    error.value = e.response?.data?.detail || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container { display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #f0f2f5; }
.login-card { width: 400px; }
.login-card h1 { text-align: center; margin-bottom: 4px; }
.subtitle { text-align: center; color: #999; margin-bottom: 24px; }
.error { color: #f56c6c; text-align: center; }
</style>
```

- [ ] **Step 8: Commit**

```bash
git add frontend/
git commit -m "feat: frontend shell — Vue 3, router, auth store, login page"
```

---

### Task 11: Frontend Media Views

**Files:**
- Create: `frontend/src/views/MediaSquareView.vue`
- Create: `frontend/src/views/MediaDetailView.vue`
- Create: `frontend/src/components/MediaCard.vue`
- Create: `frontend/src/components/MediaSearch.vue`
- Create: `frontend/src/components/SeasonEpisodeList.vue`
- Create: `frontend/src/components/SubscribeButton.vue`

- [ ] **Step 1: Create frontend/src/views/MediaSquareView.vue**

```vue
<template>
  <div class="layout">
    <el-header>
      <el-menu mode="horizontal" router :default-active="$route.path">
        <el-menu-item index="/">影视广场</el-menu-item>
        <el-menu-item index="/my-list">我的片单</el-menu-item>
        <el-menu-item index="/notifications">
          <el-badge :value="unreadCount" :hidden="unreadCount === 0">通知</el-badge>
        </el-menu-item>
        <el-menu-item v-if="auth.user?.is_admin" index="/admin">管理</el-menu-item>
        <el-menu-item style="margin-left:auto" @click="auth.logout()">退出</el-menu-item>
      </el-menu>
    </el-header>
    <el-main>
      <MediaSearch @search="handleSearch" />
      <el-row :gutter="16">
        <el-col v-for="item in results" :key="item.tmdb_id" :xs="12" :sm="8" :md="6" :lg="4">
          <MediaCard :item="item" @click="goDetail(item)" />
        </el-col>
      </el-row>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { mediaAPI } from '../api'
import MediaSearch from '../components/MediaSearch.vue'
import MediaCard from '../components/MediaCard.vue'

const auth = useAuthStore()
const router = useRouter()
const results = ref<any[]>([])
const unreadCount = ref(0)

async function handleSearch(keyword: string) {
  const { data } = await mediaAPI.search(keyword)
  results.value = data.results
}

function goDetail(item: any) {
  router.push(`/media/${item.tmdb_id}`)
}
</script>

<style scoped>
.layout { min-height: 100vh; }
.el-header { padding: 0; }
.el-main { padding: 20px; }
</style>
```

- [ ] **Step 2: Create frontend/src/components/MediaSearch.vue**

```vue
<template>
  <div class="search-bar">
    <el-input v-model="keyword" placeholder="搜索影视..." @keyup.enter="$emit('search', keyword)" clearable size="large">
      <template #append>
        <el-button @click="$emit('search', keyword)" :icon="Search" />
      </template>
    </el-input>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Search } from '@element-plus/icons-vue'

const keyword = ref('')
defineEmits(['search'])
</script>

<style scoped>
.search-bar { margin-bottom: 24px; max-width: 600px; }
</style>
```

- [ ] **Step 3: Create frontend/src/components/MediaCard.vue**

```vue
<template>
  <el-card shadow="hover" class="media-card" @click="$emit('click')">
    <img v-if="item.poster_path" :src="`https://image.tmdb.org/t/p/w300${item.poster_path}`" class="poster" />
    <div v-else class="poster-placeholder">无海报</div>
    <div class="info">
      <h3>{{ item.title }}</h3>
      <p class="meta">{{ item.media_type === 'movie' ? '电影' : '剧集' }} · {{ item.release_date?.slice(0, 4) || '未知' }}</p>
      <p v-if="item.vote_average" class="rating">⭐ {{ item.vote_average?.toFixed(1) }}</p>
    </div>
  </el-card>
</template>

<script setup lang="ts">
defineProps<{ item: any }>()
defineEmits(['click'])
</script>

<style scoped>
.media-card { cursor: pointer; margin-bottom: 16px; }
.poster { width: 100%; border-radius: 4px; aspect-ratio: 2/3; object-fit: cover; }
.poster-placeholder { width: 100%; aspect-ratio: 2/3; background: #eee; display: flex; align-items: center; justify-content: center; color: #999; }
.info h3 { margin: 8px 0 4px; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta { color: #999; font-size: 12px; }
.rating { color: #e6a23c; font-size: 12px; }
</style>
```

- [ ] **Step 4: Create frontend/src/views/MediaDetailView.vue — skeleton (placeholder)**

```vue
<template>
  <div class="layout">
    <el-header><!-- same nav as MediaSquareView — ponytail: extract to layout component later --></el-header>
    <el-main>
      <h1>{{ media?.title }}</h1>
      <p>{{ media?.overview }}</p>
      <p>👁 {{ media?.watch_count }}次观看 · 📊 {{ media?.subscription_count }}人订阅</p>
      <el-button type="primary" @click="subscribe">订阅</el-button>
      <el-button @click="vote">投票 (+1)</el-button>
      <SeasonEpisodeList v-if="media?.media_type === 'tv'" :seasons="media?.seasons || []" />
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { mediaAPI, subscriptionAPI } from '../api'
import SeasonEpisodeList from '../components/SeasonEpisodeList.vue'

const route = useRoute()
const media = ref<any>(null)

onMounted(async () => {
  // ponytail: fetch by tmdb_id — API needs lookup or direct detail
  const { data } = await mediaAPI.detail(route.params.id as string)
  media.value = data
})

async function subscribe() {
  await subscriptionAPI.subscribe(media.value.id)
}

async function vote() {
  await subscriptionAPI.vote(media.value.id)
}
</script>
```

- [ ] **Step 5: Create frontend/src/components/SeasonEpisodeList.vue** (placeholder)

```vue
<template>
  <div v-for="s in seasons" :key="s.id" class="season">
    <h3>第 {{ s.season_number }} 季</h3>
    <el-tag v-for="ep in s.episodes" :key="ep.id" :type="ep.in_emby ? 'success' : 'info'" style="margin:4px">
      第{{ ep.episode_number }}集 {{ ep.in_emby ? '✓' : '○' }}
    </el-tag>
  </div>
</template>

<script setup lang="ts">
defineProps<{ seasons: any[] }>()
</script>
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/views/MediaSquareView.vue frontend/src/views/MediaDetailView.vue frontend/src/components/
git commit -m "feat: frontend media views — square, detail, search, cards"
```

---

### Task 12: Remaining Frontend Pages

**Files:**
- Create: `frontend/src/views/MyList.vue`
- Create: `frontend/src/views/AdminDashboard.vue`
- Create: `frontend/src/views/TaskMonitor.vue`
- Create: `frontend/src/views/NotificationCenter.vue`
- Create: `frontend/src/components/RatingStars.vue`
- Create: `frontend/src/stores/notification.ts`

- [ ] **Step 1: Create frontend/src/views/MyList.vue**

```vue
<template>
  <div class="layout">
    <el-header><!-- same nav, ponytail: extract later --></el-header>
    <el-main>
      <h2>我的片单</h2>
      <el-tabs v-model="activeTab">
        <el-tab-pane label="订阅" name="subs">
          <el-table :data="subscriptions" style="width:100%">
            <el-table-column prop="media_title" label="影视" />
            <el-table-column label="操作">
              <template #default="{ row }">
                <el-button size="small" @click="unsubscribe(row.media_id)">取消订阅</el-button>
                <el-button size="small" type="warning" @click="setStatus(row.media_id, 'skip')">不想看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="不想看" name="skipped">
          <span>暂未实现 — 需要从 interactions 查询</span>
        </el-tab-pane>
      </el-tabs>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { subscriptionAPI, interactionAPI } from '../api'

const activeTab = ref('subs')
const subscriptions = ref<any[]>([])

onMounted(async () => {
  const { data } = await subscriptionAPI.list()
  subscriptions.value = data
})

async function unsubscribe(mediaId: string) {
  await subscriptionAPI.unsubscribe(mediaId)
  subscriptions.value = subscriptions.value.filter(s => s.media_id !== mediaId)
}

async function setStatus(mediaId: string, status: string) {
  await interactionAPI.setStatus(mediaId, status)
}
</script>
```

- [ ] **Step 2: Create frontend/src/views/AdminDashboard.vue**

```vue
<template>
  <div class="layout">
    <el-header><!-- nav --></el-header>
    <el-main>
      <h2>管理后台</h2>
      <el-tabs v-model="activeTab">
        <el-tab-pane label="订阅审批" name="approvals">
          <el-table :data="subs" style="width:100%">
            <el-table-column prop="media_title" label="影视" />
            <el-table-column prop="username" label="订阅者" />
            <el-table-column prop="vote_count" label="票数" />
            <el-table-column label="操作">
              <template #default="{ row }">
                <el-button type="success" size="small" @click="approve(row.media_id)">通过</el-button>
                <el-button type="danger" size="small" @click="deleteMedia(row.media_id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { adminAPI } from '../api'

const activeTab = ref('approvals')
const subs = ref<any[]>([])

onMounted(async () => {
  const { data } = await adminAPI.subscriptions()
  subs.value = data
})

async function approve(mediaId: string) {
  await adminAPI.approve(mediaId)
  subs.value = subs.value.filter(s => s.media_id !== mediaId)
}

async function deleteMedia(mediaId: string) {
  await adminAPI.deleteMedia(mediaId)
  subs.value = subs.value.filter(s => s.media_id !== mediaId)
}
</script>
```

- [ ] **Step 3: Create frontend/src/views/TaskMonitor.vue**

```vue
<template>
  <div class="layout">
    <el-header><!-- nav --></el-header>
    <el-main>
      <h2>任务监控</h2>
      <el-table :data="tasks" style="width:100%">
        <el-table-column prop="media_title" label="影视" />
        <el-table-column prop="episode_range" label="集数" />
        <el-table-column prop="status" label="状态">
          <template #default="{ row }">
            <el-tag :type="row.status === 'completed' ? 'success' : row.status === 'downloading' ? 'warning' : 'info'">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" />
      </el-table>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { taskAPI } from '../api'

const tasks = ref<any[]>([])

onMounted(async () => {
  const { data } = await taskAPI.list()
  tasks.value = data
})
</script>
```

- [ ] **Step 4: Create frontend/src/views/NotificationCenter.vue**

```vue
<template>
  <div class="layout">
    <el-header><!-- nav --></el-header>
    <el-main>
      <h2>通知中心</h2>
      <el-button @click="markAll">全部已读</el-button>
      <el-timeline>
        <el-timeline-item v-for="n in notifications" :key="n.id" :timestamp="n.created_at">
          <el-card :class="{ unread: !n.is_read }">
            <h4>{{ n.title }}</h4>
            <p>{{ n.body }}</p>
          </el-card>
        </el-timeline-item>
      </el-timeline>
    </el-main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { notificationAPI } from '../api'

const notifications = ref<any[]>([])

onMounted(async () => {
  const { data } = await notificationAPI.list()
  notifications.value = data
})

async function markAll() {
  await notificationAPI.markAllRead()
  notifications.value.forEach(n => n.is_read = true)
}
</script>
```

- [ ] **Step 5: Create frontend/src/stores/notification.ts**

```typescript
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { notificationAPI } from '../api'

export const useNotificationStore = defineStore('notification', () => {
  const unreadCount = ref(0)

  async function fetchUnreadCount() {
    try {
      const { data } = await notificationAPI.list(true)
      unreadCount.value = data.length
    } catch { /* ponytail: silent */ }
  }

  return { unreadCount, fetchUnreadCount }
})
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/views/MyList.vue frontend/src/views/AdminDashboard.vue frontend/src/views/TaskMonitor.vue frontend/src/views/NotificationCenter.vue frontend/src/stores/notification.ts
git commit -m "feat: remaining frontend pages — my list, admin, tasks, notifications"
```

---

## Phase 7: Integration & Polish

### Task 13: Schedule Configuration & Docker Polish

**Files:**
- Modify: `docker-compose.yml` (add cron-like scheduling for ARQ tasks)
- Modify: `backend/app/tasks/worker.py` (add cron jobs)

- [ ] **Step 1: Update worker.py with cron configuration**

Add to WorkerSettings:
```python
    cron_jobs = [
        # Daily full scan at 3 AM
        {"name": "Daily full scan", "coroutine": "app.tasks.scan.scan_all_media", "cron": "0 3 * * *"},
        # Weekly Quark cleanup at Sunday 4 AM  
        {"name": "Weekly Quark cleanup", "coroutine": "app.tasks.cleanup.cleanup_quark_files", "cron": "0 4 * * 0"},
    ]
```

- [ ] **Step 2: Verify docker-compose.yml works**

```bash
docker compose config  # validates YAML
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml backend/app/tasks/worker.py
git commit -m "feat: ARQ cron scheduling, Docker polish"
```

---

### Task 14: Git Remote & Initial Push

- [ ] **Step 1: Add remote and push**

```bash
git remote add origin https://github.com/zhangjinglei137/LumenCloud.git
git branch -M main
git add -A
git commit -m "feat: LumenCloud initial implementation — full media management system"
git push -u origin main
```

---

## Implementation Notes

1. **Placeholders:** Frontend pages (MyList, AdminDashboard, etc.) use simplified single-file components. Shared layout/nav should be extracted to a `Layout.vue` component — deferred to avoid touching every file twice.
2. **Media Detail API routing:** Currently `/api/media/{id}` expects the LumenCloud UUID, not TMDB ID. A `/api/media/by-tmdb/{tmdb_id}` lookup endpoint should be added for the frontend flow where search returns TMDB IDs.
3. **Error handling:** Services use basic `raise_for_status()`. Production needs retry logic and circuit breakers for external service failures.
4. **CloudSaver token caching:** Currently re-logins on every call if no cached token. Should use Redis to cache the token (as N8n did) — deferred optimization.
5. **Aria2 webhook:** Requires Aria2 configured with `--on-download-complete` pointing to `http://lumencloud-backend:8000/api/webhook/aria2`.


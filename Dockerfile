# ============================================
# LumenCloud 多阶段构建
# Stage 1: 构建 Vue3 前端
# Stage 2: Python 运行时 + FastAPI 单进程 + supervisord
# ============================================

# ---------- Stage 1: 前端构建 ----------
FROM node:20-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: Python 运行时 ----------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LUMENCLOUD_DATA_DIR=/app/data

WORKDIR /app

# 系统依赖（supervisord）
RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

# 后端依赖
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# 后端代码
COPY backend/app /app/backend/app

# Alembic 迁移脚本（app/database.py init_db 启动时执行 alembic upgrade head 必需）
COPY backend/alembic /app/backend/alembic

# 运维脚本（迁移/探测；阶段 4 用 docker exec 执行 migrate_from_n8n.py）
COPY scripts /app/scripts

# 前端构建产物 → FastAPI 静态直出
COPY --from=frontend-builder /build/frontend/dist /app/backend/static

# supervisord 配置
COPY supervisord.conf /etc/supervisor/conf.d/lumencloud.conf

# SQLite 数据目录（volume 挂载）
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/lumencloud.conf"]

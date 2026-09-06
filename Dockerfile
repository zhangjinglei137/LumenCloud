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

# 升级 pip 消除镜像内系统级 pip CVE（trivy 门禁要求 pip>=26.2.1）；
# setuptools/msgpack 一并升级到修复线：CVE-2025-47273（setuptools<78.1.1）、
# GHSA-6v7p-g79w-8964（msgpack<=1.2.0）。
# 注意：不要用 apt 安装 supervisor —— apt-get 会装入 Debian 版
# python3-setuptools（dist-packages 旧版）与 pip 新版并存，trivy 扫到旧
# metadata 仍命中 CVE-2025-47273（CI 实证）。故 supervisor 也走 pip 安装。
RUN pip install --no-cache-dir --upgrade "pip>=26.2.1" \
    && pip install --no-cache-dir --upgrade "setuptools>=78.1.1" "msgpack>=1.2.1" \
    && pip install --no-cache-dir "supervisor>=4.2.5" \
    && mkdir -p /etc/supervisor/conf.d \
    && pip --version

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LUMENCLOUD_DATA_DIR=/app/data

WORKDIR /app

# 后端依赖
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# 后端代码
COPY backend/app /app/backend/app

# Alembic 迁移脚本（app/database.py init_db 启动时执行 alembic upgrade head 必需）
COPY backend/alembic /app/backend/alembic

# 运维脚本（迁移/探测/备份/校验；阶段 4 用 docker exec 执行 migrate_from_n8n.py / backup_db.py）
# scripts/backup 目录不预建：备份脚本运行时 mkdir（root 用户 /app 下可写），
# 与 migrate_from_n8n.py 的 export_backup 一致，避免空目录被 COPY 进镜像
COPY scripts /app/scripts

# 前端构建产物 → FastAPI 静态直出
# ⚠️ vite.config.ts 的 build.outDir = '../backend/static'（相对 /build/frontend）
#    = /build/backend/static；不要改成 dist，否则多阶段 COPY not found
COPY --from=frontend-builder /build/backend/static /app/backend/static

# supervisord 配置
COPY supervisord.conf /etc/supervisor/conf.d/lumencloud.conf

# SQLite 数据目录（volume 挂载）
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/lumencloud.conf"]

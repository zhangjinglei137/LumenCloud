# syntax=docker/dockerfile:1.7

# ========== 阶段 1: 构建前端 ==========
FROM node:20-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ========== 阶段 2: 最终镜像 ==========
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl supervisor \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt

# 后端代码
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./

# 前端构建产物
COPY --from=frontend-build /frontend/dist ./static

# supervisord 配置
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1
CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]

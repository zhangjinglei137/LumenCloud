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
#
# ⚠️ 关键（CI 多轮实证后定稿）：
#   1. python:3.12-slim（Debian trixie）的系统 python 包链自带 dist-packages
#      旧版 setuptools-70.3.0 / msgpack-1.1.2（Debian 打包，版本化路径）。
#      pip 只写 site-packages 新版，不删 dist-packages 旧包；
#   2. 单纯 rm -rf 文件不够——Debian 包经 apt 安装会在 /var/lib/dpkg/status
#      留有记录，trivy 对 Debian 镜像从 dpkg 数据库读包版本，rm 文件不更新
#      dpkg 记录 → 仍报旧版本（CI 实证：文件系统实测仅 84.0.0 / 1.2.2，trivy
#      仍报 70.3.0 / 1.1.2）。所以必须 apt purge 从 dpkg 移除这些系统包；
#   3. 同时不要用 apt 安装 supervisor：apt-get 会装入 Debian 版
#      python3-setuptools（与上述同源）；supervisor 走 pip 安装。
#
# 顺序：pip 升级（site-packages 新版本）→ apt purge 系统旧包（dpkg 记录移除）
# → 兜底清理 dist-packages 残留目录。site-packages 已由 pip 提供同功能新版，
# 运行时无影响；purge 只移除系统 python 的 Debian 打包版本。
#
# ⚠️ purge 前必须 apt-get update：基础镜像层含 apt 源列表，但 RUN 层无缓存；
# 无 update 时 purge 报 'Unable to locate package' 被 || true 吞掉而静默失败
# （CI 实证：曾因吞输出未发现 purge 未执行，trivy 仍报 Debian 70.3.0/1.1.2）。
RUN apt-get update -qq \
    && pip install --no-cache-dir --upgrade "pip>=26.2.1" \
    && pip install --no-cache-dir --upgrade "setuptools>=78.1.1" "msgpack>=1.2.1" \
    && pip install --no-cache-dir "supervisor>=4.2.5" \
    && apt-get purge -y python3-setuptools python3-msgpack \
    && rm -rf /usr/lib/python*/dist-packages/setuptools* \
              /usr/lib/python*/dist-packages/pkg_resources* \
              /usr/lib/python*/dist-packages/_distutils_hack* \
              /usr/lib/python*/dist-packages/msgpack* \
    && mkdir -p /etc/supervisor/conf.d \
    && python -c "import setuptools, msgpack; print('setuptools', setuptools.__version__); print('msgpack', msgpack.version)" \
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

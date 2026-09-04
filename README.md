<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-green" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3.5-brightgreen" alt="Vue">
  <img src="https://img.shields.io/badge/单容器-Docker_Ready-2496ED" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="MIT">
</p>

# 拾光云映 LumenCloud

智能影视下载管理系统 —— 替代 n8n 流程体系，单容器部署。

从 TMDB 搜索订阅 → Emby 缺集比对 → CloudSaver 网盘搜源 → 容量感知队列转存 → Aria2 下载 → NasTools 入库整理，全流程自动化。

## 项目状态

> ⏳ **骨架阶段**：本仓库为全新起点（已清空旧实现）。当前包含文档、CI/CD 与最小可构建骨架，
> 业务功能（容量队列、审批流、鉴权、巡检调度等）按设计文档逐步实施中。

## 文档

- [📋 现状流程梳理](docs/现状流程梳理.md) —— 原 n8n 6 个流程的拓扑、数据流与 21 个已知问题
- [🏗️ 新系统设计](docs/新系统设计.md) —— 架构、数据模型、调度队列、容量策略、审批鉴权、部署迁移完整设计

## 快速开始（开发）

```bash
cp .env.example .env          # 填入密钥与外部服务地址
docker compose up -d          # 或本地: uvicorn + vite dev
访问 http://localhost:8000
```

## 生产部署

GitHub Actions 在 push `v*.*.*` tag 时自动构建多架构镜像（amd64/arm64）并推送到 Docker Hub：

```bash
# 1. 配置 Secrets：DOCKERHUB_USERNAME / DOCKERHUB_TOKEN
# 2. 打 tag 触发构建
git tag v1.0.0 && git push origin v1.0.0
# 3. 服务器上部署
IMAGE_TAG=1.0.0 docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

## 外部服务依赖

| 服务 | 用途 |
|------|------|
| TMDB | 影视元数据 / 搜索 |
| Emby | 已有集比对（防重基线）、入库确认 |
| CloudSaver | 夸克网盘搜索、分享码解析、转存 |
| AList | `/quark` 挂载、直链、空间释放 |
| Aria2 | 下载引擎 |
| NasTools | 目录同步入库（重启 + 冷却） |
| PushPlus（可选）| 微信通知 |

## License

MIT
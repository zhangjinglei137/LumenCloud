<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-green" alt="FastAPI">
  <img src="https://img.shields.io/badge/Vue-3.5-brightgreen" alt="Vue">
  <img src="https://img.shields.io/badge/Docker_Compose-Ready-2496ED" alt="Docker">
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="MIT">
</p>

# 拾光云映 LumenCloud

智能影视下载管理系统 —— 替代 n8n 流程体系，Docker Compose 部署。

从 TMDB 搜索订阅 → Emby 防重基线比对 → CloudSaver 网盘搜源 → 容量感知队列转存 → Aria2 下载 → NasTools 入库整理，全流程自动化。

## 项目状态

> ✅ **已实现完整业务链路**：订阅（TMDB 搜索添加 / 访客「想看」审批）→ 搜源（CloudSaver）→
> 容量感知转存（夸克队列，模型 B 硬上限）→ Aria2 下载 → NasTools 目录同步入库，
> 全流程自动化；并配套鉴权（admin/guest + 邀请码注册）、影视库与 Emby 展示、
> 转存队列与容量监控、审批、运行日志、设置等完整页面。
>
> **Phase 8 配置入库**：服务凭据（alist/cloudSaver/aria2/NasTools/Emby/TMDB/PushPlus/夸克）
> 可在设置页配置保存到数据库，保存即生效、无需重启（敏感键 GET 以 `***` 遮蔽，env 仅作 fallback）；
> JWT 密钥首启自动生成落盘 `./data/.jwt_secret`，admin 初始密码首启随机生成并打印在服务日志。

## 文档

- [📋 现状流程梳理](docs/现状流程梳理.md) —— 原 n8n 6 个流程的拓扑、数据流与 21 个已知问题
- [🏗️ 新系统设计](docs/新系统设计.md) —— 架构、数据模型、调度队列、容量策略、审批鉴权、部署迁移完整设计
- [🛠️ 运维手册](docs/运维手册.md) —— 部署、初始密码获取、凭据配置、备份恢复、迁移与故障排查

## 快速开始（开发）

**零 .env 开箱即用**（服务凭据在 Web 设置页配置，无需准备任何环境变量）：

```bash
docker compose up -d --build        # 内置 db（PostgreSQL 16）+ lumencloud，alembic 启动自动迁移建表
访问 http://localhost:8000
# 首次启动的 admin 初始密码（随机生成，唯一渠道在服务日志）：
docker compose logs lumencloud | grep 初始密码
```

登录后先到 **设置页** 配置各服务凭据（保存即生效，无需重启），首次登录建议立即修改 admin 密码。

## 生产部署

GitHub Actions 在 push `v*.*.*` tag 时自动构建多架构镜像（amd64/arm64）并推送到 Docker Hub
（需配置 Secrets：`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`）：

```bash
# 1. 打 tag 触发镜像构建与 GitHub Release
git tag v1.0.0 && git push origin v1.0.0
# 2. 服务器上部署（零 env：无需 .env.prod；IMAGE_TAG 缺省用 latest）
IMAGE_TAG=1.0.0 docker compose -f docker-compose.prod.yml up -d
# 3. 获取 admin 初始密码
docker compose -f docker-compose.prod.yml logs lumencloud | grep 初始密码
```

部署即用，无需任何手工初始化：内置 PostgreSQL 16（`db` 服务，数据在 `./pgdata` 卷）、
alembic 迁移随启动自动执行、JWT 密钥首启自动生成落盘 `./data/.jwt_secret`（chmod 600）、
admin 初始密码首启随机生成（见上）。凭据与运行参数全部在 **设置页** 配置（入库即生效），
备份参考 `scripts/backup_db.py`（详见 [运维手册](docs/运维手册.md)）。

## 外部服务依赖

| 服务 | 用途 |
|------|------|
| TMDB | 影视元数据 / 搜索（订阅与审批候选） |
| Emby | 已有集比对（防重基线）、入库二次确认 |
| CloudSaver | 夸克网盘搜索、分享码解析、转存（**无容量接口**，Q1 结论） |
| AList | `/quark` 挂载、**容量统计**、直链、空间释放 |
| Aria2 | 下载引擎（GID 来源校验：转存前发现陌生任务即暂停并告警） |
| NasTools | 目录同步入库（重启 + 冷却） |
| PushPlus（可选）| 微信通知 |

## License

MIT
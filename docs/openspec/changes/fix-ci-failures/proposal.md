# fix-ci-failures

## 问题描述

GitHub Actions `ci` workflow 连续 5 次失败（09-11 起），三个 job 中 `Backend 导入检查` 与 `镜像安全扫描（trivy）` 失败：

1. **pytest 失败（2 个用例）**
   - `tests/test_static_path_traversal.py::test_spa_fallback_serves_normal_static_files`（GET /normal.txt 期望 200，实际 404 `{"detail":"Not Found"}`）
   - `tests/test_static_path_traversal.py::test_spa_fallback_serves_index_for_unknown_path`（GET /some/unknown/route 期望 200 SPA-INDEX，实际 404）

2. **Trivy 镜像扫描 exit 1**
   - `.trivyignore` 已豁免 3 个 Python SBOM 误报，但扫描仍失败，存在未豁免的 HIGH/CRITICAL 漏洞。

## 根因分析

### 根因 1：SPA fallback 路由在 CI 环境未注册（环境差异 bug）

- D7 安全修复 `538d7d2` 将 `serve_spa` 路由注册放进 `if STATIC_DIR.exists():` 门控内（`backend/app/main.py:151`）。
- `backend/static/` 是前端构建产物，被 `.gitignore:49` 排除，CI checkout 后**不存在** → 门控为 False → `serve_spa` 与 `/assets` 挂载全部未注册。
- 于是 `/normal.txt`、`/some/unknown/route` 落入 FastAPI 默认 404 `{"detail":"Not Found"}`。
- 穿越测试 `test_spa_fallback_cannot_read_outside_static_root` 断言 `r.status_code in (200, 404)` 皆可，404 也算通过 → **假绿**，掩盖了路由未注册的事实。
- 本地开发机因前端构建产物存在而测试通过，CI 从干净 checkout 失败 → 环境差异。

### 根因 2：Docker 基础镜像系统包存在未修复漏洞

- `python:3.12-slim` 基础镜像（Debian 13.6/trixie）自带系统包 gzip / libpcre2-8-0 / libsqlite3-0 / perl-base 等。
- Dockerfile 仅升级了 pip / setuptools / msgpack，**从未 `apt-get upgrade` 系统包**。
- 本地 trivy 复现（v0.70.0 扫描同源镜像）确认 12 个 Debian 系统包漏洞（9 HIGH / 3 CRITICAL）：
  - gzip CVE-2026-41992
  - libpcre2-8-0 CVE-2026-86145 / CVE-2026-89161
  - libsqlite3-0 CVE-2026-11822 / CVE-2026-11824
  - perl-base CVE-2026-13221(CRITICAL) / CVE-2026-42496(CRITICAL) / CVE-2026-8376(CRITICAL) / CVE-2026-42497 / CVE-2026-48962 / CVE-2026-57432 / CVE-2026-57433
- 这些是真实系统包漏洞（非 SBOM 误报），`.trivyignore` 的 3 项豁免（setuptools/msgpack 误报）覆盖不到，`exit-code: "1"` 必然失败。

## 修复目标

1. `serve_spa` SPA fallback 路由在无静态产物（CI 干净 checkout）环境下仍注册，行为一致：static 根内存在的文件直出、未知路径 fallback index.html、`/api` 未知路径返回 404 JSON、路径穿越不得越界读文件。
2. Docker 镜像构建时 `apt-get upgrade` 系统包到修复版本，消除 12 个 Debian 系统包 HIGH/CRITICAL 漏洞，Trivy 扫描通过。
3. CI 恢复全绿：pytest 全部通过、Trivy exit 0、前端构建通过。

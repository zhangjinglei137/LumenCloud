# Verification Report: media-poster-proxy

- Date: 2026-09-09
- 验证模式：full（9 任务、1 delta spec capability、44 变更文件 > 8 阈值）
- Plan base-ref：11d5051725a57ab858eee276c86c22d92e249c34
- 最终 HEAD：47ead1f（含集成审查修复）
- 产物语言：zh-CN

## 验证证据（独立于 build 阶段记录）

| 命令 | 结果 |
|------|------|
| `backend: .venv/bin/python -m pytest tests/ -q` | **445 passed**, 2 warnings（anyio DeprecationWarning，pre-existing 非本 change 引入） |
| `frontend: npm test` | vitest 4/4 passed |
| `frontend: npm run build`（vue-tsc --noEmit + vite build） | 成功，产物写入 backend/static（chunk >500kB 为既有体积警告） |
| `comet state record-check <name> verify` | [RECORDED] exit=0 |

## 检查项（comet-verify full 模式）

### 1. tasks.md 全部任务已完成
9/9 全部 `[x]`（1.1、1.2、2.1、2.2、3.1、3.2、3.3、4.1、4.2）；`comet state task-checkoff` 逐项 PASS。

### 2. 实现符合 `<change-dir>/design.md` 高层设计决策
| 决策 | 实现 | 状态 |
|------|------|------|
| D1 代理端点 `GET /api/poster?p=`：/t/p/ 前缀校验、无 ../ 无协议段、非 400 不发请求、httpx 回源 + Content-Type + Cache-Control max-age=86400、失败 502 + 节流告警 | `backend/app/routers/poster.py` + `services/poster.py` | ✅ |
| D2 镜像配置 `tmdb_poster_proxy`：system_config 优先、env 兜底、保存即生效、空则回退官方 | `config.py` TMDB_POSTER_PROXY + `settings.py` 白名单 + `config_store.get` 函数内读取 | ✅ |
| D3 前端统一改造：`posterUrl(path)` → `/api/poster?p=...`，替换 6 处使用点，poster_path 契约不变 | `utils/poster.ts` + 5 视图/组件替换（EmbyLibraryView 经 TmdbSearch 覆盖） | ✅ |
| D4 缓存：浏览器 HTTP 缓存头 + 进程内 TTL dict（上限+过期） | `Cache-Control: public, max-age=86400` + `_POSTER_CACHE`（TTL 600s、上限 100、超限不写） | ✅ |

### 3. 实现符合 Design Doc（docs/superpowers/specs/2026-09-09-media-poster-proxy-design.md）
- §3.1 端点契约 400/401/502/503 + Cache-Control ✅（test_poster_auth/proxy 覆盖）
- §3.2 路径校验规则（normpath 归一化、拒绝 ://、\、null、双重编码）✅
- §3.3 镜像配置注册点（config.py/settings.py/settingsMeta.ts）✅（settingsMeta 在集成审查后补齐）
- §3.4 鉴权 get_current_user（cookie 兜底）✅
- §3.5 回源与节流告警 ✅
- §3.6 进程内 TTL 缓存（key=path、TTL 600、上限 100、失败不写）✅
- §3.7 前端 posterUrl + 替换点清单 + vitest 单测 ✅
- **实现偏差（已修复并闭合）**：落库 poster_path 为 TMDB 原始格式 `/ab12cd.jpg`（无 size 段），与校验 `/t/p/` 前缀契约不符——集成审查发现为 Critical（真实海报全 400），已在 `posterUrl` 注入 `/t/p/w500` size 段（带幂等前缀判断）修复并经 scoped re-review 闭合。测试契约同步改为真实格式。

### 4. 能力规格场景全部通过
| 场景 | 覆盖 | 状态 |
|------|------|------|
| 合法海报路径代理返回（正确 Content-Type + 缓存头） | test_poster_proxy.py 回源成功用例 | ✅ |
| 非法路径被拒绝（4xx，不发起对外请求） | test_poster_path_validation.py 18 例 + 路由 400 用例 | ✅ |
| 代理端点要求登录态（未登录 401） | test_poster_auth.py（RED 证明 401 来自鉴权依赖） | ✅ |
| 已配置镜像（从镜像拉取） | test_base_url_prefers_mirror + test_mirror_path_requests | ✅ |
| 未配置镜像（回退官方） | test_base_url_falls_back_to_official | ✅ |
| 影视库海报经代理显示 | posterUrl（真实格式注入 size 段后校验通过、回源 URL 正确）+ 视图替换 | ✅ |
| 代理不可达优雅降级（标题占位不破页） | 前端 imgErrors/posterBroken/posterErrors 兜底保留（Task 5 核对） | ✅ |

### 5. proposal.md 目标已满足
- 后端代理端点 ✅ / 前端地址改走代理 ✅ / 可配置镜像 ✅ / 优雅降级 ✅

### 6. delta spec 与 design doc 无矛盾
- delta spec 补入的「代理端点要求登录态」场景与 Design Doc §3.4 一致；posterUrl 注入 size 段属实现细节修正，spec 的「TMDB 图床相对路径」语义不变，无矛盾。

### 7. Design Doc 可定位
`docs/superpowers/specs/2026-09-09-media-poster-proxy-design.md` 存在且含 frontmatter（comet_change / role: technical-design / canonical_spec: openspec）。

## 集成代码审查（review_mode: standard → Verify 唯一最终集成审查）

- ora-9 集成审查：发现 **Critical #1（poster_path 契约错配，真实海报全 400）+ Important #2（settingsMeta 漏补条目）** + Minor #3-6。
- Critical #1 经 DB 查询独立核实（9 条 poster_path 全部为 `/xxxx.jpg` 无 size 段）。
- 修复（fix-8 / 47ead1f）：`posterUrl` 注入 `/t/p/w500` size 段 + 测试契约改真实格式 + settingsMeta 补条目。
- scoped re-review（ora-10）：**All findings addressed, no new Critical/Important breakage** → 集成审查 PASS。

## 保留的 Minor 项（可接受，不阻塞归档）

| # | 项 | 理由 |
|---|-----|------|
| 1 | 502 detail 透传内部异常字符串 | 已登录用户上下文风险有限；保留便于排查 |
| 2 | 缓存 100 条满后不回收过期条目 | 设计取舍（LRU 简化为直拒）；命中率下降时可加惰性淘汰 |
| 3 | `_fetch_calls_with_factory` 死代码辅助函数 | 按 brief 保留，删除无害 |
| 4 | `p` 含 `#`/`?` 时校验通过但上游解析截断 | 非安全问题（失败即降级 502） |
| 5 | `_alert` 清理分支（>200 键）无测试 | 防御性清理，非功能路径 |
| 6 | `_POSTER_CACHE_MAX * 2` 阈值语义耦合 | 可读性小瑕疵 |
| 7 | test_poster_auth.py env 命名差异与覆盖面窄 | 401 早返不触达外部服务，不影响结论 |

## 结论

**All checks passed. Ready for archive.** 无 CRITICAL/IMPORTANT 未解决项；保留 Minor 项均不影响正确性、安全与边界条件。

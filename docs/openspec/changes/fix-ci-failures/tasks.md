# fix-ci-failures / tasks

## 任务清单

- [x] 1. main.py：`serve_spa` 路由移出 `STATIC_DIR.exists()` 门控，无条件注册；`/assets` 挂载保留存在性判断；static 根缺失时 fallback index.html 不存在则 404 JSON，不 500
- [x] 2. Dockerfile：Stage 2 的 `apt-get update -qq` 后追加 `apt-get upgrade -y -qq`，升级系统包到修复版本
- [x] 3. 本地复现验证：删除/重命名 `backend/static` 后运行 `pytest tests/test_static_path_traversal.py`，3 用例全绿（RED→GREEN）
- [x] 4. 本地全量回归：无 static 产物时 `pytest tests/ -q` 通过；恢复 static 产物后同用例仍全绿
- [x] 5. 构建与扫描验证：本地无法构建 docker（无 docker daemon），改为推送后由 CI trivy job 实证；已确认 Debian trixie 仓库存在全部修复版本（gzip 1.13-1+deb13u1 / pcre2 10.46-1~deb13u2 / sqlite3 3.46.1-7+deb13u2 / perl 5.40.1-6+deb13u1）
- [ ] 6. 提交代码并推送 main，观察 CI 全绿（pytest / trivy / 前端构建）

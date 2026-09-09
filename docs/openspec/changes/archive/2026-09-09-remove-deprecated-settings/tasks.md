## 1. 前端移除废弃项

- [x] 1.1 从 settingsMeta.ts 删除 download_queue_max_concurrent 条目；验证前端构建通过且设置页不再展示该条目

## 2. 后端清理引用

- [x] 2.1 审计并移除 config_store 与 settings router 中对 download_queue_max_concurrent 的读取/校验/透传；验证 GET /api/settings 响应不再包含该键
- [x] 2.2 grep 全仓验证 download_queue_max_concurrent 无残留代码引用（测试同步清理）；验证 pytest 通过

## 3. 文档清理

- [x] 3.1 检索并清理 docs/ 与 README 中「下载队列最大并发数 / download_queue_max_concurrent」提及；验证文档检索无残留

## 4. 存量数据与收尾

- [x] 4.1 确认 system_config 存量键值保留不被删除（无迁移）；验证存储中键值不受影响
- [x] 4.2 全量后端测试 + 前端构建通过
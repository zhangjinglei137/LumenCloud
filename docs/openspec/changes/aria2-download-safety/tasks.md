## 1. GID 来源校验放行（pipeline-admission）

- [x] 1.1 移除 `_admit_batch` 段 2 的陌生 gid 校验逻辑（tell_active/tell_waiting 来源校验、`_unknown_gid_strikes` 计数、flow_error 告警、best-effort `aria2.remove` 强删）并清理 `_GID_STRIKE_LIMIT` 常量；验证：转存准入路径不再出现「检测到非本系统 aria2 任务」分支，`pytest backend/tests/test_transfer.py` 通过且无引用残留
- [x] 1.2 更新受影响的既有测试与断言（test_transfer.py / test_capacity.py 中针对陌生 gid 拦截、告警、强删的用例改为验证放行共存语义）；验证：全量 `pytest backend/tests/ -q` 通过
- [x] 1.3 确认阶段 A 对本系统已签发 gid（download_queue.aria2_gid 非空行）的轮询/进度/完成推进/recovery 清理不受影响；验证：`_poll_downloading_tasks` 与 `trigger_download_complete` 相关测试全部通过

## 2. 空间不足提示来源排查与口径修复（notifications）

- [x] 2.1 证据排查：核对 quark_capacity_log 快照（总量/已用/使用率）、`check_capacity_alert` 触发门槛、`_record_alert(category="capacity")` 各触发点（「容量不足已累计 N 次」「持续超过 24 小时」）、前端「等待容量」文案的展示条件与 `quota_wait` 产生路径；验证：产出排查结论（是否真实存在误报路径，定位到具体函数与触发条件）
- [x] 2.2 若发现容量充足时误触发空间不足通知的路径，修复触发条件或数据源，确保与真实容量一致（spec：容量充足 MUST NOT 产生空间不足通知）；验证：对应单元测试覆盖「使用率低于阈值不告警」场景（test_capacity_alert.py / test_capacity.py），并人工核对通知表不再新增容量类误报
- [x] 2.3 复核前端「等待容量」文案（QueueView.vue quotaWaitText）在无 quota_wait 任务时不展示，quota_hint 数据来源与容量一致性；验证：前端构建通过，容量条/排队原因文案逻辑与后端契约一致

## 3. 清理保护 aria2 下载源（quark-cleanup-safety）

- [x] 3.1 扩展 `Aria2Client.tell_active` / `tell_waiting` 返回 keys 增加 `"files"`，新增 `list_source_basenames()`：组合 tell_active+tell_waiting，从 `files[].uris[].uri` 解析 URL path 末段（URL-decoded basename）作为下载源文件名集合；验证：新增单元测试（mock aria2 响应，断言 uri 解析与 basename 提取正确，含 query string 与路径边界）
- [x] 3.2 `release_space_cleanup_job` 在孤儿判定（present − referenced）之后、删除之前计算 aria2 下载源保护集并求差（orphans = present − referenced − aria2_sources）；验证：新增测试覆盖「下载中文件被保护」「等待下载文件被保护」「无下载任务正常清理」
- [x] 3.3 aria2 查询失败（Aria2Unavailable）时清理 fail-safe：本轮不删除任何孤儿、`record_task_run(cleanup, error)` 并记录告警；验证：新增测试断言查询异常时 `alist.remove` 不被调用、task_run 记录 error

## 4. 收尾验证

- [ ] 4.1 运行后端全量测试套件（`pytest backend/tests/ -q`）并确认新增/修改用例全部通过，无回归
- [ ] 4.2 复核三个 delta spec 的验收场景均被测试覆盖（pipeline-admission 放行共存、notifications 容量一致、quark-cleanup-safety 保护与 fail-safe）；验证：测试清单与 spec 场景逐条对应

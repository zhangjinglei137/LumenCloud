# Tasks: queue-inspection-rework

## 1. 后端：队列列表排序/字段/分享码

- [x] 1.1 `_list_flat` 排序改为 `created_at ASC, id ASC`（创建时间从小到大），保留 limit/offset，验证排序断言（构造多行不同 created_at）
- [x] 1.2 `_list_download` 排序改为 `enqueued_at ASC, id ASC`，验证排序断言
- [x] 1.3 巡检队列扁平行补 `media_title`（join media.title）与明文 `share_code`（仅 admin；guest 不返回，沿用 §9.1 脱敏），验证 admin/guest 返回差异
- [x] 1.4 下载队列输出补 `share_url`（夸克分享地址，由 share_code 构造，域名集中常量），无 share_code 返回 null，验证 URL 构造正确
- [x] 1.5 补充后端测试：排序/分页/脱敏/分享 URL 用例，验证 `pytest` 通过
- [x] 1.6 更新既有测试的旧契约断言（`test_queue.py` 的 10 字段定界/裸数组遍历、`test_scan_run_phases.py`、`test_api_smoke.py` 的 list_queue 调用处与 `_SENSITIVE_QUEUE_FIELDS` 拆分）：契约已变 `{items,total}` + share_code 明文，同步断言，验证 `pytest` 全量通过

## 2. 后端：大小真实值调查与修复

- [x] 2.1 调查「队列每行大小都一样」根因（核查 task_queue/download_queue 建行与转存后 file_size 是否回填真实值、`_list_flat`/`_list_download` 输出链路），记录根因结论
- [x] 2.2 按根因修复：队列行展示来自本行的真实 file_size（若为数据回填缺失则在对应流程回填），验证多任务大小各异时逐行真实

## 3. 后端：入库确认卡死调查与修复

- [ ] 3.1 调查「status=library 迟迟不入库」根因（find_emby_id 未命中/遗漏集盲匹配/超时配置/scheduler 注册缺失，逐项核查日志与代码），记录根因结论
- [ ] 3.2 修复：library_check 的 continue 分支补充明确日志（记录不 finalize 的具体原因），并让超时判定覆盖「Emby 命中但遗漏集判定」路径（避免无限等待），验证卡死可诊断/可超时
- [ ] 3.3 补充测试：遗漏集判定终态、超时路径用例，验证 `pytest` 通过

## 4. 前端：巡检队列改名与展示

- [ ] 4.1 QueueView 原「任务队列」Tab 改名为「巡检队列」，验证页面 Tab 命名更新
- [ ] 4.2 巡检队列行展示 影视名称 / SxxExx / 分享码明文（admin）/ 状态 / 真实大小 / 更新时间，验证字段齐全
- [ ] 4.3 下载队列分享码明文展示且可点击跳转 share_url（无地址则不可点击），验证点击行为
- [ ] 4.4 巡检/下载队列改 el-pagination 标准分页（page/pageSize/total），移除 loadMore「展开更多」交互，默认加载第一页全量展示，验证翻页正常且无展开更多按钮
- [ ] 4.5 stores/queue.ts 与 api/types 契约同步（total/page 字段、share_url、media_title），验证类型一致
- [ ] 4.6 前端测试补充：队列行字段渲染、分享码链接、分页数据流用例，验证 `vitest` 通过

## 5. 验证

- [ ] 5.1 手动验证：巡检队列命名/字段/正序分页、下载队列分享码跳转、无展开更多、入库确认不再长期卡死，验证 `npm run build` 与后端启动无报错

# Tasks

## 1. 归因字段与结论文案

- [x] 1.1 在 `_scan_one` 的各结束分支写入 `scan_detail.skip_reason` / `gated_out` / `missing_aired_total`，覆盖 no_missing、not_aired、gated_out、no_candidates、skip_enqueue 五类；验证：新增单测对每类分支断言 `scan_detail` 键值正确 <!-- comet-task:d01d3009-f64c-4279-80bd-d21307541f96 -->
- [x] 1.2 重写 `_result_message`，按 `skip_reason` 生成互不混淆的中文结论，并去掉「已有任务跳过」对「因下载中未入队」的误述；验证：单测覆盖五种 skip_reason 的文案互不相同且含缺失集数 <!-- comet-task:a765b577-70c8-4b28-916d-7d121d0dea83 -->
- [x] 1.3 保留 `status=skipped` 语义收窄为「无需进一步动作」，确认运行日志筛选与前端映射不因新增键报错；验证：`pytest backend/tests` 中 scan 与 logs 相关用例通过 <!-- comet-task:3c9d97cd-4b8b-40a0-8409-1a0bb74f1079 -->

## 2. 巡检告警

- [x] 2.1 巡检以 `error` 结束时调用 notifier 发 `flow_error`（含影视名、失败阶段、原因）；验证：单测断言 notifier 被调用一次且正文含影视名 <!-- comet-task:dcd28dff-274c-4067-9873-bd28a3a43c08 -->
- [x] 2.2 实现「连续 N 轮 no_candidates 且存在已播缺集」告警，N 走 `system_config`（缺省 3），冷却键为 `media_id+reason`，缺失集数量下降时清除；验证：单测覆盖连续 3 轮触发一次、第 4 轮不重复、数量下降后重新可触发 <!-- comet-task:0bcfcaa8-95f0-420b-9a90-38c07cb6b8a5 -->
- [x] 2.3 确认 PushPlus 未配置/失败时按既有降级规则写站内通知；验证：notifier 既有用例通过，并补一条巡检告警的降级断言 <!-- comet-task:123407d7-87d0-48a7-a387-32ed3eac2db0 -->

## 3. 运行记录回收

- [x] 3.1 `recover_stale_tasks` 增加 `task_run` 超时回收分支（默认 30 分钟，可配置），标注超时回收原因；验证：单测构造超期 running 记录，断言被置为 error 且 message 含回收标记 <!-- comet-task:dd6ffcc3-fb93-4cef-9dde-515fe7c65394 -->
- [x] 3.2 `_scan_one` 外层补 try/finally，异常时收敛当前 `task_run` 为 error；验证：单测注入异常后断言无 running 残留 <!-- comet-task:caeef910-38c5-4b2b-af11-46126e34dcc6 -->

## 4. 手动触发留痕

- [x] 4.1 `trigger_scan_background` 受理时同步创建 `task_run(running, trigger_source=manual)` 并返回记录 id；`POST /api/media/{id}/scan` 返回该 id 与受理结果；验证：接口测试断言调用后存在 manual 记录 <!-- comet-task:1bd4e282-cb94-4963-ae14-8b8b276478f9 -->
- [x] 4.2 队列 retry 路径同样记录 `trigger_source=manual`；验证：接口测试断言 retry 后新记录来源为 manual <!-- comet-task:2e769dd1-76fc-40e9-b64c-abc09b930760 -->
- [x] 4.3 后台任务异常时将该 manual 记录收敛为 error 并保留原因；验证：单测注入异常，断言记录状态为 error 且非 running <!-- comet-task:7cf3960d-ce24-4618-8841-f66da539735c -->
- [x] 4.4 前端消费受理结果：`scanMediaApi` 类型补 `accepted` / `run_id`，`stores/media.ts` 的 `scan` 返回受理结果，列表页与详情页按 `accepted` 分支提示成功或失败；验证：前端用例断言 `accepted=false` 时不出现成功提示 <!-- comet-task:0525e1b8-d8fe-4a01-b49e-3ed722a97d98 -->

## 5. 集成验证

- [x] 5.1 用「有已播缺集 + 搜索无候选」的构造场景跑一轮完整巡检，确认 `skip_reason=no_candidates`、告警产生一次、运行日志可读；验证：集成测试断言三者同时成立 <!-- comet-task:bd079cc9-8a0a-49ed-9b18-ffd07dbe3288 -->
- [x] 5.2 回归 `backend/tests` 全量通过，确认既有 scan / notifier / recovery 用例未因文案与字段变更失败；验证：`pytest backend/tests` 全绿 <!-- comet-task:53cadb8b-8294-4e71-beb2-9dd479d729c5 -->

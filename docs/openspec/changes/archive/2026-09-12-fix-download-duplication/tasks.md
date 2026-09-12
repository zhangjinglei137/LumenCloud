# Tasks: 全量模式与标准模式跨键防重对齐

## 任务清单

- [x] Task 1: 修改 `backend/app/tasks/scan.py` 的 `_enqueue` 幂等检查，
      新增「同 media 同 file_name」跨键防重（同一物理文件无论以文件名键
      还是 SxxExx 键入队，第二次一律 existing，杜绝重复入队）
- [x] Task 2: 新增单元测试 `test_scan_enqueue_dedup.py` 覆盖跨键防重
      （正向/反向同文件场景、不同文件不受影响、不同 media 不受影响）
- [x] Task 3: 运行 scan 相关测试与全量后端测试，确认无回归

## 生产数据人工清理（发布后执行）

- 对生产库 `download_queue` 中「渗透」10 集文件名键重复记录（id 898/901-908 批次，
  episode 为文件名格式）执行去重：保留 SxxExx 键记录，删除文件名键记录；
  同步清理对应 `task_queue` 记录与在途 aria2 下载
- 由运维/管理员在发布后按实际情况执行，不在代码提交范围

## 验证命令

```bash
cd backend && python -m pytest tests/ -x -q
```

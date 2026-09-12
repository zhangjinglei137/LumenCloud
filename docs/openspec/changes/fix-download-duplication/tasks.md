# Tasks: 全量模式 episode 键归一化对齐

## 任务清单

- [x] Task 1: 修改 `backend/app/tasks/scan.py` 全量模式 episode 键生成，
      文件名含「第N集/第N话」时归一化为 SxxExx 集号键（缺失集同季时），
      无法提取集号仍回退文件名键
- [x] Task 2: 新增/更新单元测试覆盖全量模式键生成三种情形
      （SxxExx 保持、第N集归一化、无集号回退文件名）
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

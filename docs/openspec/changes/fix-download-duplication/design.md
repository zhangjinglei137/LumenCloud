# Design: 全量模式 episode 键归一化对齐

## 修复方案

修改 `backend/app/tasks/scan.py` 全量模式（`tv_full_mode` 分支，scan.py:1765-1770）
的 episode 键生成逻辑，与标准模式 `match_missing` 的三重匹配对齐：

当前逻辑（tv 未收录全量模式）：
```
文件名含 SxxExx → _fmt_episode 归一化键
否则            → matched_key = file_name（回退文件名键）
```

修改后逻辑：
```
文件名含 SxxExx       → _fmt_episode 归一化键（不变）
文件名含 第N集/第N话   → 归一化为集号键
    - 季号从文件名 SxxExx 提取（已在上一步处理）
    - 纯「第N集」无季号 → 复用 match_missing 的跨季匹配语义：当该影视缺失集全部
      属于同一季时，取该季号组合出 SxxExx 键；多季缺失时回退文件名键
否则                  → matched_key = file_name（回退文件名键，兜底防丢集）
```

关键点：

1. **键对齐**：全量模式「第N集」归一化与 `match_missing` 的规则 2 保持一致——
   用 `_RE_CN_EP` 提取集号，缺失集同季时组合 `_fmt_episode(season, ep)`。
2. **复用现有工具**：`_RE_CN_EP`、`_fmt_episode`、`_ep_num`、`_season_of_key`
   均已在 scan.py 定义，无需新增。
3. **行为回退**：无法提取集号（无 SxxExx 也无「第N集」，如纯孤立数字、无集号命名）
   时仍回退文件名键，保留「防丢集」语义（P9 原权衡）。
4. **不改变标准模式**：`match_missing` 与 `_enqueue` 幂等逻辑（先查同键、撞
   UNIQUE 判并发）不动。

## 影响范围

- 仅修改 `backend/app/tasks/scan.py` 一个函数分支（约 10 行）。
- 不涉及接口变更、schema 变更、新 capability。
- 已存在的重复数据（生产库 10 集双键）需另行清理，不在本次代码修复范围内
  （见 tasks.md 的人工清理步骤）。

## 验证方式

- 单元测试：新增针对全量模式 episode 键生成的测试用例（「第N集」归一化、
  无集号回退文件名键、SxxExx 保持）。
- 回归：现有 scan 相关测试全部通过。
- 生产验证：修复上线后对「渗透」再巡检，确认不再产生新的文件名键记录。

# Design: 全量模式与标准模式跨键防重对齐

## 修复方案

**实际实现方案**（build 阶段决策）：在 `_enqueue`（`backend/app/tasks/scan.py`）
的幂等检查中，除现有「同 media 同 episode 键」检查外，新增「同 media 同 file_name」
跨键防重检查——同一物理文件无论此前以文件名键还是 SxxExx 键入队，第二次一律
返回 `existing`，不重复入队。

```
_enqueue 幂等检查顺序：
1. 同 media 同 episode 键 → existing（既有）
2. 同 media 同 file_name → existing（新增，本次修复）
3. 写入撞 UNIQUE(media_id, episode) → conflict（既有）
```

### 为何放弃「全量模式第N集归一化」备选方案

最初设想的方案是把全量模式（Emby 未收录）下文件名含「第N集」格式的资源也归一化
为 `SxxExx` 集号键，与标准模式 `match_missing` 对齐。经深入分析后放弃，原因：

1. **无季号信息来源**：全量模式触发条件是 Emby 未收录该剧（`_emby_missing_codes`
   返回 `[None]`），此时 `missing_keys` 为空集，`_RE_CN_EP` 提取到的只是集号
   （如「第05集」→ 5），无法确定属于 S01 还是 S02。强行假设单季（S01）在多季剧
   （如斗罗大陆 S01/S02）下会生成错误的防重键，引入新的键错乱风险。
2. **键生成层面已尽力**：全量模式文件名含标准 SxxExx 时已归一化（scan.py:1766-1768）；
   「第N集」无季号是信息本身缺失，键生成层无法凭空补全。
3. **`_enqueue` 跨键防重是根因消除**：无论两种模式产生何种键，同一物理文件
   （file_name 唯一标识）只允许入队一次，直接封堵「同集双键」路径。

### 关键点

1. **根因定位**：生产事故中同一集在 `download_queue` 出现两条 `downloading` 记录，
   一条 `episode='S01E05'`、一条 `episode='渗透 - 第05集.mp4'`。两键不同绕过
   `UNIQUE(media_id, episode)`。file_name（`渗透 - 第05集.mp4`）是唯一不变的物理
   文件标识，作为跨键防重的锚点。
2. **限定同 media**：file_name 兼容检查带 `media_id` 条件，不同影视同名文件
   （如各自文件夹下 `第01集.mp4`）不受影响。
3. **不改变标准模式键生成**：`match_missing`、`_enqueue` 原同键幂等逻辑不动，
   仅新增 file_name 检查。
4. **保留防丢集语义**：纯数字/无集号文件名（P9 权衡）仍以文件名键入队，只是
   再次出现同名文件时不再重复入队。

## 影响范围

- 仅修改 `backend/app/tasks/scan.py` 的 `_enqueue` 幂等检查（约 13 行）。
- 不涉及接口变更、schema 变更、新 capability。
- 已存在的重复数据（生产库 10 集双键）需另行清理，不在本次代码修复范围内
  （见 tasks.md 的人工清理步骤）。

## 验证方式

- 单元测试：新增 `test_scan_enqueue_dedup.py` 覆盖跨键防重（正向/反向场景、
  不同文件不受影响、不同 media 不受影响）。
- 回归：scan 相关测试（66 个）与全量后端测试（592 个）全部通过。
- 生产验证：修复上线后对「渗透」再巡检，确认不再产生新的文件名键记录。

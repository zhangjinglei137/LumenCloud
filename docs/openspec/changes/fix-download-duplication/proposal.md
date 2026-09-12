# Proposal: 修复全量模式 episode 防重键与标准模式不一致导致的重复下载

## 问题描述

生产环境（2026-09-11）出现重复下载：影视「渗透」（media_id=17）同一集在 `download_queue` 中
出现两条同时 `downloading` 的记录。9 集被重复入队下载（第 05/09/10/11/17/18/29/31/33 集），
`task_queue` 另有第 25 集同样双键，共 10 集重复入队。

典型表现：同一集存在两条记录，`episode` 字段分别为 `S01E05` 与 `渗透 - 第05集.mp4`
（文件名格式），而 `UNIQUE(media_id, episode)` 唯一约束未拦截。

## 根因分析

`backend/app/tasks/scan.py` 中 episode 防重键的生成有两条路径，行为不一致：

1. **标准模式**（Emby 已收录该剧，`match_missing`，scan.py:304）：用 `_RE_SXXEXX` 与
   `_RE_CN_EP`（`第\s*(\d{1,3})\s*[集话]`）把文件名（如 `渗透 - 第05集.mp4`）归一化为
   `S01E05`。
2. **全量模式**（Emby 未收录，`tv_full_mode`，scan.py:1765-1770）：只尝试 `_RE_SXXEXX`；
   文件名无 SxxExx 时**直接回退文件名作防重键**（`matched_key = file_name`）。

「渗透」在 13:37 批次巡检时 Emby 未收录（走全量模式），以文件名键 `渗透 - 第05集.mp4`
入队；14:19 批次 Emby 已收录（走标准模式），`match_missing` 把同名文件匹配为 `S01E05`
再次入队。两个键字符串不同，绕过 `UNIQUE(media_id, episode)` 约束，同一集被下载两次。

全量模式下文件名含「第N集」格式（无 SxxExx）时本可归一化为集号键，但当前实现
未使用 `_RE_CN_EP`，导致键不一致。

## 修复目标

全量模式下 episode 防重键与标准模式对齐：文件名含「第N集/第N话」（`_RE_CN_EP`）
格式时，归一化为 `SxxExx` 集号键（与 `match_missing` 行为一致），仅在集号完全无法
提取时才回退文件名键。修复后同一集在不同巡检模式下产生相同防重键，杜绝重复入队。

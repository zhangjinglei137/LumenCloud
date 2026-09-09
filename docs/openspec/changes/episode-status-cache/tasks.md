# Tasks: episode-status-cache

## 1. 集信息缓存表与 ORM

- [x] 1.1 新增 `episode_info_cache` 表 alembic 迁移（字段：tmdb_id、season、episode、name、air_date，UNIQUE(tmdb_id, season, episode)，双后端兼容 BIG_PK/Text 约定），验证 `alembic upgrade head` 在 SQLite 与 PG 均成功建表
- [x] 1.2 models/__init__.py 新增 `EpisodeInfoCache` ORM 类（映射迁移表，含索引），验证导入不报错且表元数据可查询

## 2. 集信息回源与缓存写入

- [x] 2.1 services/tmdb.py 新增 `refresh_episode_info(tmdb_id)`：复用 `get_tv_all_episodes` 回源并将每集 upsert 到 `episode_info_cache`，验证单影视刷新后缓存表出现对应季/集/名称/首播日期行
- [x] 2.2 services/tmdb.py 新增 `get_episode_info(tmdb_id)` 读缓存函数（无缓存返回 []），验证返回结构与 spec（season/episode/name/air_date）一致
- [x] 2.3 新增后端测试（tests/test_tmdb.py 或新增测试文件）覆盖：刷新写缓存、刷新失败保留旧数据、读取空缓存返回 []，验证 `pytest` 通过

## 3. 每日定时刷新任务

- [x] 3.1 scheduler.py 注册 `episode_info_refresh` 任务（默认每日一次，system_config 键 `episode_info_refresh_interval_hours` 默认 24，get_job_enabled 开关可停用），验证任务注册进 scheduler 且开关读取生效
- [x] 3.2 实现刷新遍历：查询全部 tv media 逐个调用 refresh_episode_info，单影视失败跳过并 log warning，验证全量刷新不中断且失败不影响其余

## 4. 标记状态机与集数统计修正

- [x] 4.1 routers/media.py 新增统一状态判定 `resolve_episode_status(ep)`（已在库>异常>巡检中>未开播>待定，输入综合 episode_state/task_queue/download_queue/Emby 收录/首播日期），验证单测覆盖各状态优先级
- [x] 4.2 修正列表 episode_stats 聚合：可用集数按真实已入库状态统计，验证「已有 x/xx 集」不再恒为 0（构造 5/20 入库场景断言 5）
- [x] 4.3 修正详情 episode_state 输出：逐集状态经 resolve_episode_status 归一、单集大小取 download_queue.file_size（回退 episode_state.file_size，禁止共享第一个文件大小），验证多集大小各异时逐行真实值
- [x] 4.4 详情接口输出集信息缓存列表（调用 get_episode_info，movie/无 tmdb_id 返回 [] 不阻断），验证详情 JSON 含 tmdb_episodes 且无缓存时为空列表

## 5. 入库完成联动更新

- [x] 5.1 library_check._finalize_done 完成单集入库时同步更新该集在库状态（写 episode_state 或标记在库，供列表/详情聚合自然反映），验证 finalize 后列表集数统计 +1
- [x] 5.2 补充测试：入库完成触发状态更新与统计联动，验证 `pytest` 通过

## 6. 前端展示修正

- [x] 6.1 frontend/src/utils/format.ts 新增归一状态文案与颜色映射（已在库/巡检中/异常/未开播/待定），验证 MEDIA_STATUS_MAP/集数状态映射含新状态
- [x] 6.2 MediaListView episodeText 展示真实集数统计（有统计显示 x/xx，无统计回退），验证列表不再全为 0
- [x] 6.3 MediaDetailView 集数状态行使用归一状态与真实大小（大小缺失显示 —），验证详情集数状态展示正确
- [x] 6.4 前端测试补充状态文案/集数统计格式化用例，验证 `vitest` 通过

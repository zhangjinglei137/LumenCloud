-- ============================================
-- LumenCloud 手动迁移 SQL（media 表 19 条）
-- 数据来源：旧 n8n 库 thntime.fun:5432/media（media_db_mcp 只读读取核实）
-- 生成时间：2026-09-06（阶段 4，Phase 3 dry-run 同级数据核验）
-- 目标库：LumenCloud 新库（compose 内置 Postgres lumencloud / 或本地 SQLite）
--
-- ★ 用法：应用首次启动（自动建表）后，在目标库执行本文件即可。
--   Postgres（compose）:
--     docker exec -i lumencloud-db psql -U lumen -d lumencloud < scripts/media_migrate_manual.sql
--   本地 SQLite: sqlite3 data/lumencloud.db < scripts/media_migrate_manual.sql
--
-- 说明：
--   - media 表列：title/tmdb_id/media_type/status(默认tracking)/scan_interval_minutes(默认60)
--   - 19 条全部为国产剧/动画番剧 → media_type='tv'
--   - 幂等：title 已存在则跳过（可重复执行）
--   - TMDB 补查（media_type 精确化/海报等）由后续 scan/nastools_sync 自动完成，无需处理
-- ============================================

INSERT INTO media (title, tmdb_id, media_type)
SELECT v.title, v.tmdb_id, 'tv'
FROM (VALUES
    ('一人之下',         67063),
    ('云南虫谷',         571446),
    ('仙逆',             223911),
    ('光阴之外',         281233),
    ('凡人修仙传',       106449),
    ('剑来',             259537),
    ('南海归墟',         230131),
    ('吞噬星空',         101172),
    ('巫峡棺山',         606709),
    ('怒晴湘西',         86107),
    ('我的前半生',       73028),
    ('昆仑神宫',         201722),
    ('沧元图',           229192),
    ('灵笼',             91097),
    ('牧神记',           236534),
    ('遮天',             224839),
    ('鬼吹灯之精绝古城', 69168),
    ('鬼吹灯之黄皮子坟', 72957),
    ('龙岭迷窟',         101306)
) AS v(title, tmdb_id)
WHERE NOT EXISTS (SELECT 1 FROM media m WHERE m.title = v.title);

-- 校验（应输出 19 行 tracking）
SELECT count(*) AS inserted_count FROM media;

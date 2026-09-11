"""斗罗大陆中英文混搜端到端回归（任务 4-7 集成态）。

线上案例：巡检「斗罗大陆」（tmdb 61852）缺失 S02E167 时，cloudSaver 中英文
关键词双路召回——英文名资源「Soul.Land.S02E167」是正确候选，而「斗罗大陆Ⅱ
绝世唐门.The.Peerless.Tang.Clan.S01E29」（错部续作 + 错季 S01）必须被拒。

本用例打通 _search_and_rank 集成态：
- 任务 3-4：别名关键词（source tmdb.get_by_tmdb_id）+ asyncio.gather 并行搜索
  + share_code 去重；
- 任务 5：A1 分享标题过滤——主标题「斗罗大陆」命中「斗罗大陆Ⅱ…」后紧贴罗马
  数字续作标记 → 拒绝；别名 soulland 命中「Soul.Land.S02E167」的点分隔 → 放行；
- 任务 6：季号硬校验——AAA 季号 S02 ∈ 目标季 {2} 放行；BBB 若穿过 A1 也会被
  S01 ∉ {2} 兜底拒绝；
- 任务 7：_rank_candidates 别名/季号加权保持正确候选入选。

别名唯一来源（I-1 修复）：tmdb.get_by_tmdb_id（_resolve_media_aliases）——media
为纯 ORM 形状（无 aliases 属性），A1 过滤与排序的别名集合必须来自 tmdb；若该源
失守，AAA（英文名候选）在 A1 层即被剔除，用例变红。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tasks import scan as scan_mod


def run(coro):
    return asyncio.run(coro)


def _media():
    # 纯 SimpleNamespace（ORM Media 形状）：无 aliases 属性——别名只能来自
    # tmdb.get_by_tmdb_id mock，杜绝测试层别名注入掩盖生产缺口
    return SimpleNamespace(
        id=1, title="斗罗大陆", media_type="tv", tmdb_id=61852,
    )


def _cs(title, code):
    """cloudSaver.search 归一化返回结构（见 cloudsaver.search 契约）。"""
    return {"title": title, "cloud_links": [{"link": f"https://pan.quark.cn/s/{code}", "cloud_type": "quark"}]}


def test_douluo_end_to_end(monkeypatch):
    """Soul.Land.S02E167 入候选；The.Peerless.Tang.Clan.S01E29 被拒。"""
    async def fake_search(kw):
        if "soulland" in kw or "soul" in kw:
            return [_cs("Soul.Land.S02E167.2023.2160p.WEB-DL.H265.10bit.DDP2.0-PanWEB.mkv", "AAA")]
        if "douluo" in kw or "斗罗" in kw:
            return [_cs("斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29.2023.2160p.WEB-DL.DDP2.0.H265.mkv", "BBB")]
        return []

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)
    # _resolve_media_aliases（统一别名来源）以 media.tmdb_id 调 tmdb 拉别名 →
    # 必须 mock 返回 aliases + year；否则关键词不含英文别名词、fake_search 英文
    # 分支不触发、AAA 无法召回
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"year": 2023, "aliases": ["soul land", "douluo dalu"]}),
    )

    items = run(scan_mod._search_and_rank(_media(), {"S02E167"}))
    codes = {i["share_code"] for i in items}
    assert "AAA" in codes
    assert "BBB" not in codes  # 错季 S01 vs 目标 S02 → 拒绝
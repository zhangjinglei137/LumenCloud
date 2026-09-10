"""A1 标题成员匹配与边界检查（斗罗大陆场景回归）。

背景（线上案例）：巡检搜索「斗罗大陆」时「斗罗大陆Ⅱ绝世唐门.The.Peerless.
Tang.Clan.S01E29」（标题含「斗罗大陆」子串被旧 A1 子串宽松放行 + 季号 S01）
被误收，而正确英文名资源「Soul.Land.S02E167」因不含中文关键词被剔除。修复：
A1 从「精确/前缀/子串」宽松放行改为「[主标题] ∪ [别名集合] 成员匹配 +
后续字符边界检查」（后随分隔符/串尾放行，紧贴中文字符/罗马数字拒绝）。
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tasks import scan as scan_mod


def run(coro):
    return asyncio.run(coro)


def _media(title="凡人修仙传", media_type="tv", tmdb_id=106449, aliases=None):
    return SimpleNamespace(
        title=title, media_type=media_type, tmdb_id=tmdb_id, id=1, aliases=aliases
    )


def _cs_result(title: str, code: str) -> dict:
    """cloudSaver.search 归一化返回结构（见 cloudsaver.search 契约）。"""
    return {
        "title": title,
        "cloud_links": [{"link": f"https://pan.quark.cn/s/{code}", "cloud_type": "quark"}],
    }


# ---------------------------------------------------------------------------
# _share_title_relevant：成员匹配 + 后续字符边界
# ---------------------------------------------------------------------------

def test_title_member_match_ok():
    """主标题成员命中且后随分隔符（括号）→ 放行（凡人修仙传 (2020) 4K 场景）。"""
    assert scan_mod._share_title_relevant("凡人修仙传", "凡人修仙传 (2020) 4K [更新190集]", None)


def test_chinese_suffix_adjacent_rejected():
    """「斗罗大陆Ⅱ绝世唐门」中「斗罗大陆」后紧贴「Ⅱ」（罗马数字）→ 拒绝。"""
    assert not scan_mod._share_title_relevant(
        "斗罗大陆", "斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29", None
    )


def test_english_alias_member_match_ok():
    """英文别名成员命中：候选「Soul.Land.S02E167…」（点分隔）对别名 soulland 命中 → 放行。"""
    assert scan_mod._share_title_relevant(
        "斗罗大陆", "soul.land.s02e167.2160p.mkv", ["soulland", "douluodalu"]
    )


def test_empty_title_degrades_open():
    """剧名为空 → True（兜底不阻断，保持既有降级语义）。"""
    assert scan_mod._share_title_relevant("", "whatever", None) is True


def test_empty_candidate_degrades_open():
    """候选标题为空 → True（兜底放行，避免误杀无标题候选）。"""
    assert scan_mod._share_title_relevant("凡人修仙传", "", ["soulland"]) is True


def test_adjacent_chinese_rejected():
    """「少帅将我宠上天…」对「少帅」：紧贴中文字符 → 拒绝（子串宽松放行移除）。"""
    assert not scan_mod._share_title_relevant("少帅", "少帅将我宠上天 1080p", None)


def test_season_suffix_adjacent_ok():
    """「斗罗大陆S01E157…」：主标题后紧贴季号标记 → 放行（后随非中文/罗马数字）。"""
    assert scan_mod._share_title_relevant("斗罗大陆", "斗罗大陆S01E157 1080p", None)


def test_exact_match_ok():
    """候选与主标题完全相等 → 放行（命中处为串尾）。"""
    assert scan_mod._share_title_relevant("凡人修仙传", "凡人修仙传", None)


def test_alias_missing_not_required():
    """aliases 可空（None / []）→ 仅主标题成员参与匹配，向后兼容。"""
    assert scan_mod._share_title_relevant("斗罗大陆", "斗罗大陆 S01E157 1080p", None)
    assert not scan_mod._share_title_relevant("斗罗大陆", "Soul.Land.S02E167.2160p.mkv", [])


# ---------------------------------------------------------------------------
# _media_aliases：media.aliases JSON 数组解析与空回退
# ---------------------------------------------------------------------------

def test_media_aliases_parses_json_array():
    """media.aliases 为 JSON 数组字符串 → 解析为字符串列表。"""
    assert scan_mod._media_aliases(_media(aliases='["soulland", "douluodalu"]')) == [
        "soulland",
        "douluodalu",
    ]


def test_media_aliases_none_or_invalid_falls_back_empty():
    """aliases 缺失（无属性）/None/空串/非法 JSON/非数组 → 空列表（不阻断搜索）。"""
    assert scan_mod._media_aliases(SimpleNamespace(title="t")) == []
    assert scan_mod._media_aliases(_media(aliases=None)) == []
    assert scan_mod._media_aliases(_media(aliases="")) == []
    assert scan_mod._media_aliases(_media(aliases="{broken json")) == []
    assert scan_mod._media_aliases(_media(aliases='{"a": 1}')) == []


# ---------------------------------------------------------------------------
# _search_and_rank：A1 过滤读取 media.aliases（集成）
# ---------------------------------------------------------------------------

def test_search_and_rank_a1_filter_uses_media_aliases(monkeypatch):
    """A1 过滤传入 media.aliases：主标题不中但别名词（soulland ↔ Soul.Land）命中
    的候选保留；主标题/别名全不中的无关候选剔除。"""
    async def fake_search(kw: str):
        return [
            _cs_result("Soul.Land.S02E167 2160p", "codeA"),
            _cs_result("完全无关的剧集 1080p", "codeB"),
        ]

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"year": 2020}),  # 无 aliases → 关键词不走别名词路径
    )

    media = _media(title="斗罗大陆", tmdb_id=61852, aliases=json.dumps(["soulland", "douluodalu"]))
    items = run(scan_mod._search_and_rank(media, {"S01E01"}))
    codes = [i["share_code"] for i in items]
    assert "codeA" in codes      # 别名词成员命中 → 保留进候选
    assert "codeB" not in codes  # 主标题/别名全不中 → A1 过滤剔除
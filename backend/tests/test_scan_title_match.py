"""A1 标题成员匹配与边界检查（斗罗大陆场景回归）。

背景（线上案例）：巡检搜索「斗罗大陆」时「斗罗大陆Ⅱ绝世唐门.The.Peerless.
Tang.Clan.S01E29」（标题含「斗罗大陆」子串被旧 A1 子串宽松放行 + 季号 S01）
被误收，而正确英文名资源「Soul.Land.S02E167」因不含中文关键词被剔除。修复：
A1 从「精确/前缀/子串」宽松放行改为「[主标题] ∪ [别名集合] 成员匹配 +
后续字符边界检查」。边界语义（裁定收敛）：命中处紧贴罗马数字续作标记
（如「斗罗大陆Ⅱ」）拒绝；紧贴中文字符/分隔符/年份/季号等均放行（回归裁定：
「少帅全集」等合法打包资源不得误杀）。
"""
import asyncio
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
    """「斗罗大陆Ⅱ绝世唐门」中「斗罗大陆」后紧贴「Ⅱ」（罗马数字续作标记）→ 拒绝。"""
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


def test_adjacent_chinese_allowed():
    """「少帅将我宠上天 1080p」「少帅全集」对「少帅」：紧贴中文字符 → 放行
    （回归裁定：CJK 紧贴视为合法打包资源，边界仅拒绝罗马数字续作标记）。"""
    assert scan_mod._share_title_relevant("少帅", "少帅将我宠上天 1080p", None) is True
    assert scan_mod._share_title_relevant("少帅", "少帅全集", None) is True


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
# _search_and_rank：A1 过滤别名来源 = tmdb 解析（集成，I-1 唯一来源）
# ---------------------------------------------------------------------------

def test_search_and_rank_a1_filter_uses_tmdb_aliases(monkeypatch):
    """A1 过滤别名来自 tmdb 解析（_resolve_media_aliases 唯一来源，media 无
    aliases 属性）：主标题不中但别名词（soulland ↔ Soul.Land）命中的候选保留；
    主标题/别名全不中的无关候选剔除。

    适配：目标缺失集从 S01E01 改为 S02E167——候选 Soul.Land.S02E167 季号 S02，
    任务 6 季号硬校验要求候选季号与目标季一致（S02 vs 目标 S01 会被正确拒绝），
    用例意图（A1 别名过滤）与季号无关，故对齐目标季；别名注入从 media.aliases
    属性迁移到 tmdb.get_by_tmdb_id mock（I-1 修复：ORM 链路下 tmdb 是唯一来源）。"""
    async def fake_search(kw: str):
        return [
            _cs_result("Soul.Land.S02E167 2160p", "codeA"),
            _cs_result("完全无关的剧集 1080p", "codeB"),
        ]

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"year": 2020, "aliases": ["soulland", "douluodalu"]}),
    )

    media = _media(title="斗罗大陆", tmdb_id=61852)  # 纯 ORM 形状：无 aliases 属性
    items = run(scan_mod._search_and_rank(media, {"S02E167"}))
    codes = [i["share_code"] for i in items]
    assert "codeA" in codes      # tmdb 别名词成员命中 + 季号 S02 命中目标 S02 → 保留进候选
    assert "codeB" not in codes  # 主标题/别名全不中 → A1 过滤剔除


# ---------------------------------------------------------------------------
# 季号解析与候选硬校验（任务 6）
# ---------------------------------------------------------------------------

def test_season_of_title():
    """候选标题季号：SxxExx 的 Sxx 优先；无 → None。"""
    assert scan_mod._season_of_title("Soul.Land.S02E167.mkv") == 2
    assert scan_mod._season_of_title("The.Peerless.Tang.Clan.S01E29") == 1
    assert scan_mod._season_of_title("凡人修仙传 (2020) 4K [更新190集]") is None


def test_season_of_title_chinese_season_marker():
    """「第N季」标记 → N（接口契约：第N季→N）。"""
    assert scan_mod._season_of_title("凡人修仙传 (2020) 第2季") == 2


def test_candidate_season_ok():
    """候选季号硬校验：解析成功且不在目标季集合 → 拒；命中 → 放行；无季号 → 降级放行。"""
    assert not scan_mod._candidate_season_ok("The.Peerless.Tang.Clan.S01E29", {2})  # S01 vs 目标 S02 → 拒
    assert scan_mod._candidate_season_ok("Soul.Land.S02E167.mkv", {2})              # S02 命中
    assert scan_mod._candidate_season_ok("凡人修仙传 (2020) 4K", {2})               # 无季号降级放行


def test_candidate_season_ok_empty_target_seasons_degrades_open():
    """空目标季集合（movie_missing / tv 未收录全量 → missing_keys 为空集）：
    target_seasons=set() 时带季号候选也整体降级放行，不误杀。对齐任务 7 评分侧
    `target_seasons and ...` 空集不加权口径（ora-5 裁定守卫）。"""
    assert scan_mod._candidate_season_ok("Soul.Land.S02E167.mkv", set())  # 带季号 + 空集合 → 降级放行


def test_search_and_rank_season_filter(monkeypatch):
    """季号硬校验在 _search_and_rank 内生效（A1 过滤之后）：
    错季候选（S01 vs 目标 S02）被拒；命中季（S02）保留；无季号候选（更新集连载
    资源）降级放行。三个候选均通过 A1（主标题/别名词成员命中），仅验证季号层。
    别名来源为 tmdb 解析（I-1：media 无 aliases 属性，别名词走 mock）。"""
    async def fake_search(kw: str):
        return [
            _cs_result("Soul.Land.S02E167 2160p", "codeA"),          # 别名词命中 + S02 命中目标 → 保留
            _cs_result("斗罗大陆S01E29 1080p", "codeB"),             # 主标题命中 + S01 错季 → 拒
            _cs_result("斗罗大陆 (2020) 4K [更新190集]", "codeC"),   # 主标题命中 + 无季号 → 降级放行
        ]

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"year": 2020, "aliases": ["soulland", "douluodalu"]}),
    )

    media = _media(title="斗罗大陆", tmdb_id=61852)  # 纯 ORM 形状：无 aliases 属性
    items = run(scan_mod._search_and_rank(media, {"S02E167"}))
    codes = [i["share_code"] for i in items]
    assert "codeA" in codes      # S02 命中目标季集合 {2} → 保留
    assert "codeB" not in codes  # S01 ∉ {2} → 季号硬校验拒绝
    assert "codeC" in codes      # 无季号 → 降级放行（不误杀更新集连载资源）
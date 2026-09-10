"""搜索召回与版本加权单测（未匹配10 案例修复）。

背景（线上案例）：凡人修仙传（tmdb 106449，2020 动画版）Emby 已有 189 集、
缺失集 S01E190（E191+ 未播出被 aired-only 过滤）。巡检按季聚合生成关键词
「凡人修仙传 S01」，但 cloudSaver 该关键词下召回的夸克分享全是 2025 版/无关剧
（标题不含 S01 的 2020 版资源「凡人修仙传 (2020) [更新190集]」召回不到），
文件编号体系与缺失集错位 → 全量「未匹配10」skipped。

修复：
1. _build_keywords：tv 季词后追加纯标题兜底词，保证标题不含 Sxx 的资源可召回；
2. _rank_candidates：按 TMDB 首播年份加权，标题含媒体年份（如 (2020)）的资源优先，
   多版本同名词条时召回与订阅一致的版本；
3. _search_and_rank：集成前两者，年份取自 tmdb.get_by_tmdb_id（失败降级不加权）。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.tasks import scan as scan_mod


def run(coro):
    return asyncio.run(coro)


def _media(title="凡人修仙传", media_type="tv", tmdb_id=106449):
    return SimpleNamespace(title=title, media_type=media_type, tmdb_id=tmdb_id, id=1)


def _cs_result(title: str, code: str) -> dict:
    """cloudSaver.search 归一化返回结构（见 cloudsaver.search 契约）。"""
    return {
        "title": title,
        "cloud_links": [{"link": f"https://pan.quark.cn/s/{code}", "cloud_type": "quark"}],
    }


def _candidate(title: str, code: str) -> dict:
    """_expand_share_codes 展开后的候选结构（_rank_candidates 输入契约）。"""
    return {"title": title, "share_code": code}


# ---------------------------------------------------------------------------
# _build_keywords：季词 + 纯标题兜底
# ---------------------------------------------------------------------------

def test_build_keywords_tv_returns_season_word_plus_title():
    """tv：缺失集按季聚合 → [季词, 纯标题兜底词]，不重复。"""
    kws = scan_mod._build_keywords(_media(), {"S01E190"})
    assert kws == ["凡人修仙传 S01", "凡人修仙传"]


def test_build_keywords_tv_multi_season():
    """tv：多季缺失集 → 各季词 + 纯标题兜底词。"""
    kws = scan_mod._build_keywords(_media(), {"S01E01", "S02E05"})
    assert kws == ["凡人修仙传 S01", "凡人修仙传 S02", "凡人修仙传"]


def test_build_keywords_tv_no_season_full_mode():
    """tv：无缺失集季信息（全量模式）→ 仅纯标题。"""
    kws = scan_mod._build_keywords(_media(), set())
    assert kws == ["凡人修仙传"]


def test_build_keywords_movie_uses_title_only():
    """movie：仅标题，不加季词。"""
    kws = scan_mod._build_keywords(_media(media_type="movie"), {"S01E01"})
    assert kws == ["凡人修仙传"]


def test_build_keywords_includes_aliases_and_caps():
    """tv：主标题季词 + 主标题 + 别名词，总量 ≤5。"""
    media = _media(title="斗罗大陆")  # SimpleNamespace: title/媒体类型/tmdb_id
    kws = scan_mod._build_keywords(media, {"S01E157"}, aliases=["soul land", "douluo dalu"])
    assert "斗罗大陆 S01" in kws
    assert "soulland" in kws  # 别名归一化后入词
    assert len(kws) <= 5


def test_build_keywords_movie_ignores_aliases():
    """movie：不加别名词（仅主标题）。"""
    kws = scan_mod._build_keywords(
        _media(title="斗罗大陆", media_type="movie"), {"S01E01"}, aliases=["soul land"]
    )
    assert kws == ["斗罗大陆"]


def test_build_keywords_aliases_inserted_before_title_word():
    """tv：别名词插在季词之后、纯标题兜底词之前。"""
    kws = scan_mod._build_keywords(_media(title="斗罗大陆"), {"S01E01"}, aliases=["soul land"])
    assert kws == ["斗罗大陆 S01", "soulland", "斗罗大陆"]


def test_build_keywords_async_uses_tmdb_aliases(monkeypatch):
    """async 封装：从 tmdb.get_by_tmdb_id 拉取 aliases 参与构造。"""
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"aliases": ["soul land"]}),
    )
    kws = run(scan_mod._build_keywords_async(_media(title="斗罗大陆"), {"S01E157"}))
    assert "soulland" in kws


def test_build_keywords_async_falls_back_when_tmdb_fails(monkeypatch):
    """async 封装：tmdb 拉取失败 → 降级仅主标题词，不阻断搜索。"""
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(side_effect=RuntimeError("tmdb down")),
    )
    kws = run(scan_mod._build_keywords_async(_media(title="斗罗大陆"), {"S01E157"}))
    assert kws == ["斗罗大陆 S01", "斗罗大陆"]


# ---------------------------------------------------------------------------
# _rank_candidates：TMDB 年份加权
# ---------------------------------------------------------------------------

def test_rank_candidates_year_weight_prefers_matching_version():
    """年份加权：媒体 2020 年 → 标题含 (2020) 的资源排到 (2025) 之前。"""
    items = [
        _candidate("凡人修仙传 (2025) 1080p NF S01全", "code2025"),
        _candidate("凡人修仙传 (2020) 4K [更新190集]", "code2020"),
    ]
    ranked = scan_mod._rank_candidates(_media(), items, year=2020)
    assert ranked[0]["share_code"] == "code2020"
    assert ranked[1]["share_code"] == "code2025"


def test_rank_candidates_without_year_keeps_stable_order():
    """无年份（year=None）→ 不加权，按标题命中分排序保持原序。"""
    items = [
        _candidate("凡人修仙传 (2025) 1080p NF S01全", "code2025"),
        _candidate("凡人修仙传 (2020) 4K [更新190集]", "code2020"),
    ]
    ranked = scan_mod._rank_candidates(_media(), items, year=None)
    # 两标题均含「凡人修仙传」同分，sort 稳定 → 保持原序
    assert ranked[0]["share_code"] == "code2025"


# ---------------------------------------------------------------------------
# _search_and_rank：季词 + 纯标题召回 + 年份加权（集成）
# ---------------------------------------------------------------------------

def test_search_and_rank_recalls_title_word_and_ranks_by_year(monkeypatch):
    """集成：季词只召回 2025 版、纯标题词召回 2020 版 → 两个关键词都搜，
    2020 版（年份加权）排前——未匹配10 案例的修复路径。"""
    async def fake_search(kw: str):
        if "S01" in kw:
            return [_cs_result("凡人修仙传 (2025) 1080p NF S01全", "code2025")]
        return [
            _cs_result("凡人修仙传 (2025) 1080p NF S01全", "code2025"),
            _cs_result("凡人修仙传 (2020) 4K [更新190集]", "code2020"),
        ]

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(return_value={"year": 2020, "status": "Returning Series"}),
    )

    results = run(scan_mod._search_and_rank(_media(), {"S01E190"}))
    codes = [r["share_code"] for r in results]
    assert codes[0] == "code2020"  # 年份加权 → 2020 版优先
    assert "code2025" in codes


def test_search_and_rank_tmdb_year_failure_still_searches(monkeypatch):
    """TMDB 年份获取失败（降级 None）→ 不阻断搜索，仍返回排序结果。"""
    async def fake_search(kw: str):
        return [_cs_result("凡人修仙传 (2020)", "codeA")]

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)
    monkeypatch.setattr(
        scan_mod.tmdb, "get_by_tmdb_id",
        AsyncMock(side_effect=RuntimeError("tmdb down")),
    )
    results = run(scan_mod._search_and_rank(_media(), {"S01E190"}))
    assert results and results[0]["share_code"] == "codeA"

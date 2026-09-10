# tmdb-alias-search-match 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---
change: tmdb-alias-search-match
design-doc: docs/superpowers/specs/2026-09-10-tmdb-alias-search-design.md
base-ref: a6a187053d27b76375b886bda1fcc072441eda57
---

**Goal:** 通过 TMDB 别名扩展 + 多关键词并行搜索 + 季号校验 + 成员匹配，修复「斗罗大陆」类中英文混搜误匹配。

**Architecture:** 别名在 `get_by_tmdb_id` 回源时读取 original_title/also_known_as 落库 tmdb_cache.aliases；巡检时 `_build_keywords` 构造 ≤5 关键词并行搜 cloudSaver；`_share_title_relevant` 改别名成员匹配 + 边界检查，新增季号硬校验（无季号降级）；`_rank_candidates` 增量加权。

**Tech Stack:** FastAPI (Python), SQLAlchemy async, Alembic, pytest, aiohttp/httpx

**Spec:** docs/openspec/changes/tmdb-alias-search-match/specs/title-alias-search/spec.md + media-pipeline/spec.md；设计见 docs/superpowers/specs/2026-09-10-tmdb-alias-search-design.md

## Global Constraints

- 产物语言 zh-CN（代码注释/测试名/commit message 中文）
- 保持确定性规则：不引入 AI/LLM 挑选
- 不改下载队列消费与任务流转；不改前端
- 关键词总数上限常量 `_MAX_SEARCH_KEYWORDS = 5`
- 既有降级语义保留：单关键词失败 warning 继续；全部失败（ok==0）抛 `ScanSearchUnavailable`
- 别名缺失/解析失败一律回退主标题，绝不阻断搜索
- 提交信息遵循 Conventional Commits（feat/fix/refactor/test/chore），摘要 ≤50 字符中文，不写句号

---

## 任务 1: tmdb_cache 新增 aliases 列（迁移 + 模型）

**Files:**
- Create: `backend/alembic/versions/0016_tmdb_cache_aliases.py`
- Modify: `backend/app/models/__init__.py:381-410`（TmdbCache 增加 aliases 列）

**Interfaces:**
- Consumes: 现有 TmdbCache 模型结构
- Produces: `TmdbCache.aliases`（Text, nullable；存 JSON 数组字符串，如 `["soul land","dou luo da lu"]`）

- [x] **Step 1: 写失败测试（模型字段）**

在 `backend/tests/test_tmdb_cache.py` 末尾新增：

```python
def test_tmdb_cache_aliases_column(db):
    """TmdbCache 含可空 aliases 列（迁移后）。"""
    from sqlalchemy import inspect
    cols = {c["name"] for c in inspect(db).get_columns("tmdb_cache")}
    assert "aliases" in cols
```

- [x] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_tmdb_cache.py::test_tmdb_cache_aliases_column -v`
Expected: FAIL（tmdb_cache 无 aliases 列）

- [x] **Step 3: 写迁移与模型字段**

`backend/app/models/__init__.py` TmdbCache 增加：

```python
    # TV 别名集合（JSON 数组字符串：original_title + also_known_as 归一化小写）。
    # 由 get_by_tmdb_id 详情回源时落库；search_multi 不写（search 响应无该字段）。
    # 迁移见 alembic/versions/0016_tmdb_cache_aliases.py。
    aliases = mapped_column(Text, nullable=True)
```

参照 `0014_tmdb_cache_episode_count.py` 风格创建 `backend/alembic/versions/0016_tmdb_cache_aliases.py`（add_column tmdb_cache.aliases Text nullable，down_revision 指向 0015）。

- [x] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_tmdb_cache.py -v`
Expected: PASS（含新用例与既有用例）

- [x] **Step 5: 提交**

```bash
git add backend/alembic/versions/0016_tmdb_cache_aliases.py backend/app/models/__init__.py backend/tests/test_tmdb_cache.py
git commit -m "feat(tmdb): tmdb_cache 新增 aliases 列（别名持久化基础）"
```

---

## 任务 2: get_by_tmdb_id 读取并落库别名

**Files:**
- Modify: `backend/app/services/tmdb.py:145-197`（`_upsert_cache` 增 aliases 参数）、`backend/app/services/tmdb.py:200-286`（`get_by_tmdb_id` 读取并写入）、`backend/app/services/tmdb.py:289-360`（`search_multi` 传 aliases=None 不覆盖）

**Interfaces:**
- Consumes: TmdbCache.aliases 列（任务 1）
- Produces: `_upsert_cache(..., aliases: list[str] | None = None)`；`get_by_tmdb_id` 返回 dict 增加 `aliases: list[str]`；模块级 `_normalize_aliases(raw_aliases, original_title) -> list[str]`（小写、去空格、去版本后缀、去重、限 20 条）

- [x] **Step 1: 写失败测试**

`backend/tests/test_tmdb_cache.py` 新增：

```python
def test_get_by_tmdb_id_parses_aliases_and_caches(monkeypatch):
    """详情回源时 original_title + also_known_as 归一化落库。"""
    from app.services import tmdb as tmdb_mod
    payload = {
        "name": "斗罗大陆", "original_name": "Soul Land",
        "also_known_as": ["Soul Land", "Douluo Dalu", "斗罗大陆"],
        "first_air_date": "2018-01-20", "status": "Returning Series",
        "number_of_episodes": 263,
    }
    captured = {}
    async def fake_get(url, params):
        captured["params"] = params
        class R:
            status_code = 200
            def json(self): return payload
        return R()
    import httpx
    monkeypatch.setattr(tmdb_mod.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))
    meta = tmdb_mod._run_sync...  # 按 test_tmdb_cache 既有 run() 模式调用 get_by_tmdb_id(123, "tv")
    assert "soul land" in meta["aliases"]
    assert "douluo dalu" in meta["aliases"]
```

（沿用该文件既有的 `_FakeClient`/`run()` 辅助；`_FakeClient` 需支持 `__aenter__`/`__aexit__`/`get`）

- [x] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_tmdb_cache.py::test_get_by_tmdb_id_parses_aliases_and_caches -v`
Expected: FAIL（aliases 键不存在）

- [x] **Step 3: 实现**

```python
def _normalize_aliases(raw: list | None, original_title: str | None) -> list[str]:
    """别名归一化：小写、去空格、去括号版本后缀、去重；上限 20 防膨胀。"""
    out: list[str] = []
    for name in [original_title, *(raw or [])]:
        if not name or not isinstance(name, str):
            continue
        s = name.strip().lower().replace(" ", "")
        s = re.sub(r"\(\d{4}\)", "", s).strip()
        if s and s not in out:
            out.append(s)
    return out[:20]
```

`get_by_tmdb_id` 回源分支：`raw_aliases = payload.get("also_known_as") or []`、`orig = payload.get("original_name") or payload.get("original_title")`，调用 `_upsert_cache(..., aliases=_normalize_aliases(raw_aliases, orig))`，返回 dict 附 `aliases`；`_upsert_cache` 增 `aliases` 参数（仅非 None 时覆盖，search_multi 传 None 不覆盖已落库别名）。

- [x] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_tmdb_cache.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add backend/app/services/tmdb.py backend/tests/test_tmdb_cache.py
git commit -m "feat(tmdb): 详情回源时解析并落库别名集合"
```

---

## 任务 3: _build_keywords 别名关键词扩展

**Files:**
- Modify: `backend/app/tasks/scan.py:693-709`（`_build_keywords`）

**Interfaces:**
- Consumes: `media.title`、`media.tmdb_id`；`tmdb.get_by_tmdb_id` 返回 dict 的 `aliases` 键（任务 2）
- Produces: `_build_keywords(media, missing_keys) -> list[str]` 支持别名词；`_MAX_SEARCH_KEYWORDS = 5` 常量；异步封装 `_build_keywords_async(media, missing_keys) -> list[str]`（内部调 tmdb.get_by_tmdb_id 拉 aliases，失败降级只返回主标题词）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_scan_search_keywords.py` 新增：

```python
def test_build_keywords_includes_aliases_and_caps():
    """tv：主标题季词 + 主标题 + 别名词，总量 ≤5。"""
    media = _media()  # SimpleNamespace: title/媒体类型/tmdb_id
    kws = scan_mod._build_keywords(media, {"S01E157"}, aliases=["soul land", "douluo dalu"])
    assert "斗罗大陆 S01" in kws
    assert "soulland" in kws  # 别名归一化后入词
    assert len(kws) <= 5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_scan_search_keywords.py::test_build_keywords_includes_aliases_and_caps -v`
Expected: FAIL（无别名词）

- [ ] **Step 3: 实现**

```python
_MAX_SEARCH_KEYWORDS = 5

def _build_keywords(media, missing_keys: set[str], aliases: list[str] | None = None) -> list[str]:
    title = (media.title or "").strip()
    if not title:
        return []
    # aliases 由调用方从 tmdb_cache 传入（_build_keywords_async 负责拉取）；
    # 纯函数不读 media 模型属性（ORM 无 aliases 列），保持可测。
    aliases = [a for a in (aliases or []) if isinstance(a, str) and a.strip()]
    if media.media_type == "movie":
        kws = [title]
    else:
        seasons = sorted({_season_of_key(k) for k in missing_keys})
        kws = [f"{title} S{se:02d}" for se in seasons] if seasons else []
        if title not in kws:
            kws.append(title)
    # 别名词（≤2 个，插到纯标题词前）；总量裁剪到上限
    for a in aliases[:2]:
        a_norm = a.strip().lower().replace(" ", "")
        a_word = f"{a_norm} S{seasons[0]:02d}" if seasons and media.media_type != "movie" else a_norm
        if a_word not in kws:
            kws.insert(-1 if kws else 0, a_word)
    return kws[:_MAX_SEARCH_KEYWORDS]
```

并新增异步封装（scan 调用侧使用）：

```python
async def _build_keywords_async(media, missing_keys: set[str]) -> list[str]:
    """取别名后构造关键词；tmdb 不可用/失败降级为仅主标题词，绝不阻断搜索。"""
    aliases: list[str] = []
    if media.tmdb_id:
        try:
            meta = await tmdb.get_by_tmdb_id(media.tmdb_id, media.media_type or "tv")
            aliases = meta.get("aliases") or []
        except Exception as exc:
            logger.warning("[scan] media=%s 别名获取失败（降级主标题）: %s", media.id, exc)
    return _build_keywords(media, missing_keys, aliases=aliases)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_scan_search_keywords.py -v`
Expected: PASS（既有用例保持：季词 + 纯标题兜底不变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_search_keywords.py
git commit -m "feat(scan): 巡检关键词扩展别名词并限制上限"
```

---

## 任务 4: _search_and_rank 多关键词并行搜索

**Files:**
- Modify: `backend/app/tasks/scan.py:872-908`（`_search_and_rank`）

**Interfaces:**
- Consumes: `_build_keywords_async`（任务 3）、`cloudsaver.search`
- Produces: `_search_and_rank` 并行语义：`asyncio.gather` 并发搜索多词，结果合并去重（share_code 维度），单词失败降级语义保留

- [ ] **Step 1: 写失败测试**

`backend/tests/test_scan_search_keywords.py` 新增：

```python
def test_search_and_rank_parallel_merges_dedup(monkeypatch):
    """多关键词并行搜索：结果合并、share 去重、单词失败不中断。"""
    calls = []

    async def fake_search(kw):
        calls.append(kw)
        if kw == "斗罗大陆":
            return [_cs_result("Soul.Land.S02E167", "AAA")]
        if kw == "soulland":
            return [_cs_result("soulland.s02e167", "AAA"), _cs_result("Soul.Land.S02E168", "BBB")]
        return []

    async def fake_cloud(kw):
        return await fake_search(kw)

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_cloud)
    media = _media()
    media.aliases = json.dumps(["soulland"])
    items = run(scan_mod._search_and_rank(media, {"S01E157"}))
    # AAA 去重保留一条，BBB 保留
    codes = {i["share_code"] for i in items}
    assert "AAA" in codes and "BBB" in codes
    assert len(calls) >= 3
```

（沿用文件头部 `_cs_result`/`run()`/`_media()` 辅助；若 `_search_and_rank` 改为内部 `_build_keywords_async`，media 直接带 aliases 属性避免额外 mock tmdb）

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_scan_search_keywords.py::test_search_and_rank_parallel_merges_dedup -v`
Expected: FAIL（当前串行只搜主标题/季词，无别名词）

- [ ] **Step 3: 实现**

`_search_and_rank` 开头改为：

```python
    keywords = await _build_keywords_async(media, missing_keys)
    raw: list[dict] = []

    async def _search_one(kw: str) -> tuple[str, list[dict]]:
        try:
            return kw, await cloudsaver.search(kw)
        except Exception as exc:
            logger.warning("[scan] cloudSaver 搜索 %s 失败: %s", kw, exc)
            return kw, []

    ok = 0
    results = await asyncio.gather(*(_search_one(kw) for kw in keywords))
    for kw, res in results:
        if res:
            ok += 1
        raw.extend(res)
    if ok == 0 and keywords:
        raise ScanSearchUnavailable("全部搜索关键词调用 cloudSaver 均失败")
```

后续 `_expand_share_codes` 处增加 share 去重（按 share_code，保留首现），其余保持。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_scan_search_keywords.py backend/tests/test_scan_async.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_search_keywords.py
git commit -m "feat(scan): 云盘搜索改多关键词并行并去重合并"
```

---

## 任务 5: A1 成员匹配 + 边界检查重构

**Files:**
- Modify: `backend/app/tasks/scan.py:807-825`（`_share_title_relevant`）

**Interfaces:**
- Consumes: media.title + aliases（task 3 属性契约）
- Produces: `_share_title_relevant(media_title, cand_title, aliases: list[str] | None = None) -> bool`：别名集合成员匹配 + 后续字符边界检查（后随分隔符/空白/括号/数字/季号标记放行；紧贴中文字符/罗马数字拒绝）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_scan_title_match.py`：

```python
"""A1 标题成员匹配与边界检查（斗罗大陆场景回归）。"""
from app.tasks import scan as scan_mod

def test_title_member_match_ok():
    assert scan_mod._share_title_relevant("凡人修仙传", "凡人修仙传 (2020) 4K [更新190集]", None)

def test_chinese_suffix_adjacent_rejected():
    # 「斗罗大陆Ⅱ绝世唐门」中「斗罗大陆」后紧贴「Ⅱ」→ 拒绝
    assert not scan_mod._share_title_relevant("斗罗大陆", "斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29", None)

def test_english_alias_member_match_ok():
    assert scan_mod._share_title_relevant("斗罗大陆", "soul.land.s02e167.2160p.mkv", ["soulland", "douluodalu"])

def test_empty_title_degrades_open():
    assert scan_mod._share_title_relevant("", "whatever", None) is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_scan_title_match.py -v`
Expected: FAIL（`斗罗大陆Ⅱ...` 现被子串放行返回 True）

- [ ] **Step 3: 实现**

```python
def _title_member_hit(cand_norm: str, member: str) -> bool:
    """成员命中 + 后续字符边界：member 在 cand 中出现且后一字符为分隔/结束。"""
    idx = cand_norm.find(member)
    while idx != -1:
        after = cand_norm[idx + len(member):len(cand_norm) + 1]
        if not after or after[0] in " .()[]-—_【】":
            return True
        idx = cand_norm.find(member, idx + 1)
    return False


def _share_title_relevant(media_title, cand_title, aliases=None):
    if not media_title:
        return True
    title_norm = media_title.replace(" ", "").lower()
    cand_norm = (cand_title or "").replace(" ", "").lower()
    if not cand_norm:
        return True
    members = [title_norm, *(a.replace(" ", "").lower() for a in (aliases or []) if a)]
    return any(_title_member_hit(cand_norm, m) for m in members if m)
```

`_search_and_rank` 调用处传入 `aliases`（从 media 属性解析，与 task 3 相同 helper 提取；抽公共 `_media_alias_list(media) -> list[str]`）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_scan_title_match.py backend/tests/test_scan_search_keywords.py backend/tests/test_scan_silent_filter.py -v`
Expected: PASS（既有 A1 相关用例不退化）

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_title_match.py
git commit -m "fix(scan): A1 标题过滤改成员匹配与边界检查"
```

---

## 任务 6: 季号解析与硬校验

**Files:**
- Modify: `backend/app/tasks/scan.py`（新增 `_season_of_title`；`_search_and_rank` 过滤处加季号校验）

**Interfaces:**
- Consumes: `_RE_SXXEXX`（scan.py:60）、`_season_of_key`（scan.py:688）
- Produces: `_season_of_title(text: str) -> int | None`（候选标题季号：SxxExx 的 Sxx；`第N季`→N；无 → None）；`_candidate_season_ok(cand_title, target_seasons: set[int]) -> bool`（季号解析成功且不在集合 → False；无季号 → True 降级）

- [ ] **Step 1: 写失败测试**

`backend/tests/test_scan_title_match.py` 追加：

```python
def test_season_of_title():
    assert scan_mod._season_of_title("Soul.Land.S02E167.mkv") == 2
    assert scan_mod._season_of_title("The.Peerless.Tang.Clan.S01E29") == 1
    assert scan_mod._season_of_title("凡人修仙传 (2020) 4K [更新190集]") is None

def test_candidate_season_ok():
    assert not scan_mod._candidate_season_ok("The.Peerless.Tang.Clan.S01E29", {2})  # S01 vs 目标 S02 → 拒
    assert scan_mod._candidate_season_ok("Soul.Land.S02E167.mkv", {2})              # S02 命中
    assert scan_mod._candidate_season_ok("凡人修仙传 (2020) 4K", {2})               # 无季号降级放行
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_scan_title_match.py -v`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现**

```python
def _season_of_title(text: str) -> int | None:
    m = _RE_SXXEXX.search(text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*(\d{1,2})\s*季", text or "")
    if m:
        return int(m.group(1))
    return None


def _candidate_season_ok(cand_title: str, target_seasons: set[int]) -> bool:
    """候选季号硬校验：解析成功且不在目标季集合 → 拒绝；无季号 → 降级放行。"""
    s = _season_of_title(cand_title)
    if s is None:
        return True
    return s in target_seasons
```

`_search_and_rank` 的 A1 过滤后追加季号校验：`target_seasons = {_season_of_key(k) for k in missing_keys}`，`if not _candidate_season_ok(title, target_seasons): continue`（记录 debug 日志）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_scan_title_match.py backend/tests/test_scan_full_mode_filter.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_title_match.py
git commit -m "feat(scan): 候选季号硬校验（无季号降级放行）"
```

---

## 任务 7: _rank_candidates 别名/季号增量加权

**Files:**
- Modify: `backend/app/tasks/scan.py:716-737`（`_rank_candidates`）

**Interfaces:**
- Consumes: 任务 5/6 的 aliases 提取与季号逻辑
- Produces: `_rank_candidates(media, items, year=None, aliases=None, target_seasons=None)`：主标题精确 +10 保留；别名/原名精确命中 +8；季号与目标一致 +3；词命中/年份保留

- [ ] **Step 1: 写失败测试**

`backend/tests/test_scan_search_keywords.py` 追加：

```python
def test_rank_candidates_alias_and_season_weights():
    """别名命中与正确季号加权优先。"""
    wrong = _candidate("斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29", "W")
    right = _candidate("Soul.Land.S02E167.2160p.mkv", "R")
    ranked = scan_mod._rank_candidates(
        _media(), [wrong, right], year=2023,
        aliases=["soulland", "douluodalu"], target_seasons={2},
    )
    assert ranked[0]["share_code"] == "R"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_scan_search_keywords.py::test_rank_candidates_alias_and_season_weights -v`
Expected: FAIL（wrong 因含主标题子串得分更高排前）

- [ ] **Step 3: 实现**

在 `_rank_candidates` 循环内追加（保留原有权重行）：

```python
        # 别名/原名命中 +8（成员匹配，非子串）；季号一致 +3
        alias_hit = any(_title_member_hit(name, m) for m in (alias_norms or []) if m)
        if alias_hit:
            score += 8
        s = _season_of_title(name)
        if target_seasons and s is not None and s in target_seasons:
            score += 3
```

签名改为 `_rank_candidates(media, items, year=None, aliases=None, target_seasons=None)`，`alias_norms = [a.replace(" ", "").lower() for a in (aliases or [])]`；`_search_and_rank` 调用处传入 aliases 与 target_seasons。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_scan_search_keywords.py backend/tests/test_scan_async.py -v`
Expected: PASS（既有排序用例保持）

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_search_keywords.py
git commit -m "feat(scan): 候选排序增加别名与季号加权"
```

---

## 任务 8: 斗罗大陆端到端回归用例

**Files:**
- Create: `backend/tests/test_scan_douluo_regression.py`

**Interfaces:**
- Consumes: `_search_and_rank`（任务 4-7 集成态）

- [ ] **Step 1: 写失败测试**

```python
"""斗罗大陆中英文混搜回归：错部/错季被拒，正确英文名入选。"""
import asyncio
import json
from types import SimpleNamespace
import pytest
from app.tasks import scan as scan_mod

def run(coro):
    return asyncio.run(coro)

def _media():
    return SimpleNamespace(
        id=1, title="斗罗大陆", media_type="tv", tmdb_id=61852,
        aliases=json.dumps(["soul land", "douluo dalu"]),
    )

def _cs(title, code):
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
    items = run(scan_mod._search_and_rank(_media(), {"S02E167"}))
    codes = {i["share_code"] for i in items}
    assert "AAA" in codes
    assert "BBB" not in codes  # 错季 S01 vs 目标 S02 → 拒绝
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest backend/tests/test_scan_douluo_regression.py -v`
Expected: FAIL（BBB 当前被收录）

- [ ] **Step 3: 实现**

本任务无新实现——依赖任务 4-7 已落地；若失败则回到对应任务修复（按 systematic-debugging 走根因）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest backend/tests/test_scan_douluo_regression.py backend/tests/test_scan_search_keywords.py backend/tests/test_scan_silent_filter.py backend/tests/test_scan_full_mode_filter.py -v`
Expected: PASS（斗罗大陆用例 + 既有搜索用例全部通过）

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_scan_douluo_regression.py
git commit -m "test(scan): 新增斗罗大陆中英文混搜回归用例"
```

---

## 任务 9: 全量测试与构建验证

**Files:**
- 无源码改动（验证任务）

- [ ] **Step 1: 后端全量测试**

Run: `pytest backend/tests/ -q`
Expected: 全绿（既有 60+ 测试文件无回归；scan/tmdb 相关文件重点确认）

- [ ] **Step 2: 后端静态/启动检查**

Run: `python -m compileall backend/app` 与 `python -c "from app.tasks import scan; from app.services import tmdb"`（backend venv 下）
Expected: 无语法/导入错误

- [ ] **Step 3: 前端构建（后端无改动但 guard 会跑 npm build）**

Run: `npm run build`（frontend 目录）
Expected: 构建成功

- [ ] **Step 4: 提交（如有遗留改动）**

```bash
git status --short
git add -A && git commit -m "chore(scan): 搜索匹配改造全量验证通过"
```

---

## 任务 10: spec 同步与收尾

**Files:**
- Modify: `docs/openspec/changes/tmdb-alias-search-match/tasks.md`（勾选全部任务）

- [ ] **Step 1: 勾选 tasks.md 全部复选框**
  确认 10 项全部 `- [x]`，与实现一致
- [ ] **Step 2: 提交**

```bash
git add docs/openspec/changes/tmdb-alias-search-match/tasks.md
git commit -m "docs(comet): 勾选 tmdb-alias-search-match 全部任务"
```

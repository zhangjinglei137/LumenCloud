"""
scan_all_media 巡检主流程（设计文档 §4.3 / 实施计划 §3.5）

阶段 2 范围：搜索 → 入队（不转存）。
阶段 3 改造：share-list 递归遍历到具体文件（_walk_share，对齐 n8n quarkRecursiveGetFiles，
           阶段 1 Q2 实证：文件夹 fid 不被生产版落盘，须逐文件转存）；
           入队成功后触发 transfer.trigger_transfer()（transfer lane 并行实现，未就绪静默跳过）。

入口：
- scan_media(media_id)   单影视巡检；API POST /api/media/{id}/scan 手动触发，
                         返回最近一条 task_run id（routers/media.py 契约）
- scan_all_media()       遍历全部 tracking/downloading 影视
- scan_all_media_job()   APScheduler job 包装（阶段2 不注册定时，仅保留供手动调用）

服务层调用（app.services，另一 lane 已产出）：
- emby.find_emby_id / emby.get_missing_episodes  → 防重基线（P11 模糊兜底内置）
- cloudsaver.search / cloudsaver.share_info / cloudsaver.share_list → 搜源/凭据/文件
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.models import DownloadTask, EpisodeState, Media, TransferQueue
from app.services import cloudsaver, config_store, emby
from app.tasks import as_bool, get_config_value, record_task_run

logger = logging.getLogger(__name__)

# 每部影视一把进程内扫描锁：手动 / 定时扫描共享，杜绝并发双入队（设计文档 §3.1 入队幂等约定）
_scan_locks: dict[int, asyncio.Lock] = {}
# P1-6（council）：后台任务强引用集合——fire-and-forget 触发 transfer 时防任务被 GC 回收
_background: set[asyncio.Task] = set()

# 分享码正则（P8 沿用：当前仅匹配 pan.quark.cn/s/，多域名兼容列为待办）
_SHARE_CODE_RE = re.compile(r"pan\.quark\.cn/s/([0-9a-zA-Z]+)", re.IGNORECASE)
# 视频文件扩展名白名单（对齐 n8n quarkRecursiveGetFiles 的 ALLOWED_EXTENSIONS = {mp4, mkv}，
# 真实验证中发现 Cover.jpg 等非视频文件会被全量模式匹配入队，浪费中转空间 → 统一过滤）
_VIDEO_EXTENSIONS = frozenset({"mp4", "mkv"})


def _is_video_file(file_name: str) -> bool:
    """是否视频文件（扩展名白名单，对齐 n8n ALLOWED_EXTENSIONS={mp4,mkv}）。"""
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return ext in _VIDEO_EXTENSIONS
# 三重匹配：SxxExx / SxxExxx / 第N集
_RE_SXXEXX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_RE_CN_EP = re.compile(r"第\s*(\d{1,3})\s*[集话]")

# 入队防重态：queued/transferring/downloading 视为已处理；done 在 Emby 二次确认前仍参与防重（§4.5）；
# failed 需人工 retry（§4.5 retry≥3→failed），不可被 scan 自动重新入队（否则撞 UNIQUE 且绕过人工确认）
_ACTIVE_STATES = ("queued", "transferring", "downloading", "done", "failed")

# P3-1（延后项）：done→failed 循环上限——对齐 transfer._RETRY_LIMIT（3 次）。
# _resolve_done_states 中 done→failed 算一次循环（retry_count +1），达到上限后保持
# done 状态只写 error，杜绝「人工 retry→转存 done→Emby 仍未入库转 failed」无限循环。
_DONE_FAIL_RETRY_LIMIT = 3
# 转 failed 与达上限两种 error 文案（供测试与人工排查识别）
_DONE_FAIL_ERROR = "下载完成但 Emby 未入库，请人工确认"
_DONE_LIMIT_ERROR = "下载完成但 Emby 未入库已达循环上限，请人工核实 Emby 端"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 三重匹配
# ---------------------------------------------------------------------------

def _fmt_episode(season: int, ep: int) -> str:
    """规范化集 key：S01E01（两位）；三位集数（如 S01E100）保留三位。"""
    ep_s = f"E{ep:03d}" if ep >= 100 else f"E{ep:02d}"
    return f"S{season:02d}{ep_s}"


def _ep_num(key: str) -> int | None:
    m = re.search(r"E(\d{2,3})$", key or "")
    return int(m.group(1)) if m else None


def match_missing(text: str, missing_keys: set[str]) -> str | None:
    """三重匹配缺失集：SxxExx / SxxExxx / 第N集（跨季按集号匹配）。

    返回命中的缺失集 key；未命中返回 None。
    """
    if not text:
        return None
    # 1) SxxExx / SxxExxx（带季号精确匹配）
    m = _RE_SXXEXX.search(text)
    if m:
        key = _fmt_episode(int(m.group(1)), int(m.group(2)))
        if key in missing_keys:
            return key
    # 2) 第N集（无季号，跨季匹配集号）
    m = _RE_CN_EP.search(text)
    if m:
        ep = int(m.group(1))
        hits = [k for k in missing_keys if _ep_num(k) == ep]
        if hits:
            return sorted(hits)[0]
    return None


# ---------------------------------------------------------------------------
# Emby 防重基线（§4.3 步骤2 / P11）
# ---------------------------------------------------------------------------

async def _emby_missing_codes(media) -> list[str | None] | None:
    """Emby 防重基线。

    返回缺失集列表；**None 表示基线不可用**（Emby 未收录该剧，无法探明遗漏）。
    - tv    → 缺失集 code 列表（如 ["S01E01"]）
    - movie → 已在库 []（无遗漏）；整部缺失 [None]（全量模式，episode=文件名）
    - tv 未收录 → 由配置项 scan_baseline_required 分流：
        True（强防重，旧行为）→ None（主流程本轮跳过，防盲入占中转空间）；
        False（默认软处理）→ [None]（全量模式等价表达：后续对每个搜索到的具体
        文件都视为缺失集入队——scan 搜索链 _walk_share 只收集具体文件并受
        max_files 与大小过滤约束，「盲入整部剧」顾虑已大幅缓解）。
    """
    emby_id = await emby.find_emby_id(media.tmdb_id, media.title)  # P11 二次模糊兜底内置
    if media.media_type == "movie":
        return [] if emby_id else [None]
    if not emby_id:
        # Emby 未收录该剧集 → 防重基线缺失（本函数是唯一返回 None 的来源）
        required = as_bool(
            config_store.get("scan_baseline_required", settings.SCAN_BASELINE_REQUIRED)
        )
        if required:
            logger.info(
                "[scan] media=%s Emby 未收录该剧集，防重基线强制（scan_baseline_required=True），本轮跳过",
                media.id,
            )
            return None
        logger.info(
            "[scan] media=%s Emby 未收录该剧集，防重基线缺失按全量模式处理（scan_baseline_required=False），搜索到的具体文件均视为缺失集",
            media.id,
        )
        return [None]
    missing = await emby.get_missing_episodes(emby_id)
    return [ep.get("code") for ep in missing if ep.get("code")]


async def _resolve_done_states(media, missing_keys: set[str], movie_missing: bool) -> None:
    """done 防重解除（P1-1，Oracle 审查）：Emby 二次确认入库 → 解除防重；仍未入库 → 转 failed 人工确认。

    在 Emby 基线之后、搜索/入队之前执行（_scan_one 步骤 2b），对该 media 全部
    state='done' 的 episode_state 逐条判定：

    - tv 模式：episode key 不在 missing_keys → Emby 已确认入库 → 删除 es/tq/dl（解除防重，
      允许后续搜索重新入队）；仍在 missing_keys（Emby 仍缺失）→ 未入库 → 条件更新转 failed。
    - movie 全量模式：episode=文件名。movie_missing=True（Emby 整部缺失）→ 未入库 → 转 failed；
      movie 已入库（missing=[]，movie_missing=False）→ 全部视为确认 → 删除。

    P3-1（延后项）done→failed 循环上限：转 failed 消耗一次 retry_count（SQL 表达式 +1），
    条件 `retry_count < _DONE_FAIL_RETRY_LIMIT`（对齐 transfer._RETRY_LIMIT=3）——未达上限
    才转 failed 并联动 tq；已达上限的 done 记录保持 done（不转 failed、不删除、防重保留），
    仅更新 error 说明（上限 error 文案），杜绝「人工 retry → 转存 done → 转 failed」无限循环。
    上限分支不通知（巡检避免噪音），task_run 由 _scan_one 主流程统一记录。

    全程条件删除/更新（WHERE 当前状态）不影响其他数据；异常由调用方 try/except 兜底，不阻断巡检。
    """
    async with async_session() as tx:
        async with tx.begin():
            done_rows = (
                (
                    await tx.execute(
                        select(EpisodeState).where(
                            EpisodeState.media_id == media.id,
                            EpisodeState.state == "done",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not done_rows:
                return
            now = _now()
            confirmed = 0
            to_retry = 0
            at_limit = 0
            for es in done_rows:
                if movie_missing or es.episode in missing_keys:
                    if es.retry_count >= _DONE_FAIL_RETRY_LIMIT:
                        # P3-1：已达循环上限 → 不转 failed、不删除，保持 done 状态仅写 error
                        # 供人工核实（防重保留，避免无限循环）。
                        # m1（Oracle Gate2）：error 已是上限文案的记录不再重复写（无效写 +
                        # 污染 updated_at），下一轮 select 命中后 update 无实际改写、保持干净。
                        await tx.execute(
                            update(EpisodeState)
                            .where(
                                EpisodeState.media_id == media.id,
                                EpisodeState.episode == es.episode,
                                EpisodeState.state == "done",
                                EpisodeState.retry_count >= _DONE_FAIL_RETRY_LIMIT,
                                or_(
                                    EpisodeState.error.is_(None),
                                    EpisodeState.error != _DONE_LIMIT_ERROR,
                                ),
                            )
                            .values(
                                error=_DONE_LIMIT_ERROR,
                                updated_at=now,
                            )
                        )
                        at_limit += 1
                        continue
                    # Emby 仍未入库且未达上限 → 转 failed 进人工 retry 路径（防重保留）；
                    # P3-1：done→failed 算一次循环，retry_count SQL 自增（对齐 transfer 上限语义）
                    r_es = await tx.execute(
                        update(EpisodeState)
                        .where(
                            EpisodeState.media_id == media.id,
                            EpisodeState.episode == es.episode,
                            EpisodeState.state == "done",
                            EpisodeState.retry_count < _DONE_FAIL_RETRY_LIMIT,
                        )
                        .values(
                            state="failed",
                            error=_DONE_FAIL_ERROR,
                            retry_count=EpisodeState.retry_count + 1,
                            updated_at=now,
                        )
                    )
                    if r_es.rowcount > 0:
                        # 仅在 es 成功转 failed 时联动 tq（P3-1 rowcount 门控：上限分支不动 tq）
                        await tx.execute(
                            update(TransferQueue)
                            .where(
                                TransferQueue.media_id == media.id,
                                TransferQueue.episode == es.episode,
                                TransferQueue.status == "done",
                            )
                            .values(
                                status="failed",
                                error=_DONE_FAIL_ERROR,
                                updated_at=now,
                            )
                        )
                        to_retry += 1
                else:
                    # Emby 已确认入库 → 解除防重：删除 es / tq / dl（条件删除，P1-4：
                    # 与同块 update(WHERE state='done') 对称，防阶段 4 新路径误删
                    # downloading/transferring 等非 done 记录）
                    await tx.execute(
                        delete(EpisodeState).where(
                            EpisodeState.media_id == media.id,
                            EpisodeState.episode == es.episode,
                            EpisodeState.state == "done",
                        )
                    )
                    await tx.execute(
                        delete(TransferQueue).where(
                            TransferQueue.media_id == media.id,
                            TransferQueue.episode == es.episode,
                            TransferQueue.status == "done",
                        )
                    )
                    await tx.execute(
                        delete(DownloadTask).where(
                            DownloadTask.media_id == media.id,
                            DownloadTask.episode == es.episode,
                            DownloadTask.status == "complete",
                        )
                    )
                    confirmed += 1
            logger.info(
                "[scan] media=%s done 防重解除：Emby 确认入库删除 %d 条 / 未入库转 failed %d 条 / 达循环上限保持 done %d 条",
                media.id, confirmed, to_retry, at_limit,
            )


# ---------------------------------------------------------------------------
# cloudSaver 搜源 / 分享凭据 / 文件列表
# ---------------------------------------------------------------------------

def _expand_share_codes(results: list[dict]) -> list[dict]:
    """从 search 结果展开 quark 分享码候选：{title, share_code}（P8 正则提取）。"""
    out: list[dict] = []
    for item in results:
        title = item.get("title") or ""
        for cl in item.get("cloud_links") or []:
            if (cl.get("cloud_type") or "").lower() != "quark":
                continue
            m = _SHARE_CODE_RE.search(str(cl.get("link") or ""))
            if m:
                out.append({"title": str(title), "share_code": m.group(1)})
    return out


async def _cloudsaver_share_info(share_code: str) -> dict:
    """share-info（逐码 500ms 间隔由调用方控制）。返回转存凭据字典。"""
    info = await cloudsaver.share_info(share_code)
    return info if isinstance(info, dict) else {}


def _as_flag(v) -> bool:
    """share-list 条目布尔字段容错解析（isFolder 可能是 bool/数字/字符串）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return bool(v)


async def _walk_share(share_code: str, info: dict, max_depth: int = 4,
                      max_files: int = 300) -> list[dict]:
    """递归遍历分享目录树（对齐 n8n quarkRecursiveGetFiles；阶段 1 Q2/Q3 实证结论 2/3）。

    分享源多为文件夹结构（阶段 1 附 2），原 _share_files 仅列根目录，三重匹配无法命中
    文件夹内具体集；且生产版 cloudSaver 对「文件夹 fid」不落盘，转存必须落到具体文件。
    本函数从根 share_list(pdir_fid="") 开始逐目录下钻，收集全部具体文件：

        返回 [{file_name, file_size, is_folder=False, file_id, fid_token, path}]，
        path 为相对路径（如 "季目录/第1集.mkv"；根目录文件即文件名）。

    遍历上限：
        - 收集文件数达到 max_files → 停止整棵遍历（防 178G 巨型分享拖垮，阶段 1 附 1）；
        - 深度 > max_depth → 停止下钻；单次 share-list 失败 try/except 跳过该目录继续。
    服务端友好：每条 share-list 调用之间 asyncio.sleep(0.3)。

    size 处理（递归内层文件 size 字段可能缺失，阶段 1 Q2 结论 3）：
        - 优先取 it.size / it.fileSize；缺失（0 或 None）时：
          a) 该分享顶层仅 1 个条目（单文件/单文件夹分享）且 share-info 有 fileSize → 用分享总大小；
          b) 否则保留 size=0 并标记 size_unknown=True（fail-closed：未知大小不参与转存决策）。
    """
    top_count = 0  # 根目录条目总数（share-info fileSize 兜底的判定前提）
    top_done = False
    files: list[dict] = []
    stopped = False
    # P2-12（延后项）：已下钻目录判重（按 pdir_fid 字符串），
    # 防分享目录循环引用 / share-list 异常重复返回导致死递归（根目录 "" 一并入集）
    visited: set[str] = set()
    # P2-5（Oracle 审查）：share-info 文件总大小字段名容错（fileSize/file_size/size/totalSize）
    share_size = int(info.get("fileSize") or info.get("file_size")
                     or info.get("size") or info.get("totalSize") or 0)

    async def list_dir(pdir_fid: str, depth: int, rel_path: str) -> None:
        nonlocal top_count, top_done, stopped
        if stopped:
            return
        if pdir_fid in visited:
            logger.warning(
                "[scan] %s 目录 %s 已遍历（循环引用或重复），跳过下钻 %s",
                share_code, pdir_fid, f"/{rel_path}" if rel_path else "",
            )
            return
        visited.add(pdir_fid)
        try:
            data = await cloudsaver.share_list(
                share_code,
                pdir_fid=pdir_fid,
                pwd_id=info.get("pwd_id") or info.get("pwdId") or "",
                stoken=info.get("stoken") or "",
                receive_code=info.get("receive_code") or info.get("receiveCode") or "",
            )
        except Exception as exc:
            logger.warning(
                "[scan] share-list %s%s 失败（跳过该目录）: %s",
                share_code, f"/{rel_path}" if rel_path else "", exc,
            )
            return
        await asyncio.sleep(0.3)  # 服务端友好：目录间 300ms 间隔（P3-4 成功分支末尾，移出 finally）

        data = data if isinstance(data, dict) else {}
        items: list[dict] = [
            x for x in (data.get("list") or []) if isinstance(x, dict)
        ]
        if not top_done:
            top_count = len(items)
            top_done = True

        for it in items:
            if stopped:
                break
            name = str(it.get("fileName") or it.get("name") or "").strip()
            if not name:
                continue
            path = f"{rel_path}/{name}" if rel_path else name
            if _as_flag(it.get("isFolder") or it.get("is_folder")):
                if depth + 1 > max_depth:
                    logger.info("[scan] %s 深度 %s 超限，停止下钻 %s", share_code, depth + 1, path)
                    continue
                await list_dir(str(it.get("fileId") or it.get("file_id") or ""), depth + 1, path)
                continue

            file_id = it.get("fileId") or it.get("file_id")
            if not file_id:
                logger.warning("[scan] %s 条目不携带 fileId，跳过: %s", share_code, path)
                continue
            size = int(it.get("size") or it.get("fileSize") or 0)
            size_unknown = size <= 0
            if size_unknown and top_count == 1 and share_size > 0:
                size = share_size  # 单条目分享：文件缺失大小用分享总大小兜底
                size_unknown = False
            files.append({
                "file_name": name,
                "file_size": size,
                "is_folder": False,
                "file_id": str(file_id),
                "fid_token": str(it.get("fileIdToken") or it.get("fid_token") or ""),
                "path": path,
                "size_unknown": size_unknown,
            })
            if len(files) >= max_files:
                stopped = True
                logger.info("[scan] %s 文件数达上限 %s，停止遍历", share_code, max_files)

    await list_dir("", 0, "")
    logger.info("[scan] share %s 递归遍历完成：%d 个文件", share_code, len(files))
    return files


def _enqueue_payload(info: dict, f: dict) -> dict:
    """构造入队凭据（G1 修复 + Q2 实证结论 1：一律逐文件 fids）。

    阶段 1 实证：生产版 cloudSaver 仅对分享内**具体文件**的 fileId/fileIdToken 落盘，
    文件夹 fid 不被受理（Q2 结论 1）。递归遍历后拿到的一定是具体文件 → 转存目标一律用
    当前文件的 fids=[file_id] / fid_tokens=[fid_token]；info 顶层 fids/fidTokens/folder_id
    不再使用（它们只有代表文件层时才有意义，递归后已无此场景）。
    pwd_id / stoken / receive_code 继续取自 share-info。
    """
    return {
        "pwd_id": info.get("pwd_id") or info.get("pwdId") or "",
        "stoken": info.get("stoken") or "",
        "receive_code": info.get("receive_code") or info.get("receiveCode") or "",
        "fids": [f["file_id"]],
        "fid_tokens": [f.get("fid_token") or ""],
    }


# ---------------------------------------------------------------------------
# 搜索关键词 / 加分匹配（P5 保留）
# ---------------------------------------------------------------------------

def _season_of_key(key: str) -> int:
    m = re.match(r"[Ss](\d{1,2})", key or "")
    return int(m.group(1)) if m else 1


def _build_keywords(media, missing_keys: set[str]) -> list[str]:
    """搜索关键词：movie 用标题；tv 按缺失集所在季聚合（P4：一次巡检每个关键词只搜一次）。"""
    title = (media.title or "").strip()
    if not title:
        return []
    if media.media_type == "movie":
        return [title]
    seasons = sorted({_season_of_key(k) for k in missing_keys})
    return [f"{title} S{se:02d}" for se in seasons] if seasons else [title]


def _title_words(title: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", title or "") if len(w) >= 2]


def _rank_candidates(media, items: list[dict]) -> list[dict]:
    """加分匹配排序：标题精确包含高分，部分词命中加分（忽略空格/大小写）。"""
    title_norm = (media.title or "").replace(" ", "").lower()
    words = _title_words(media.title)
    scored = []
    for it in items:
        name = str(it.get("title") or "").replace(" ", "").lower()
        score = 0
        if title_norm and title_norm in name:
            score += 10
        score += sum(2 for w in words if w in name)
        scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored]


async def _search_and_rank(media, missing_keys: set[str]) -> list[dict]:
    """cloudSaver 搜索 → 展开分享码 → 加分匹配 → 限数 20。单关键词故障不中断整轮。"""
    raw: list[dict] = []
    for kw in _build_keywords(media, missing_keys):
        try:
            raw.extend(await cloudsaver.search(kw))
        except Exception as exc:
            logger.warning("[scan] cloudSaver 搜索 %s 失败: %s", kw, exc)
    expanded = _expand_share_codes(raw)
    return _rank_candidates(media, expanded)[:20]


# ---------------------------------------------------------------------------
# 大小过滤（§6.1）：media 覆盖 > system_config 全局 > settings 默认
# ---------------------------------------------------------------------------

async def _size_limits(session, media) -> tuple[float, float]:
    global_ep = await get_config_value(
        session, "max_episode_size_gb", settings.DEFAULT_MAX_EPISODE_SIZE_GB
    )
    global_movie = await get_config_value(
        session, "max_movie_size_gb", settings.DEFAULT_MAX_MOVIE_SIZE_GB
    )
    ep = media.max_episode_size_gb if media.max_episode_size_gb is not None else float(global_ep)
    movie = media.max_movie_size_gb if media.max_movie_size_gb is not None else float(global_movie)
    return ep, movie


async def _read_size_limits(media) -> tuple[float, float]:
    """独立短 session 读取大小过滤上限（P1-1：主流程网络 IO 阶段不持有 DB session）。

    内部自行开启/关闭短 session 读取 system_config，不依赖外层长事务；
    media 为 detached 对象（expire_on_commit=False），其覆盖值属性可直接读取。
    """
    async with async_session() as s:
        return await _size_limits(s, media)


def _size_limit_gb(media, ep_limit: float, movie_limit: float) -> float:
    return movie_limit if media.media_type == "movie" else ep_limit


# ---------------------------------------------------------------------------
# 入队（事务内「检查并写入」+ UNIQUE 冲突捕获）
# ---------------------------------------------------------------------------

def _json_dumps(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


async def _enqueue(media_id: int, episode_key: str, file_name: str, file_size: int,
                   share_code: str, payload: dict) -> str:
    """transfer_queue(pending) + episode_state(queued) 同时写入（§3.1 双表联动）。

    幂等：事务内先查 episode_state 防重态，命中则跳过；写入撞 UNIQUE(media_id, episode)
    则捕获 IntegrityError 判定为并发冲突。返回 'enqueued' / 'existing' / 'conflict'。
    """
    async with async_session() as tx:
        async with tx.begin():
            has = (
                await tx.execute(
                    select(EpisodeState.id).where(
                        EpisodeState.media_id == media_id,
                        EpisodeState.episode == episode_key,
                        EpisodeState.state.in_(_ACTIVE_STATES),
                    )
                )
            ).first()
            if has:
                return "existing"

            tx.add(EpisodeState(
                media_id=media_id,
                episode=episode_key,
                state="queued",
                file_name=file_name,
                file_size=file_size,
                share_code=share_code,
                retry_count=0,
                updated_at=_now(),
            ))
            tx.add(TransferQueue(
                media_id=media_id,
                episode=episode_key,
                file_name=file_name,
                file_size=file_size,
                share_code=share_code,
                status="pending",
                pwd_id=payload.get("pwd_id") or payload.get("pwdId"),
                stoken=payload.get("stoken"),
                # G4（Q3 双语义）：本字段存的是「提取码」（share-info 端点里的 passcode），
                # 来自 payload["receive_code"]；而 save 端点（POST /api/quark/save）的
                # receiveCode 语义 = **stoken**（阶段 1 实证）。transfer lane 消费时须取
                # 上面的 stoken 字段作为 save 的 receiveCode，勿将本字段直接透传 save。
                receive_code=payload.get("receive_code") or payload.get("receiveCode"),
                fids=_json_dumps(payload.get("fids")),
                fid_tokens=_json_dumps(payload.get("fid_tokens") or payload.get("fidTokens")),
                # 转存目标目录 folderId：share-info 无此字段时回退 QUARK_DEFAULT_FOLDER
                # （阶段 3 实证：folderId 为空 → cloudSaver 转存不落盘到 alist /quark）
                # Phase 8：改读 config_store（system_config 优先，env fallback，保存即生效）
                folder_id=payload.get("folder_id") or payload.get("folderId")
                or config_store.get("quark_default_folder", settings.QUARK_DEFAULT_FOLDER)
                or None,
                updated_at=_now(),
            ))
            try:
                await tx.commit()
                return "enqueued"
            except IntegrityError:
                await tx.rollback()
                # P3-6（Oracle 审查）：并发冲突后补查 tq 记录，双表不一致风险告警（不做自动修复）
                try:
                    has_tq = (
                        await tx.execute(
                            select(TransferQueue.id).where(
                                TransferQueue.media_id == media_id,
                                TransferQueue.episode == episode_key,
                            )
                        )
                    ).first() is not None
                except Exception:  # noqa: BLE001
                    has_tq = None
                logger.warning(
                    "[scan] media=%s episode=%s 并发冲突（UNIQUE），本轮跳过；tq 记录%s",
                    media_id, episode_key,
                    "存在（双表可能不一致，请人工核查）" if has_tq else "不存在",
                )
                return "conflict"


async def _trigger_transfer() -> None:
    """入队成功后触发转存队列消费（§4.4 process_transfer_queue 事件触发）。

    P1-6（council）：改为 fire-and-forget——原实现同步 await 完整转存链
    （cloudSaver save 受理后等落盘最长 180s + aria2 提交），阻塞 scan HTTP 请求；
    现用后台任务持引用防 GC，scan_media 立即返回 task_run_id。
    transfer 模块未就绪时静默跳过；trigger_transfer 内部已 try/except 全包。
    """
    try:
        from app.tasks import transfer as _t  # 延迟导入，避免子模块初始化时序

        trigger = getattr(_t, "trigger_transfer", None)
        if trigger is None:
            logger.debug("[scan] transfer.trigger_transfer 未就绪，跳过触发")
            return
        task = asyncio.create_task(trigger())
        _background.add(task)
        task.add_done_callback(_background.discard)
    except Exception:
        logger.exception("[scan] 触发 transfer 失败")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _media_lock(media_id: int) -> asyncio.Lock:
    return _scan_locks.setdefault(media_id, asyncio.Lock())


async def scan_media(media_id: int) -> int | None:
    """单影视巡检（API /api/media/{id}/scan 手动触发入口），返回最近一条 task_run id。

    P2-11（延后项）：巡检完成后清理 _scan_locks 中该 media 的锁，防 media 删除后
    锁对象永久泄漏。清理必须保证不破坏并发互斥（等待中的调用方不可被丢下）。

    安全判定（单线程事件循环）说明：finally 在 `async with lock` 块**内**执行，此刻
    当前协程仍持有锁（release 在 finally 之后），持有期间到达的并发调用方全部进入
    asyncio.Lock 内部 `_waiters` 队列——因此仅当 `_waiters` 为空（无等待者）时才移除：
      - 有等待者：保留锁。等待者仍持有该锁引用，释放后照常串行执行；后续到达者复用
        同一把锁继续排队，不会出现「新锁 + 旧锁」并发巡检同一 media。
      - 无等待者：唯一执行者已结束，移除后新调用方会新建锁，同样无并发。
    依赖 asyncio.Lock._waiters（CPython 3.14 实现，懒初始化为 None / deque of futures）；
    不用 lock.locked() 判断——它仅反映 _locked，与是否有人在等待无关，无法区分清理时机。
    """
    lock = _scan_locks.setdefault(media_id, asyncio.Lock())
    async with lock:
        try:
            return await _scan_one(media_id)
        finally:
            # 清理：无等待者时才移除（新调用方会重新创建，等待中的调用方仍持有旧锁引用）
            if not getattr(lock, "_waiters", None):
                _scan_locks.pop(media_id, None)


def _scan_interval_minutes(raw) -> float:
    """解析单部 media 巡检周期（分钟）。缺失/非法回落全局默认 settings.SCAN_INTERVAL_MINUTES。

    输入可为 media.scan_interval_minutes（int/None）或 system_config 字符串值（"60" 等）。
    """
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("[scan] scan_interval_minutes 非法值 %r，回落默认 %.0f", raw, settings.SCAN_INTERVAL_MINUTES)
    return float(settings.SCAN_INTERVAL_MINUTES)


async def scan_all_media_job() -> None:
    """定时 tick 包装（B 定时：每分钟；内部按 last_scan_at 到期过滤）。

    M1（Oracle Gate2）：与同模块其它 job 一致（transfer.process_transfer_queue_job 等），
    DB 读取/执行异常时记录 task_run(error) 兜底而不是静默抛出——APScheduler 会吞
    任务内异常，若不记录则作业持续失败运维不可见。task_type 用 "scan_all_media"
    （作业级，对齐 transfer/cleanup/notify 的作业级短名惯例；区别于单部巡检的
    "scan_media"）。
    """
    try:
        await scan_all_media()
    except Exception:  # noqa: BLE001
        logger.exception("[scan] scan_all_media 定时巡检异常")
        try:
            async with async_session() as s:
                await record_task_run(
                    s, "scan_all_media", "error", "定时巡检异常，见服务日志"
                )
                await s.commit()
        except Exception:  # noqa: BLE001  兜底记录失败只告警，不再外泄
            logger.exception("[scan] scan_all_media 异常记录失败")


async def scan_all_media(force: bool = False) -> None:
    """遍历全部 tracking/downloading 影视巡检（downloading 不跳过，防卡死，§3.1）。

    B 定时（阶段 4，§4.2 flow 2）：定时 tick（scan_all_media_job）默认按各 media
    `last_scan_at IS NULL OR last_scan_at + 周期 < now` 到期过滤——从未巡检
    （last_scan_at IS NULL）立即巡检；未到期跳过（不巡检、不写 task_run）。

    force=True（手动全量/CLI 入口）：跳过到期过滤，全部触及 media 一律巡检。

    周期取值优先级：media.scan_interval_minutes 覆盖值
    or system_config "scan_interval_minutes" or settings.SCAN_INTERVAL_MINUTES(60)。
    全局默认在短 session 中与 media 一次读取（detached 属性安全使用）。

    到期过滤仅作用于本全量遍历；scan_media 单部手动触发不做过期检查，语义不变。
    """
    async with async_session() as s:
        rows = (
            await s.execute(select(Media).where(Media.status.in_(("tracking", "downloading"))))
        ).scalars().all()
        # 全局默认巡检周期：system_config 优先，settings 兜底（media 各自可覆盖）
        global_interval = await get_config_value(
            s, "scan_interval_minutes", settings.SCAN_INTERVAL_MINUTES
        )
    now = _now()
    for media in rows:
        # B 定时到期检查：默认按 last_scan_at 到期过滤；force=True 全量不过滤（全部巡检）
        interval = _scan_interval_minutes(
            media.scan_interval_minutes
            if media.scan_interval_minutes is not None
            else global_interval
        )
        if not force and media.last_scan_at is not None \
                and media.last_scan_at + timedelta(minutes=interval) > now:
            continue
        try:
            await scan_media(media.id)
        except Exception:
            logger.exception("[scan] media=%s 巡检异常", media.id)


async def _record_scan_result(media_id: int, status: str, message: str,
                              *, touch_last_scan_at: bool = False) -> int | None:
    """短事务写一条 scan task_run（可选同步更新 media.last_scan_at）并 commit，返回 task_run id。

    P1-1（延后项）：各短路/结束分支的独立短 session，避免借用外层长事务——
    session 仅存在于此调用窗口，写完即释放；task_run 表记录（record_task_run 仅
    flush）与本调用内的一次 commit 一并落库。
    """
    async with async_session() as s:
        if touch_last_scan_at:
            await s.execute(
                update(Media).where(Media.id == media_id).values(last_scan_at=_now())
            )
        rid = await record_task_run(s, "scan_media", status, message, media_id)
        await s.commit()
        return rid


async def _scan_one(media_id: int) -> int | None:
    """严格按设计文档 §4.3 巡检主流程，阶段2 只到入队为止（不转存）。

    P1-1（延后项）：长事务拆分——Emby 基线 / 搜索 / share-info / share-list 递归
    等网络 IO 全程**不持有 DB session**（SQLite 单连接被长事务占住会阻塞其他写）；
    仅 DB 读写使用短事务（读 media、record_task_run、media.last_scan_at、
    _read_size_limits、_enqueue 均各自开启/关闭 session）。database.async_session
    expire_on_commit=False，开头短会话读出的 media 为 detached 对象，已加载属性
    （id/status/title/tmdb_id/media_type 等）可安全继续使用。
    """
    # 0. 短事务读取 media（立即关闭；detached 属性后续安全）
    async with async_session() as s:
        media = await s.get(Media, media_id)
    if media is None:
        logger.warning("[scan] media=%s 不存在", media_id)
        return None

    # 1. 状态预检：paused/error 跳过；downloading 不跳过（防卡死），仅本轮不入队
    if media.status in ("paused", "error"):
        rid = await _record_scan_result(
            media_id, "skipped", f"media.status={media.status}，跳过巡检"
        )
        return rid

    # 2. Emby 防重基线（网络 IO，无 DB session）
    try:
        missing = await _emby_missing_codes(media)
    except Exception as exc:
        # fail-safe（§4.3）：Emby 故障暂停新缺集发现，防止故障期重复转存/误占空间；
        # 既有 queued/failed 任务保留原样（阶段2 无转存逻辑，无需额外处理）
        rid = await _record_scan_result(
            media_id, "error",
            f"Emby 故障，fail-safe 暂停新缺集发现: {exc}",
            touch_last_scan_at=True,
        )
        return rid

    if missing is None:
        # 基线不可用：Emby 未收录该剧集，无法探明遗漏。
        # _emby_missing_codes 已按开关分流——仅当 scan_baseline_required=True（强防重，
        # 旧行为）才返回 None；False（默认）返回全量模式 [None]，走下方搜索入队路径。
        # 此处保留旧行为（本轮跳过），并区分文案以与全量模式日志区分。
        rid = await _record_scan_result(
            media_id, "skipped",
            "Emby 未收录该剧集，防重基线强制（scan_baseline_required=True），本轮跳过",
            touch_last_scan_at=True,
        )
        return rid

    missing_keys = {m for m in missing if m is not None}
    movie_missing = any(m is None for m in missing)

    # 2b. done 防重解除（P1-1，Oracle 审查）：Emby 二次确认入库 → 删除 es/tq/dl 解除防重；
    #     仍未入库 → 转 failed 人工确认。异常仅告警，不阻断巡检。
    #     _resolve_done_states 自带短 session，此处不涉及任何长事务。
    try:
        await _resolve_done_states(media, missing_keys, movie_missing)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scan] media=%s done 防重解除失败（不阻断巡检）: %s", media_id, exc)

    if not missing_keys and not movie_missing:
        # 无遗漏 → 短路结束（消灭 P3 空跑）
        rid = await _record_scan_result(
            media_id, "skipped", "无遗漏集（Emby 基线已覆盖），跳过",
            touch_last_scan_at=True,
        )
        return rid

    # 3. cloudSaver 搜索 → 展开分享码 → 加分匹配 → 限数 20（网络 IO，无 DB session）
    candidates = await _search_and_rank(media, missing_keys)

    # 4-6. share-info(500ms 串行) → share-list 递归遍历文件 → 大小过滤 → 三重匹配 → 入队
    #      （大小上限经独立短 session _read_size_limits 读取，网络 IO 阶段不持 DB session）
    ep_limit, movie_limit = await _read_size_limits(media)
    limit_gb = _size_limit_gb(media, ep_limit, movie_limit)
    skip_enqueue = media.status == "downloading"  # 有进行中任务本轮不入队，但仍检查遗漏

    enqueued = existing_skipped = size_filtered = unmatched = non_video = 0
    for cand in candidates:
        share_code = cand["share_code"]
        try:
            info = await _cloudsaver_share_info(share_code)
        except Exception as exc:
            logger.warning("[scan] share-info %s 失败: %s", share_code, exc)
            continue
        finally:
            await asyncio.sleep(0.5)  # 逐码 500ms 间隔串行（§4.3 步骤3）

        try:
            files = await _walk_share(share_code, info)
        except Exception as exc:
            logger.warning("[scan] share-list 递归遍历 %s 失败: %s", share_code, exc)
            continue

        for f in files:
            file_name = (f.get("file_name") or "").strip()
            if not file_name or f.get("is_folder"):
                continue

            # 视频扩展名过滤（对齐 n8n quarkRecursiveGetFiles 的 ALLOWED_EXTENSIONS={mp4,mkv}；
            # 阶段 3 真实验证发现 Cover.jpg 等非视频文件会被全量模式匹配入队，浪费中转空间）
            if not _is_video_file(file_name):
                non_video += 1
                continue

            file_size = int(f.get("file_size") or 0)

            # 三重匹配缺失集
            if movie_missing:
                matched_key = file_name  # 全量模式：episode=文件名（P9 已知权衡）
            else:
                matched_key = match_missing(file_name, missing_keys)
                if not matched_key:
                    unmatched += 1
                    continue

            # 大小过滤（§6.1，阶段 1 Q2 结论 3）：fail-closed——未知大小保守跳过，
            # 大小判断与容量判断都依赖 size，宁缺勿滥（对齐「未知容量不转存」策略）
            if f.get("size_unknown"):
                size_filtered += 1
                logger.info(
                    "[scan] media=%s %s 文件大小未知，保守跳过（fail-closed）",
                    media_id, file_name,
                )
                continue
            if limit_gb and file_size and file_size > limit_gb * 1024 ** 3:
                size_filtered += 1
                logger.info(
                    "[scan] media=%s %s 超限 %.2fG > %.1fG，跳过",
                    media_id, file_name, file_size / 1024 ** 3, limit_gb,
                )
                continue

            if skip_enqueue:
                existing_skipped += 1
                continue

            payload = _enqueue_payload(info, f)
            res = await _enqueue(media_id, matched_key, file_name, file_size, share_code, payload)
            if res == "enqueued":
                enqueued += 1
            else:
                existing_skipped += 1  # existing（防重命中）/ conflict（行级冲突）均视为跳过

    # 7. 记录 task_run + 更新 last_scan_at（独立短事务）
    parts = []
    if enqueued:
        parts.append(f"入队{enqueued}")
    if existing_skipped:
        parts.append(f"已有/跳过{existing_skipped}")
    if size_filtered:
        parts.append(f"大小过滤{size_filtered}")  # 含未知大小保守跳过与超限排除
    if unmatched:
        parts.append(f"未匹配{unmatched}")
    if non_video:
        parts.append(f"非视频{non_video}")
    message = "，".join(parts) or "无候选命中"
    rid = await _record_scan_result(
        media_id, "success" if enqueued else "skipped", message,
        touch_last_scan_at=True,
    )
    # 6b. 入队成功后触发转存消费（§4.4 事件触发；transfer lane 未就绪时静默跳过）
    #     fire-and-forget 在 DB session 外触发（_background 强引用集合防 GC）
    if enqueued:
        await _trigger_transfer()
    return rid

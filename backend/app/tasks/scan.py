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
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.models import DownloadTask, EpisodeState, Media, TransferQueue
from app.services import cloudsaver, emby
from app.tasks import get_config_value, record_task_run

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
    """
    emby_id = await emby.find_emby_id(media.tmdb_id, media.title)  # P11 二次模糊兜底内置
    if media.media_type == "movie":
        return [] if emby_id else [None]
    if not emby_id:
        return None  # Emby 未收录该剧集 → 无基线，本轮跳过（防盲入占 10G 中转空间）
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
            for es in done_rows:
                if movie_missing or es.episode in missing_keys:
                    # Emby 仍未入库 → 转 failed 进人工 retry 路径（防重保留）
                    await tx.execute(
                        update(EpisodeState)
                        .where(
                            EpisodeState.media_id == media.id,
                            EpisodeState.episode == es.episode,
                            EpisodeState.state == "done",
                        )
                        .values(
                            state="failed",
                            error="下载完成但 Emby 未入库，请人工确认",
                            updated_at=now,
                        )
                    )
                    await tx.execute(
                        update(TransferQueue)
                        .where(
                            TransferQueue.media_id == media.id,
                            TransferQueue.episode == es.episode,
                            TransferQueue.status == "done",
                        )
                        .values(
                            status="failed",
                            error="下载完成但 Emby 未入库，请人工确认",
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
                "[scan] media=%s done 防重解除：Emby 确认入库删除 %d 条 / 未入库转 failed %d 条",
                media.id, confirmed, to_retry,
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
    # P2-5（Oracle 审查）：share-info 文件总大小字段名容错（fileSize/file_size/size/totalSize）
    share_size = int(info.get("fileSize") or info.get("file_size")
                     or info.get("size") or info.get("totalSize") or 0)

    async def list_dir(pdir_fid: str, depth: int, rel_path: str) -> None:
        nonlocal top_count, top_done, stopped
        if stopped:
            return
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
                folder_id=payload.get("folder_id") or payload.get("folderId")
                or settings.QUARK_DEFAULT_FOLDER or None,
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
    """单影视巡检（API /api/media/{id}/scan 手动触发入口），返回最近一条 task_run id。"""
    async with _media_lock(media_id):
        return await _scan_one(media_id)


async def scan_all_media_job() -> None:
    """巡检 job 包装（阶段2 不注册定时，保留供手动调用）。"""
    await scan_all_media()


async def scan_all_media() -> None:
    """遍历全部 tracking/downloading 影视巡检（downloading 不跳过，防卡死，§3.1）。"""
    async with async_session() as s:
        rows = (
            await s.execute(select(Media).where(Media.status.in_(("tracking", "downloading"))))
        ).scalars().all()
    for media in rows:
        try:
            await scan_media(media.id)
        except Exception:
            logger.exception("[scan] media=%s 巡检异常", media.id)


async def _scan_one(media_id: int) -> int | None:
    """严格按设计文档 §4.3 巡检主流程，阶段2 只到入队为止（不转存）。"""
    async with async_session() as s:
        media = await s.get(Media, media_id)
        if media is None:
            logger.warning("[scan] media=%s 不存在", media_id)
            return None

        # 1. 状态预检：paused/error 跳过；downloading 不跳过（防卡死），仅本轮不入队
        if media.status in ("paused", "error"):
            rid = await record_task_run(
                s, "scan_media", "skipped", f"media.status={media.status}，跳过巡检", media_id
            )
            await s.commit()
            return rid

        # 2. Emby 防重基线
        try:
            missing = await _emby_missing_codes(media)
        except Exception as exc:
            # fail-safe（§4.3）：Emby 故障暂停新缺集发现，防止故障期重复转存/误占空间；
            # 既有 queued/failed 任务保留原样（阶段2 无转存逻辑，无需额外处理）
            rid = await record_task_run(
                s, "scan_media", "error",
                f"Emby 故障，fail-safe 暂停新缺集发现: {exc}", media_id,
            )
            await s.commit()
            return rid

        if missing is None:
            # 基线不可用：Emby 未收录该剧集，无法探明遗漏，本轮跳过（防盲入）
            rid = await record_task_run(
                s, "scan_media", "skipped",
                "Emby 未收录该剧集，防重基线不可用，本轮跳过", media_id,
            )
            await s.commit()
            return rid

        missing_keys = {m for m in missing if m is not None}
        movie_missing = any(m is None for m in missing)

        # 2b. done 防重解除（P1-1，Oracle 审查）：Emby 二次确认入库 → 删除 es/tq/dl 解除防重；
        #     仍未入库 → 转 failed 人工确认。异常仅告警，不阻断巡检。
        try:
            await _resolve_done_states(media, missing_keys, movie_missing)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[scan] media=%s done 防重解除失败（不阻断巡检）: %s", media_id, exc)

        if not missing_keys and not movie_missing:
            # 无遗漏 → 短路结束（消灭 P3 空跑）
            media.last_scan_at = _now()
            rid = await record_task_run(
                s, "scan_media", "skipped", "无遗漏集（Emby 基线已覆盖），跳过", media_id
            )
            await s.commit()
            return rid

        # 3. cloudSaver 搜索 → 展开分享码 → 加分匹配 → 限数 20
        candidates = await _search_and_rank(media, missing_keys)

        # 4-6. share-info(500ms 串行) → share-list 递归遍历文件 → 大小过滤 → 三重匹配 → 入队
        ep_limit, movie_limit = await _size_limits(s, media)
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

        # 7. 记录 task_run + 更新 last_scan_at
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
        media.last_scan_at = _now()
        rid = await record_task_run(
            s, "scan_media", "success" if enqueued else "skipped", message, media_id
        )
        await s.commit()
        # 6b. 入队成功后触发转存消费（§4.4 事件触发；transfer lane 未就绪时静默跳过）
        if enqueued:
            await _trigger_transfer()
        return rid

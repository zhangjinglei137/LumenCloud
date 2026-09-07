"""
L3：刮削执行 + 入库轮询（五节点状态机 scrape → library → done）落地。

- scrape_runner（刮削执行器）：
    由 transfer._complete_download 事件（_spawn）与 library_check job 兜底驱动。
    采集全部 node='scrape' 的集，一次 NasTools 全量目录同步
    （nastools_sync(force=True)：跳过冷却，强制登录/重启/等待/重跑全部分目录同步）
    服务所有等待刮削的集：
      - 成功 → 全部条件更新 node='scrape'→'library'（node_started_at=now、
        node_attempt=0；state 保持 downloading——library 阶段仍属「进行中」，
        scan._resolve_done_states 只处理 state='done'，不会把刮削/入库中的集当
        已入库删除/转 failed），进入入库轮询；
      - 异常 → 逐集 node_attempt++（CAS 条件更新防并发丢增量）：<3 保持
        node='scrape'（同节点重试，由后续事件/job 再触发），≥3 → node='failed'
        终态 + node_error 写 Nastools 异常详情。

- library_check（入库轮询）：APScheduler IntervalTrigger(seconds=30) job 驱动，
    采集全部 node='library' 的集，逐集判定 Emby 是否已收录：
      - emby.find_emby_id(media.tmdb_id, media.title) 命中：
          - 剧集（media_type != "movie"）→ 追加集级入库确认（P1-2）：
            emby.get_missing_episodes(emby_id) 查遗漏集，仅当「当前集不在遗漏集」
            才视为已收录 → node='done' + state='done' + node_finished_at=now +
            node_error 清空，删除夸克中转文件（es.quark_path → transfer._split_quark_path
            + alist.remove——G6 决策落地：「下载完成不删、入库确认后才删」；
            P2-5：删除前 alist.list_dir + transfer._find_real_name 匹配现实文件名，
            按真实名删除，匹配不到回退原始名），通知「入库完成」，触发
            transfer._sync_media_status 回退 media 状态（该 media 无其他进行中集 →
            tracking），并 _spawn 续跑 transfer（P2-6）；
            「当前集仍在遗漏集」→ 尚未被 Emby 收录，本轮不 finalize（保持 library
            等待，超时走 failed 逻辑）——防追更新集刮削后立即误判入库并删夸克；
            get_missing_episodes 抛异常（Emby 故障）→ 本轮跳过不误判（同上）；
          - 电影（media_type == "movie"）→ 无集级概念，find_emby_id 命中即 finalize
            （现状保留）；
      - 未命中 → 超时判定：node_started_at + timeout（system_config 键
        library_check_timeout_seconds，默认 600s）< now → node='failed' +
        node_error='入库超时：Emby 未收录，请人工核实刮削配置'，置 failed 成功后
        best-effort 清理夸克中转文件（P1-6，删除失败仅告警不阻塞）；未超时 → 继续等下一轮；
      - find_emby_id 抛异常（Emby 故障）→ 本轮跳过不误判（超时基于 node_started_at，
        跳过不消耗窗口）；
      - media 不存在/已删除 → 直接清理解除（删 es 行 + best-effort 删夸克文件）。

注意：本模块不 import app.tasks.transfer（避免 transfer ↔ 本模块循环导入），
对 transfer 的引用（_split_quark_path / _sync_media_status）一律在函数体内延迟导入。
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.database import async_session
from app.models import EpisodeState, Media
from app.services import alist, emby
from app.services.notifier import (
    EVENT_DOWNLOAD_COMPLETE,
    NotifyEvent,
    notifier,
)
from app.tasks import get_config_value, nastools_sync

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

# 刮削/入库节点级重试上限（与 transfer._RETRY_LIMIT 对齐：≥3 转 failed，需人工处理）
_RETRY_LIMIT = 3
# 入库等待超时默认值（秒）；system_config 键 library_check_timeout_seconds 覆盖
_LIBRARY_TIMEOUT_DEFAULT = 600
_TIMEOUT_CONFIG_KEY = "library_check_timeout_seconds"

# P3-3 同款：后台任务强引用集合（library_check_job 内 _spawn(scrape_runner) 用，
# 防 asyncio.create_task 的任务被 GC 回收未执行）
_background_tasks: set[asyncio.Task] = set()
# 刮削执行器互斥：事件触发与 job 兜底并发时，只允许一轮 NasTools 同步在跑。
# nastools_sync 内部 _sync_lock 已防 NasTools 双重启；此锁进一步避免重复的全量
# 同步（下载完成事件刚落，job 兜底又同步一轮属浪费）。
_scrape_lock = asyncio.Lock()


def _now() -> datetime:
    """统一时间源（naive UTC，与 tasks/__init__._now 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _spawn(coro_factory) -> None:
    """创建后台任务并持引用（与 transfer._spawn 同款；任务完成后从集合移除）。"""
    task = asyncio.create_task(coro_factory())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# ---------------------------------------------------------------------------
# 刮削执行器（scrape → library）
# ---------------------------------------------------------------------------

async def _run_scrape_once() -> None:
    """一次刮削执行（公平持 _scrape_lock；并发时后到者直接跳过不排队）。"""
    if _scrape_lock.locked():
        logger.info("[scrape] 另一轮刮削同步进行中，本轮跳过")
        return
    async with _scrape_lock:
        await _scrape_impl()


async def _scrape_impl() -> None:
    # 1) 待刮削集采集（node='scrape' 且 state='downloading' = 下载完成、等待刮削。
    #    P2-2：state 条件防把非正常流转的集（如异常置入 node='scrape' 的 failed/
    #    queued 残留）误采集/误推进）
    async with async_session() as s:
        pending = (
            (
                await s.execute(
                    select(
                        EpisodeState.media_id,
                        EpisodeState.episode,
                        EpisodeState.node_attempt,
                    ).where(
                        EpisodeState.node == "scrape",
                        EpisodeState.state == "downloading",
                    )
                )
            )
            .all()
        )
    if not pending:
        return

    # 2) 一次 NasTools 全量目录同步服务全部待刮削集（force=True 跳过冷却；
    #    失败路径在 nastools_sync 内已 task_run(error) + flow_error 通知，此处
    #    re-raise（force 分支）供执行器做节点级重试计数）
    try:
        await nastools_sync.nastools_sync(force=True)
    except Exception as exc:  # noqa: BLE001  Nastools 同步失败（force 路径向上暴露）
        logger.error("[scrape] Nastools 刮削同步失败: %s", exc)
        await _count_scrape_failure(pending, exc)
        return

    # 3) 成功：全部当前 node='scrape' 的集（含同步期间新进入的）→ library
    #    （P2-2：批量推进同样限定 state='downloading'，与采集条件一致）
    now = _now()
    async with async_session() as s:
        async with s.begin():
            await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.node == "scrape",
                    EpisodeState.state == "downloading",
                )
                .values(
                    node="library",
                    node_attempt=0,
                    node_started_at=now,   # library 节点开始
                    node_finished_at=now,  # scrape 节点完成
                    node_error=None,
                    updated_at=now,
                )
            )
    logger.info("[scrape] Nastools 刮削完成，%d 个集推进 node='library'", len(pending))


async def _count_scrape_failure(pending, exc) -> None:
    """刮削同步失败 → 逐集 node_attempt++（CAS）；<3 保持 scrape 重试 / ≥3 failed 终态。"""
    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    now = _now()
    detail = f"刮削同步失败（Nastools）: {exc}"
    async with async_session() as s:
        async with s.begin():
            for mid, episode, cur_attempt in pending:
                new_attempt = (cur_attempt or 0) + 1
                terminal = new_attempt >= _RETRY_LIMIT
                err = f"{detail}（node_attempt={new_attempt}/{_RETRY_LIMIT}）" if terminal else detail
                values: dict = {
                    "node": "failed" if terminal else "scrape",
                    "node_attempt": new_attempt,
                    "node_error": err,
                    "error": err,
                    # 非终态保持 scrape 重试：刷新节点起点；终态保留原起点 + 落完成点
                    "node_started_at": now if not terminal else EpisodeState.node_started_at,
                    "node_finished_at": now if terminal else None,
                    "updated_at": now,
                }
                if terminal:
                    values["state"] = "failed"  # 终态才动 state（旧字段双写）
                r = await s.execute(
                    update(EpisodeState)
                    .where(
                        EpisodeState.media_id == mid,
                        EpisodeState.episode == episode,
                        EpisodeState.node == "scrape",
                        EpisodeState.node_attempt == cur_attempt,  # CAS：防并发丢增量
                    )
                    .values(**values)
                )
                if r.rowcount == 0:
                    continue  # CAS 冲突（并发方已推进）→ 不重复计数
                if terminal:
                    # P3-6 同款：es 转 failed 终态后，若 media 无其他进行中集 → 回退 tracking
                    await transfer_mod._sync_media_status(mid, s)
            logger.warning(
                "[scrape] 刮削失败，已推进 %d 个集 node_attempt（≥%d 转 failed）",
                len(pending), _RETRY_LIMIT,
            )


async def scrape_runner() -> None:
    """刮削执行器：_complete_download 事件 _spawn 触发 + library_check job 兜底驱动。"""
    try:
        await _run_scrape_once()
    except Exception:  # noqa: BLE001  兜底防后台任务静默消亡（业务异常 _scrape_impl 已自处理）
        logger.exception("[scrape] scrape_runner 异常")


# ---------------------------------------------------------------------------
# 入库轮询（library → done / failed）
# ---------------------------------------------------------------------------

# P1-2 集级入库确认：es.episode（可能是规范 key 或文件名）与 Emby 遗漏集比对键，
# 参照 scan.py match_missing 的 SxxExx / SxxExxx / 第N集 三重匹配（Emby 遗漏集
# code 为 SxxExx 格式，两位集号，>99 保留三位）
_RE_SXXEXX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_RE_CN_EP = re.compile(r"第\s*(\d{1,3})\s*[集话]")


def _fmt_episode(season: int, ep: int) -> str:
    """规范化集 key：S01E01（两位）；三位集数（如 S01E100）保留三位（同 scan.py）。"""
    ep_s = f"E{ep:03d}" if ep >= 100 else f"E{ep:02d}"
    return f"S{season:02d}{ep_s}"


def _ep_num(key: str) -> int | None:
    m = re.search(r"E(\d{2,3})$", key or "")
    return int(m.group(1)) if m else None


def _episode_in_missing(episode: str, missing_codes: set[str]) -> bool:
    """当前集是否仍属 Emby 遗漏集（集级入库确认的判定核心）。

    返回 True  → 该集在遗漏列表（Emby 尚未收录）→ 本轮不 finalize，保持等待；
    返回 False → 该集不在遗漏列表（Emby 已收录 / 无遗漏集）→ 可 finalize。

    盲匹配策略：遗漏集列表为空 / episode 为空时返回 False（宁可 finalize 给
    done——find_emby_id 已确认剧集入库，此时才按集级确认放行）。
    """
    episode = (episode or "").strip()
    if not episode or not missing_codes:
        return False
    # 1) episode 本身就是遗漏 key（如 es.episode='S01E10'，含大小写/季集位差）
    if episode in missing_codes:
        return True
    # 2) 从文件名提取 SxxExx 规范化后比对（如 '剧名.S01E10.1080p.mkv'）
    m = _RE_SXXEXX.search(episode)
    if m:
        if _fmt_episode(int(m.group(1)), int(m.group(2))) in missing_codes:
            return True
    # 3) 第N集（无季号，跨季按集号匹配）
    m = _RE_CN_EP.search(episode)
    if m:
        ep = int(m.group(1))
        if any(_ep_num(k) == ep for k in missing_codes):
            return True
    return False


async def library_check() -> None:
    """入库轮询：轮询全部 node='library' 的 es，Emby 命中 → done+删夸克；超时 → failed。"""
    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(
                        EpisodeState.id,
                        EpisodeState.media_id,
                        EpisodeState.episode,
                        EpisodeState.file_name,
                        EpisodeState.quark_path,
                        EpisodeState.node_started_at,
                    ).where(EpisodeState.node == "library")
                )
            )
            .all()
        )
    if not rows:
        return

    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    timeout_seconds = await _read_timeout_seconds()
    now = _now()
    for es_id, media_id, episode, file_name, quark_path, started_at in rows:
        # 1) media 校验：不存在/已删除 → 直接清理解除（孤儿 es）
        async with async_session() as s:
            media = await s.get(Media, media_id)
        if media is None:
            await _cleanup_orphan(es_id, quark_path)
            continue

        # 2) Emby 收录判定（tmdb_id 缺失或 Emby 故障 → 本轮跳过，不误判、不消耗超时窗口）
        if media.tmdb_id is None:
            logger.warning("[library_check] media=%s 缺 tmdb_id，本轮跳过等待", media_id)
            continue
        try:
            emby_id = await emby.find_emby_id(media.tmdb_id, media.title)
        except Exception as exc:  # noqa: BLE001  含 EmbyUnavailable（Emby 故障）
            logger.warning("[library_check] media=%s Emby 查询失败（本轮跳过，不误判）: %s", media_id, exc)
            continue

        if emby_id:
            # P1-2 集级入库确认：剧集在 find_emby_id 命中后仍需确认「当前集不在
            # Emby 遗漏集」才 finalize——追更新集刚刮削完仍是遗漏集，立即判入库会
            # 误删夸克中转文件（G6 决策的入库确认在集级粒度成立）。
            if (media.media_type or "").strip().lower() != "movie":
                try:
                    missing = await emby.get_missing_episodes(emby_id)
                except Exception as exc:  # noqa: BLE001  含 EmbyUnavailable（Emby 故障）
                    logger.warning(
                        "[library_check] media=%s Emby 遗漏集查询失败（本轮跳过，不误判）: %s",
                        media_id, exc,
                    )
                    continue
                missing_codes: set[str] = {
                    str(ep.get("code")) for ep in missing if ep.get("code")
                }
                if _episode_in_missing(episode, missing_codes):
                    logger.info(
                        "[library_check] media=%s %s 仍在 Emby 遗漏集（尚未收录），本轮保持等待",
                        media_id, episode,
                    )
                    continue
            await _finalize_done(es_id, media_id, episode, file_name, quark_path, transfer_mod)
            continue

        # 3) 未命中 → 超时判定（未超时下一轮继续等）
        await _mark_timeout_if_expired(
            es_id, media_id, episode, quark_path, started_at, timeout_seconds, now
        )


async def _read_timeout_seconds() -> float:
    """读取入库超时配置（system_config 键 library_check_timeout_seconds，默认 600s）。"""
    async with async_session() as s:
        raw = await get_config_value(s, _TIMEOUT_CONFIG_KEY, _LIBRARY_TIMEOUT_DEFAULT)
    try:
        return float(raw or _LIBRARY_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return float(_LIBRARY_TIMEOUT_DEFAULT)


async def _cleanup_orphan(es_id: int, quark_path: str | None) -> None:
    """media 不存在/已删除 → 清理解除：best-effort 删夸克文件 + 删除孤儿 es 行。"""
    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    await _remove_quark_files(transfer_mod, quark_path)
    async with async_session() as s:
        async with s.begin():
            await s.execute(delete(EpisodeState).where(EpisodeState.id == es_id))


async def _remove_quark_files(transfer_mod, quark_path: str | None) -> None:
    """best-effort 删除夸克中转文件（入库确认 / 超时 failed / 孤儿清理共用）。

    P2-5：删除前先 alist.list_dir 列目录 + transfer._find_real_name 归一化匹配
    现实文件名（夸克可能对文件名规范化改名，按真实名删除才不残留）；列目录失败
    或匹配不到 → 回退按原始 quark_path 删除（best-effort）。
    删除失败仅告警不阻塞调用方（残留由清理兜底/人工处理）。
    """
    if not quark_path:
        return
    dir_part, names = transfer_mod._split_quark_path(quark_path)
    if not names:
        return
    target = names[0]
    try:
        entries = await alist.list_dir(dir_part.rstrip("/") or "/")
        real = transfer_mod._find_real_name(entries, names[0])
        if real:
            target = real
    except Exception as exc:  # noqa: BLE001  列目录/匹配失败 → 按原始名回退删除
        logger.warning("[library_check] 列目录匹配夸克真实名失败（按原始名删除）: %s", exc)
    try:
        await alist.remove([target], dir_part)
        logger.info("[library_check] 删除夸克中转文件: %s%s", dir_part, target)
    except Exception as exc:  # noqa: BLE001  删除失败仅告警（残留由清理兜底/人工处理）
        logger.warning("[library_check] 删除夸克文件失败: %s", exc)


async def _finalize_done(es_id, media_id, episode, file_name, quark_path, transfer_mod) -> None:
    """Emby 已收录 → done：先删夸克（网络 IO 事务外，失败不阻断），再条件更新置
    done + 同事务 _sync_media_status，最后「入库完成」通知 + 触发转存续跑。

    顺序说明：删除在前、置 done 在后——若删除成功但置 done 失败（CAS/并发），下轮
    仍 node='library' 可重试删除（alist.remove 幂等）；反之若先置 done 后删失败则
    文件残留且不再有重试机会。置 done 用 WHERE node='library' 条件更新保证幂等
    （并发/重复轮询不重复删、不重复通知）。

    P2-5：删夸克前先列目录按真实名删除（transfer._find_real_name 匹配），匹配不到
    回退原始 quark_path（_remove_quark_files 内实现）。
    P2-6：末尾触发转存续跑（_spawn(process_transfer_queue)），让等待容量的 pending
    立即释放（与 transfer._complete_download 续跑对称）。
    """
    await _remove_quark_files(transfer_mod, quark_path)

    now = _now()
    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(EpisodeState)
                .where(EpisodeState.id == es_id, EpisodeState.node == "library")
                .values(
                    state="done",
                    node="done",
                    node_finished_at=now,  # library 节点完成
                    node_error=None,
                    updated_at=now,
                )
            )
            if r.rowcount == 0:
                return  # 已被并发方推进/回退 → 不重复通知、不重复触发 media 回退
            # P3-6：该 media 无其他进行中集（queued/transferring/downloading）→ tracking
            await transfer_mod._sync_media_status(media_id, s)

    await notifier.notify(NotifyEvent(
        event_type=EVENT_DOWNLOAD_COMPLETE,
        title=f"入库完成: {episode}",
        body=f"媒体 {media_id} · {episode}（{file_name or ''}）已确认被 Emby 收录，"
             f"夸克中转文件已释放。",
        recipient=None,
        extra={"media_id": media_id, "episode": episode},
    ))

    # P2-6：入库完成（该 media 可能刚释放容量/状态流转）→ 触发转存续跑，
    # 让等待容量的 pending 立即释放（fire-and-forget，与 _complete_download 对称）
    try:
        _spawn(transfer_mod.process_transfer_queue)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[library_check] 入库完成后转存续跑触发失败: %s", exc)


async def _mark_timeout_if_expired(es_id, media_id, episode, quark_path, started_at,
                                   timeout_seconds, now) -> None:
    """Emby 未收录 → 超时判定；node_started_at 缺失（旧数据）保守不判定，继续等待。

    P1-6：置 failed 成功后 best-effort 清理夸克中转文件（释放中转空间）；删除失败
    仅告警不阻塞（残留由人工核实/清理兜底）。清理放事务提交后（网络 IO 不进事务）。
    """
    if started_at is None:
        return
    if started_at + timedelta(seconds=timeout_seconds) >= now:
        return  # 未超时，下一轮再查
    err = "入库超时：Emby 未收录，请人工核实刮削配置"
    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(EpisodeState)
                .where(EpisodeState.id == es_id, EpisodeState.node == "library")
                .values(
                    state="failed",
                    node="failed",
                    node_finished_at=now,
                    node_error=err,
                    error=err,
                    updated_at=now,
                )
            )
            if r.rowcount == 0:
                return  # 已被并发方推进 → 不触发 media 回退
            # P3-6：es 转 failed 终态后，若 media 无其他进行中集 → 回退 tracking
            await transfer_mod._sync_media_status(media_id, s)
    # P1-6：failed 已落库（事务提交）→ best-effort 清理夸克中转文件
    await _remove_quark_files(transfer_mod, quark_path)
    logger.warning("[library_check] %s %s", episode, err)


async def library_check_job() -> None:
    """APScheduler job 包装（IntervalTrigger(seconds=30)）。

    兜底驱动刮削执行器（覆盖下载完成事件之外的场景：重启恢复 / 上次同步失败重试；
    _run_scrape_once 内部空跑检测，无 node='scrape' 的集时零开销）+ 入库轮询。
    异常不外泄，不影响调度器其余 job。
    """
    try:
        _spawn(scrape_runner)
    except Exception:  # noqa: BLE001
        logger.exception("[library_check] 刮削执行器触发失败")
    try:
        await library_check()
    except Exception:  # noqa: BLE001
        logger.exception("[library_check] 入库轮询异常")

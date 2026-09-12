"""
L3：刮削执行 + 入库轮询（影视下载两队列重设计 §4.2：scrape → library → done）。

操作对象：download_queue 单表（status='scrape' / status='library'，承接旧 episode_state
node 维度的职责）。

- scrape_runner（刮削执行器）：
    由 transfer 下载完成事件（_spawn）与 library_check job 兜底驱动。
    采集全部 status='scrape' 的下载任务，一次 NasTools 全量目录同步
    （nastools_sync(force=True)：跳过冷却，强制登录/重启/等待/重跑全部分目录同步）
    服务所有等待刮削的任务：
      - 成功 → 全部条件更新 status='scrape'→'library'（node_started_at=now、
        node_attempt=0），进入入库轮询；
      - 异常 → 逐条 node_attempt++（CAS 条件更新防并发丢增量）：<3 保持
        status='scrape'（同节点重试，由后续事件/job 再触发），≥3 → status='failed'
        终态 + node_error 写 Nastools 异常详情。

- library_check（入库轮询）：APScheduler IntervalTrigger(seconds=30) job 驱动，
    采集全部 status='library' 的下载任务，逐条判定 Emby 是否已收录：
      - emby.find_emby_id(media.tmdb_id, media.title) 命中：
          - 剧集（media_type != "movie"）→ 追加集级入库确认（P1-2）：
            emby.get_missing_episodes(emby_id) 查遗漏集，仅当「当前集不在遗漏集」
            才视为已收录 → status='done' + node_finished_at=now +
            node_error 清空，删除夸克中转文件（quark_path → transfer._split_quark_path
            + alist.remove——G6 决策落地：「下载完成不删、入库确认后才删」；
            P2-5：删除前 alist.list_dir + transfer._find_real_name 匹配现实文件名，
            按真实名删除，匹配不到回退原始名），通知「入库完成」，触发
            transfer._sync_media_status 回退 media 状态（该 media 无其他进行中集 →
            tracking），并 _spawn 续跑下载队列（P2-6）；
            「当前集仍在遗漏集」→ 尚未被 Emby 收录，本轮不 finalize（保持 library
            等待，超时走 failed 逻辑）——防追更新集刮削后立即误判入库并删夸克；
            get_missing_episodes 抛异常（Emby 故障）→ 本轮跳过不误判（同上）；
          - 电影（media_type == "movie"）→ 无集级概念，find_emby_id 命中即 finalize
            （现状保留）；
      - 未命中 / 持续在遗漏集 / 缺 tmdb_id → 超时判定：node_started_at + timeout
        （system_config 键 library_check_timeout_seconds，默认 600s）< now →
        status='failed' + node_error='入库超时：<原因>，请人工核实刮削/收录配置'
        （原因区分 Emby 未收录 / 持续在遗漏集 / 缺 tmdb_id），置 failed 成功后
        best-effort 清理夸克中转文件（P1-6，删除失败仅告警不阻塞）；未超时 →
        继续等下一轮；
      - find_emby_id 抛异常（Emby 故障）→ 本轮跳过不误判（超时基于 node_started_at，
        跳过不消耗窗口）；
      - media 不存在/已删除 → 直接清理解除（删 download_queue 行 + best-effort 删夸克文件）。

注意：本模块不 import app.tasks.transfer（避免 transfer ↔ 本模块循环导入），
对 transfer 的引用（_split_quark_path / _sync_media_status / 下载队列消费入口）一律
在函数体内延迟导入。
"""
import asyncio
import logging
import re
import time as _time
from datetime import timedelta

from sqlalchemy import delete, select, update

from app.database import async_session
from app.models import DownloadQueue, EpisodeState, Media
from app.services import alist, emby
from app.services.notifier import (
    EVENT_DOWNLOAD_COMPLETE,
    NotifyEvent,
    notifier,
)
from app.tasks import get_config_value, nastools_sync
# tasks 层公共纯函数（app.utils，仅标准库）：统一时间源与集级匹配函数
from app.utils import fmt_episode, now_utc_naive as _now, parse_episode_num

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

# 刮削节点级重试上限（与 transfer._RETRY_LIMIT 对齐：≥3 转 failed，需人工处理）
_RETRY_LIMIT = 3
# 入库等待超时默认值（秒）；system_config 键 library_check_timeout_seconds 覆盖
_LIBRARY_TIMEOUT_DEFAULT = 600
_TIMEOUT_CONFIG_KEY = "library_check_timeout_seconds"

# T8.1（design 8.1）：刮削失败退避——NasTools 故障（外部服务问题）时对该 media
# 设置进程内退避，窗口内（10min）不再重复 force sync。参照 transfer._alert_cooldown
# 模式：进程内共享 dict + monotonic 截止时间戳；单 worker 部署可靠（重启即失，
# 影响有限：至多多触发一次重试）。防「刮削连坐风暴」：外部故障下 job 每 30s tick
# 反复全量同步，若每次都批量累加 node_attempt，全部 scrape 行会快速冲刺 failed。
_SCRAPE_BACKOFF_SECONDS = 600.0
# media_id → 退避截止 monotonic 时间戳（seconds）；仅本次进程有效
_scrape_backoff: dict[int, float] = {}


def _media_in_scrape_backoff(media_id: int) -> bool:
    """该 media 是否处于刮削退避期（退避期内不参与本轮同步尝试）。

    过期条目顺带清理（惰性、每次判定 O(1)，防止 dict 随 media 增减无界增长）。
    """
    now_m = _time.monotonic()
    until = _scrape_backoff.get(media_id)
    if until is None:
        return False
    if until <= now_m:
        _scrape_backoff.pop(media_id, None)  # 已过期 → 放行并清理
        return False
    return True

# P3-3 同款：后台任务强引用集合（library_check_job 内 _spawn(scrape_runner) 用，
# 防 asyncio.create_task 的任务被 GC 回收未执行）
_background_tasks: set[asyncio.Task] = set()
# 刮削执行器互斥：事件触发与 job 兜底并发时，只允许一轮 NasTools 同步在跑。
# nastools_sync 内部 _sync_lock 已防 NasTools 双重启；此锁进一步避免重复的全量
# 同步（下载完成事件刚落，job 兜底又同步一轮属浪费）。
_scrape_lock = asyncio.Lock()
# Task 8：Emby 全库扫描互斥——transfer.finished 事件与 scrape 成功两条触发路径并发
# 时只允许一轮全库扫描在跑（全库扫描耗资源且重复触发无意义；并发触发还可能在 Emby
# 正在扫描时叠加请求）。锁在 _emby_refresh_impl 内公平持有，后到者直接跳过不排队。
_emby_refresh_lock = asyncio.Lock()


def _spawn(coro_factory) -> None:
    """创建后台任务并持引用（与 transfer._spawn 同款；任务完成后从集合移除）。"""
    task = asyncio.create_task(coro_factory())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _download_queue_consume(transfer_mod) -> object:
    """下载队列消费入口（queue-flow-rework Task 6 起）：优先事件消费入口
    trigger_transfer_consume（带防重入锁的 _admit_batch 消费轮），回退历史
    process_download_queue / process_transfer_queue 原名（兼容迁移过渡期）。"""
    return getattr(transfer_mod, "trigger_transfer_consume", None) or \
        getattr(transfer_mod, "process_download_queue", None) or \
        getattr(transfer_mod, "process_transfer_queue", None)


# ---------------------------------------------------------------------------
# Task 8：Emby 全库 Refresh 触发（NasTools 转移/刮削完成后加速入库）
# ---------------------------------------------------------------------------

async def _emby_refresh_impl() -> None:
    """执行 Emby 全库 Refresh（公平持 _emby_refresh_lock；并发时后到者跳过）。

    失败仅记告警不抛异常——Emby 扫描触发失败不阻断主链路，新文件收录仍由
    library_check 轮询兜底确认（对外 HTTP 失败面收敛在告警内，调用方不受影响）。
    """
    if _emby_refresh_lock.locked():
        logger.info("[emby-refresh] 另一轮全库扫描进行中，本轮跳过")
        return
    async with _emby_refresh_lock:
        try:
            await emby.refresh_library()
        except Exception as exc:  # noqa: BLE001  触发失败仅告警（library_check 轮询兜底确认入库）
            logger.warning("[emby-refresh] Emby 全库扫描触发失败（library_check 轮询兜底）: %s", exc)


def trigger_emby_refresh() -> None:
    """触发 Emby 全库 Refresh（fire-and-forget + 互斥锁防并发全库扫描）。

    调用方：_scrape_impl 刮削成功推进 scrape→library 后（本模块），与
    nastools_notify 的 transfer.finished 成功推进后（跨模块复用本函数）。
    触发失败仅告警，不阻断调用方主链路——Emby 收录确认仍由 library_check 轮询兜底。
    """
    try:
        _spawn(_emby_refresh_impl)
    except Exception as exc:  # noqa: BLE001  后台任务创建失败不阻断调用方
        logger.warning("[emby-refresh] 后台任务创建失败: %s", exc)


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
    # 1) 待刮削任务采集（status='scrape' = 下载完成、等待刮削）
    async with async_session() as s:
        pending = (
            (
                await s.execute(
                    select(
                        DownloadQueue.id,
                        DownloadQueue.media_id,
                        DownloadQueue.episode,
                        DownloadQueue.node_attempt,
                    ).where(DownloadQueue.status == "scrape")
                )
            )
            .all()
        )
    if not pending:
        return

    # T8.1：退避过滤——退避期内的 media 本轮不参与同步尝试（10min 内不重复
    # force sync）；「本轮实际尝试的任务」= 全部待刮削任务 − 退避中 media 的任务。
    try_pending = [row for row in pending if not _media_in_scrape_backoff(row.media_id)]
    if not try_pending:
        return

    # 2) 一次 NasTools 全量目录同步服务本轮实际尝试的待刮削集（force=True 跳过冷却；
    #    失败路径在 nastools_sync 内已 task_run(error) + flow_error 通知，此处
    #    re-raise（force 分支）供执行器做节点级重试计数）
    try:
        await nastools_sync.nastools_sync(force=True)
    except Exception as exc:  # noqa: BLE001  Nastools 同步失败（force 路径向上暴露）
        logger.error("[scrape] Nastools 刮削同步失败: %s", exc)
        # T8.1：同步失败（外部服务故障）与节点重试计数解耦——pending 只收窄为
        # 「每 media 本轮实际尝试的那一行代表」，不批量累加该 media 全部 scrape 行
        # node_attempt（防连坐风暴：NasTools 恢复后同步成功仍可一次性推进全部行）
        await _count_scrape_failure(_representative_rows(try_pending), exc)
        return

    # 3) 成功：全部当前 status='scrape' 的任务（含同步期间新进入的）→ library
    now = _now()
    async with async_session() as s:
        async with s.begin():
            await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.status == "scrape")
                .values(
                    status="library",
                    node_attempt=0,
                    node_started_at=now,   # library 节点开始
                    node_finished_at=now,  # scrape 节点完成
                    node_error=None,
                    updated_at=now,
                )
            )
    logger.info("[scrape] Nastools 刮削完成，%d 个任务推进 status='library'", len(pending))
    # Task 8：刮削成功推进 → fire-and-forget 触发 Emby 全库 Refresh，加速新文件入库
    # （互斥锁防并发全库扫描；失败仅告警，library_check 轮询兜底确认收录）
    trigger_emby_refresh()


def _representative_rows(rows) -> list:
    """每 media 只保留一行（本轮实际尝试的代表任务）。

    T8.1 同步失败与节点重试计数解耦的载体：NasTools 故障是外部服务问题，不属
    任何具体任务的过错——不批量累加该 media 全部 scrape 行 node_attempt（防连坐
    风暴），只对每 media 一行代表计数（该 media 持续失败的信号；<3 保持重试 /
    ≥3 转 failed 终态语义不变）。同步恢复成功后其余行仍可一次性推进 library。
    """
    seen: set[int] = set()
    out = []
    for row in rows:
        if row.media_id in seen:
            continue
        seen.add(row.media_id)
        out.append(row)
    return out


async def _count_scrape_failure(pending, exc) -> None:
    """刮削同步失败（NasTools 故障）→ 逐条 node_attempt++（CAS）并对涉及的
    media 设置进程内退避（10min 内不再重复 force sync）；<3 保持 scrape 重试 /
    ≥3 failed 终态。

    pending 只含「每 media 本轮实际尝试的那一行代表」（_scrape_impl 已收窄），
    外部服务故障不批量累加该 media 全部 scrape 行 node_attempt（design 8.1）。
    """
    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    now = _now()
    # T8.1：失败涉及的 media 全部设置进程内退避（monotonic 截止时间戳）
    now_m = _time.monotonic()
    for _dq_id, mid, _ep, _att in pending:
        _scrape_backoff[mid] = now_m + _SCRAPE_BACKOFF_SECONDS
    detail = f"刮削同步失败（Nastools）: {exc}"
    async with async_session() as s:
        async with s.begin():
            for dq_id, mid, episode, cur_attempt in pending:
                new_attempt = (cur_attempt or 0) + 1
                terminal = new_attempt >= _RETRY_LIMIT
                err = f"{detail}（node_attempt={new_attempt}/{_RETRY_LIMIT}）" if terminal else detail
                values: dict = {
                    "status": "failed" if terminal else "scrape",
                    "node_attempt": new_attempt,
                    "node_error": err,
                    "error": err,
                    # 非终态保持 scrape 重试：刷新节点起点；终态保留原起点 + 落完成点
                    "node_started_at": now if not terminal else DownloadQueue.node_started_at,
                    "node_finished_at": now if terminal else None,
                    "updated_at": now,
                }
                r = await s.execute(
                    update(DownloadQueue)
                    .where(
                        DownloadQueue.id == dq_id,
                        DownloadQueue.status == "scrape",
                        DownloadQueue.node_attempt == cur_attempt,  # CAS：防并发丢增量
                    )
                    .values(**values)
                )
                if r.rowcount == 0:
                    continue  # CAS 冲突（并发方已推进）→ 不重复计数
                if terminal:
                    # P3-6 同款：转 failed 终态后，若 media 无其他进行中集 → 回退 tracking
                    await transfer_mod._sync_media_status(mid, s)
            logger.warning(
                "[scrape] 刮削失败，已推进 %d 个任务 node_attempt（≥%d 转 failed）",
                len(pending), _RETRY_LIMIT,
            )


async def scrape_runner() -> None:
    """刮削执行器：下载完成事件 _spawn 触发 + library_check job 兜底驱动。"""
    try:
        await _run_scrape_once()
    except Exception:  # noqa: BLE001  兜底防后台任务静默消亡（业务异常 _scrape_impl 已自处理）
        logger.exception("[scrape] scrape_runner 异常")


# ---------------------------------------------------------------------------
# 入库轮询（library → done / failed）
# ---------------------------------------------------------------------------

# P1-2 集级入库确认：dq.episode（可能是规范 key 或文件名）与 Emby 遗漏集比对键，
# 参照 scan.py match_missing 的 SxxExx / SxxExxx / 第N集 三重匹配（Emby 遗漏集
# code 为 SxxExx 格式，两位集号，>99 保留三位）
_RE_SXXEXX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_RE_CN_EP = re.compile(r"第\s*(\d{1,3})\s*[集话]")

# 公共化（tasks 层 utils）：_fmt_episode / _ep_num 实现迁至 app.utils
# （library_check.py 与 scan.py 原实现完全一致），这里保留局部名称使调用点不变；
# _RE_SXXEXX / _RE_CN_EP 常量保留在本文件。
_fmt_episode = fmt_episode
_ep_num = parse_episode_num


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
    # 1) episode 本身就是遗漏 key（如 dq.episode='S01E10'，含大小写/季集位差）
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
    """入库轮询：轮询全部 status='library' 的 download_queue，Emby 命中 → done+删夸克；
    超时 → failed。"""
    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(
                        DownloadQueue.id,
                        DownloadQueue.media_id,
                        DownloadQueue.episode,
                        DownloadQueue.file_name,
                        DownloadQueue.quark_path,
                        DownloadQueue.node_started_at,
                    ).where(DownloadQueue.status == "library")
                )
            )
            .all()
        )
    if not rows:
        return

    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    timeout_seconds = await _read_timeout_seconds()
    now = _now()
    for dq_id, media_id, episode, file_name, quark_path, started_at in rows:
        # 1) media 校验：不存在/已删除 → 直接清理解除（孤儿 download_queue）
        async with async_session() as s:
            media = await s.get(Media, media_id)
        if media is None:
            await _cleanup_orphan(dq_id, quark_path)
            continue

        # 2) Emby 收录判定（tmdb_id 缺失 → 纳入超时窗口：配置缺失长期不修不无限等待；
        #    Emby 故障 → 本轮跳过，不误判、不消耗超时窗口）
        if media.tmdb_id is None:
            await _mark_timeout_if_expired(
                dq_id, media_id, episode, quark_path, started_at,
                timeout_seconds, now,
                cause="media 缺 tmdb_id（配置缺失）",
            )
            continue
        try:
            emby_id = await emby.find_emby_id(media.tmdb_id, media.title)
        except Exception as exc:  # noqa: BLE001  含 EmbyUnavailable（Emby 故障）
            logger.warning(
                "[library_check] media=%s %s Emby 收录查询失败（本轮跳过，不消耗超时）: %s",
                media_id, episode, exc,
            )
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
                        "[library_check] media=%s Emby 遗漏集查询失败（本轮跳过，不误判，不消耗超时）: %s",
                        media_id, exc,
                    )
                    continue
                missing_codes: set[str] = {
                    str(ep.get("code")) for ep in missing if ep.get("code")
                }
                if _episode_in_missing(episode, missing_codes):
                    # 卡死修复：遗漏集路径同样受超时窗口约束 —— 追更新集长期在遗漏集
                    # （Emby 刮削一直不收录）不再无限等待，超时走 failed 并记录原因。
                    await _mark_timeout_if_expired(
                        dq_id, media_id, episode, quark_path, started_at,
                        timeout_seconds, now,
                        cause="持续在 Emby 遗漏集（Emby 收录超时）",
                    )
                    continue
            await _finalize_done(dq_id, media_id, episode, file_name, quark_path, transfer_mod)
            continue

        # 3) 未命中 → 超时判定（未超时下一轮继续等）
        await _mark_timeout_if_expired(
            dq_id, media_id, episode, quark_path, started_at, timeout_seconds, now
        )


async def _read_timeout_seconds() -> float:
    """读取入库超时配置（system_config 键 library_check_timeout_seconds，默认 600s）。"""
    async with async_session() as s:
        raw = await get_config_value(s, _TIMEOUT_CONFIG_KEY, _LIBRARY_TIMEOUT_DEFAULT)
    try:
        return float(raw or _LIBRARY_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        return float(_LIBRARY_TIMEOUT_DEFAULT)


async def _cleanup_orphan(dq_id: int, quark_path: str | None) -> None:
    """media 不存在/已删除 → 清理解除：best-effort 删夸克文件 + 删除孤儿 download_queue 行。"""
    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    await _remove_quark_files(transfer_mod, quark_path)
    async with async_session() as s:
        async with s.begin():
            await s.execute(delete(DownloadQueue).where(DownloadQueue.id == dq_id))


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


async def _finalize_done(dq_id, media_id, episode, file_name, quark_path, transfer_mod) -> None:
    """Emby 已收录 → done：先删夸克（网络 IO 事务外，失败不阻断），再条件更新置
    done + 同事务 _sync_media_status，最后「入库完成」通知 + 触发下载队列续跑。

    顺序说明：删除在前、置 done 在后——若删除成功但置 done 失败（CAS/并发），下轮
    仍 status='library' 可重试删除（alist.remove 幂等）；反之若先置 done 后删失败则
    文件残留且不再有重试机会。置 done 用 WHERE status='library' 条件更新保证幂等
    （并发/重复轮询不重复删、不重复通知）。

    P2-5：删夸克前先列目录按真实名删除（transfer._find_real_name 匹配），匹配不到
    回退原始 quark_path（_remove_quark_files 内实现）。
    Task 6：末尾触发下载队列消费续跑（_spawn(trigger_transfer_consume)），让等待容
    量的 quota_wait 与新入队任务立即释放（与 transfer 下载完成续跑对称；防重入锁在
    transfer 侧）。
    """
    await _remove_quark_files(transfer_mod, quark_path)

    now = _now()
    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id, DownloadQueue.status == "library")
                .values(
                    status="done",
                    node_finished_at=now,  # library 节点完成
                    node_error=None,
                    updated_at=now,
                )
            )
            if r.rowcount == 0:
                return  # 已被并发方推进/回退 → 不重复通知、不重复触发 media 回退
            # episode-status-cache：同步 legacy episode_state（state='done' + file_size），
            # 保证旧表兼容与列表聚合一致；同 media+episode 已存在则更新不重复插。
            dq_row = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.id == dq_id)
            )).scalar_one_or_none()
            if dq_row is not None:
                es = (
                    await s.execute(
                        select(EpisodeState).where(
                            EpisodeState.media_id == media_id,
                            EpisodeState.episode == episode,
                        )
                    )
                ).scalar_one_or_none()
                if es is None:
                    s.add(EpisodeState(
                        media_id=media_id, episode=episode,
                        state="done", file_size=dq_row.file_size,
                        file_name=dq_row.file_name,
                    ))
                else:
                    es.state = "done"
                    es.file_size = dq_row.file_size
                    es.file_name = dq_row.file_name
            # P3-6：该 media 无其他进行中集 → tracking
            await transfer_mod._sync_media_status(media_id, s)

    await notifier.notify(NotifyEvent(
        event_type=EVENT_DOWNLOAD_COMPLETE,
        title=f"入库完成: {episode}",
        body=f"媒体 {media_id} · {episode}（{file_name or ''}）已确认被 Emby 收录，"
             f"夸克中转文件已释放。",
        recipient=None,
        extra={"media_id": media_id, "episode": episode},
    ))

    # queue-flow-rework Task 6：入库完成（该 media 可能刚释放容量/状态流转）→ 事件
    # 触发下载队列消费续跑，让等待容量的 quota_wait / 新入队任务立即释放（fire-and-
    # forget；防重入锁由 transfer.trigger_transfer_consume 侧持有）
    consume = _download_queue_consume(transfer_mod)
    if consume is not None:
        try:
            _spawn(consume)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[library_check] 入库完成后队列续跑触发失败: %s", exc)


async def _mark_timeout_if_expired(dq_id, media_id, episode, quark_path, started_at,
                                   timeout_seconds, now, cause: str = "Emby 未收录") -> None:
    """入库 / 遗漏集 / 配置缺失统一超时判定；node_started_at 缺失（旧数据）保守不判定。

    P1-6：置 failed 成功后 best-effort 清理夸克中转文件（释放中转空间）；删除失败
    仅告警不阻塞（残留由人工核实/清理兜底）。清理放事务提交后（网络 IO 不进事务）。
    """
    if started_at is None:
        return
    if started_at + timedelta(seconds=timeout_seconds) >= now:
        return  # 未超时，下一轮再查
    err = f"入库超时：{cause}，请人工核实刮削/收录配置"
    from app.tasks import transfer as transfer_mod  # 函数内延迟：防循环导入

    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id, DownloadQueue.status == "library")
                .values(
                    status="failed",
                    node_finished_at=now,
                    node_error=err,
                    error=err,
                    updated_at=now,
                )
            )
            if r.rowcount == 0:
                return  # 已被并发方推进 → 不触发 media 回退
            # P3-6：转 failed 终态后，若 media 无其他进行中集 → 回退 tracking
            await transfer_mod._sync_media_status(media_id, s)
    # P1-6：failed 已落库（事务提交）→ best-effort 清理夸克中转文件
    await _remove_quark_files(transfer_mod, quark_path)
    logger.warning("[library_check] %s %s", episode, err)


async def library_check_job() -> None:
    """APScheduler job 包装（IntervalTrigger(seconds=30)）。

    兜底驱动刮削执行器（覆盖下载完成事件之外的场景：重启恢复 / 上次同步失败重试；
    _run_scrape_once 内部空跑检测，无 status='scrape' 的任务时零开销）+ 入库轮询。
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
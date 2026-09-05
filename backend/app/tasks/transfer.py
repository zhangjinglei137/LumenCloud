"""
转存队列消费任务（设计文档 §4.4 容量感知转存 / §4.5 状态机防重）。

阶段 3：完整转存链（交付 B「容量感知转存」+ 交付 D「下载完成即释放」）。

两阶段流程（由 process_transfer_queue 统一驱动，供 APScheduler job 与 scan 事件触发）：

- 阶段 A：downloading 完成轮询（交付 D）
    - complete      → 释放夸克残留 + 双表 done + download_complete 通知 + 触发 nastools_sync（带冷却，不阻塞）
    - error/removed → 确定性失败路径：retry_count++ → ≥3 双表 failed + flow_error 告警；
                        <3 双表回退（es queued / tq pending）+ 清理夸克残留
    - active/waiting/paused → 仍在下载：显式刷新 updated_at（防 recover 2h 误回退）
    - aria2 故障（Aria2Unavailable）→ 该任务本轮跳过（不误判失败），记 task_run(error)
- 阶段 B：取最早一个 pending 任务串行转存（交付 B）
    GID 来源校验（§12.2 简化版）→ 容量门槛 fail-closed（§6.2/§6.3 模型 B）→ 条件更新抢占 →
    cloudSaver 转存 → alist 直链 → aria2 提交流程 → 双表 downloading

核心约定（全系统正确性相关，勿破坏）：
- 防重权威源 = episode_state；transfer_queue 为执行流视图（§3.1 双表联动映射）
- 所有状态转移用「条件更新」（WHERE 当前状态）捕获行级冲突，防 recover_on_boot 并发回退
- 每次转移显式写 updated_at=now（SQLAlchemy `onupdate` 只在 ORM 赋值时生效，
  execute(update) 必须显式传值）；recover_on_boot 依赖 updated_at 判定 2h 超时
- retry_count 仅「确定性失败 / 超时回退」消耗；quota 拒绝只走 quota_reject_count（§4.5）
- 转存链路（save → get_link → add_uri）的每步失败都走「重试路径」，
  只在 retry≥3 时转入 failed（需人工 retry）
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.config import settings
from app.database import async_session
from app.models import DownloadTask, EpisodeState, TransferQueue
from app.services import alist, aria2, capacity, cloudsaver
from app.services.notifier import (
    EVENT_DOWNLOAD_COMPLETE,
    EVENT_FLOW_ERROR,
    NotifyEvent,
    notifier,
)
from app.tasks import nastools_sync, record_task_run

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

# aria2 任务 comment 来源标记前缀（§12.2 GID 来源校验：陌生任务即本轮跳过并告警）
_COMMENT_PREFIX = "lumencloud:"
# 确定性失败 / 超时回退消耗 retry_count 的上限：≥3 转 failed，需人工 retry（§4.5）
_RETRY_LIMIT = 3
# 转存/下载进行中态（recovery 超时回退候选，语义同 recovery.py 的 _PROGRESS_STATES）
_PROGRESS_STATES = ("transferring", "downloading")
# P3-3（Oracle 审查）：后台任务强引用集合——防 asyncio.create_task 的任务被 GC 回收未执行
_background_tasks: set[asyncio.Task] = set()
# P0-2（council）：全局串行锁——阶段 A 轮询 + 阶段 B 转存整条链路互斥。
# 防 scan 事件触发 / 手动 retry / 定时 job（阶段 4）三路并发各自消费不同 pending、
# 容量模型 B 双过检双双转存 → /quark 突破硬上限；违反「串行单任务」约定。
_process_lock = asyncio.Lock()


def _spawn(coro_factory) -> None:
    """创建后台任务并持引用（事件触发续跑 / nastools_sync 触发共用）。

    coro_factory：返回 coroutine 的可调用对象（如 process_transfer_queue）。
    任务完成/取消后从 _background_tasks 移除（回调 discard）。
    """
    task = asyncio.create_task(coro_factory())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _now() -> datetime:
    """统一时间源（naive UTC，与 tasks/__init__._now 一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_link_wait_visible(file_name: str, timeout: float = 180.0) -> str:
    """save 受理后轮询等待转存文件在 alist /quark 可见，返回直链。

    阶段 3 实证：cloudSaver save 返回 task_id 受理后转存**异步落盘**，alist 同步存在
    延迟（阶段 1 实测 164KB srt 约 15s 落盘；本次实证 1.5-2.6G 文件落盘耗时
    60-180s，n8n 用「Wait(10s)」节点兜底但大文件不够）。立即 get_link 会 object
    not found。此处每 5s 轮询直至可见或超时（默认 180s）；超时抛 AlistUnavailable
    走外层重试路径（retry_count++，≥3 → failed）。
    """
    import time as _time

    path = f"/quark/{file_name}"
    deadline = _time.monotonic() + timeout
    last_exc: Exception | None = None
    while _time.monotonic() < deadline:
        try:
            return await alist.get_link(path)
        except Exception as exc:  # noqa: BLE001  直链暂不可用（未同步/瞬时失败）→ 继续等待
            last_exc = exc
            await asyncio.sleep(5)
    raise alist.AlistUnavailable(f"转存后等待落盘超时（{timeout:.0f}s）: {path}（{last_exc}）")


def _split_quark_path(path: str) -> tuple[str, list[str]]:
    """把夸克完整路径拆为 (dir, [name])，适配 alist.remove(names, dir) 契约（同 recovery.py 拆法）。

    例如 /quark/movie.mkv → ("/quark/", ["movie.mkv"])；dir 以 / 结尾。
    """
    path = (path or "").strip()
    if not path:
        return "/", []
    path = path.rstrip("/")
    if "/" in path:
        dir_part, name = path.rsplit("/", 1)
        return (dir_part or "/") + "/", [name]
    return "/", [path]


# ---------------------------------------------------------------------------
# 阶段 A：downloading 完成轮询（交付 D）
# ---------------------------------------------------------------------------

async def _poll_downloading_tasks() -> None:
    """阶段 A：轮询 downloading 任务 → complete / 确定性失败 / 刷新进度。

    网络 IO（aria2 tell_status、alist remove）放在数据库事务外；
    状态转移全部走条件更新，保证可重复执行幂等（不重复计数 / 通知 / 触发同步）。
    """
    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(DownloadTask).where(DownloadTask.status == "downloading")
                )
            )
            .scalars()
            .all()
        )
        snap = [
            (dt.id, dt.media_id, dt.transfer_id, dt.episode,
             dt.file_name, dt.quark_path, dt.aria2_gid)
            for dt in rows
        ]
    if not snap:
        return

    aria2_errors: list[str] = []
    for dt_id, media_id, tq_id, episode, file_name, quark_path, gid in snap:
        try:
            st = await aria2.client.tell_status(gid)
        except Exception as exc:  # aria2 故障 → 本轮跳过（不误判失败，交给 recover 2h 超时兜底）
            aria2_errors.append(f"media={media_id} ep={episode}: {exc}")
            logger.warning("[transfer] aria2 轮询失败（本轮跳过，不误判失败）: %s", exc)
            continue

        status = (st or {}).get("status")
        if status == "complete":
            await _complete_download(dt_id, media_id, tq_id, episode, file_name, quark_path)
        elif status in ("error", "removed"):
            await _fail_download(dt_id, media_id, tq_id, episode, f"aria2 任务状态 {status}")
        else:
            # active / waiting / paused（及未知状态按进行中处理）：刷新 updated_at
            await _refresh_progress(tq_id, media_id, episode)

    if aria2_errors:
        async with async_session() as s:
            await record_task_run(
                s, "transfer", "error",
                "aria2 状态轮询失败（本轮跳过，不误判失败; recover 超时兜底）: "
                + "; ".join(aria2_errors),
            )
            await s.commit()


async def _complete_download(dt_id, media_id, tq_id, episode, file_name, quark_path) -> None:
    """下载完成释放链：删夸克 → 双表 done → 通知 → 触发 nasTools 同步（不阻塞）。"""
    # a) 删除夸克文件（失败仅告警，不阻断 done）
    try:
        if quark_path:
            dir_part, names = _split_quark_path(quark_path)
            if names:
                await alist.remove(names, dir_part)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 下载完成清理夸克失败（不阻断）%s: %s", quark_path, exc)

    # b) 双表联动 done + 中介 download_task complete（条件更新，幂等防重复处理）
    #    P0-3b（council）：校验 rowcount——若 tq/es 已非 downloading（被 recovery 超时
    #    回退 / 人工 retry，双表失联），则置 download_task complete 但**不发完成通知、
    #    不触发 nastools_sync**，防「重复下载链的虚假完成通知」。
    now = _now()
    async with async_session() as s:
        async with s.begin():
            r_dl = await s.execute(
                update(DownloadTask)
                .where(DownloadTask.id == dt_id, DownloadTask.status == "downloading")
                .values(status="complete", downloaded_at=now)
            )
            r_tq = await s.execute(
                update(TransferQueue)
                .where(TransferQueue.id == tq_id, TransferQueue.status == "downloading")
                .values(status="done", updated_at=now)
            )
            r_es = await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                    EpisodeState.state == "downloading",
                )
                .values(state="done", updated_at=now)
            )
            if r_tq.rowcount == 0 or r_es.rowcount == 0 or r_dl.rowcount == 0:
                # 双表失联 / 重复处理：仅落中介终态，不广播完成事件
                await record_task_run(
                    s, "transfer", "error",
                    f"下载完成但双表失联（tq={r_tq.rowcount}/es={r_es.rowcount}/"
                    f"dl={r_dl.rowcount}），已置 download_task complete，不发送完成通知",
                    media_id,
                )
                logger.warning(
                    "[transfer] 下载完成但 tq/es 已非 downloading（可能被 recovery 回退/人工 retry），"
                    "跳过完成通知与 nasTools 同步（media=%s %s）", media_id, episode,
                )
                return
            await record_task_run(
                s, "transfer", "success", f"下载完成: {episode} ({file_name})", media_id,
            )

    # c) 通知（§7 download_complete，全体）
    await notifier.notify(NotifyEvent(
        event_type=EVENT_DOWNLOAD_COMPLETE,
        title=f"下载完成: {file_name}",
        body=f"媒体 {media_id} · 集 {episode} · {file_name} 下载完成，夸克中转空间已释放。",
        recipient=None,
        extra={"media_id": media_id, "episode": episode},
    ))

    # d) 触发 nastools_sync（任务 2 自带冷却；事件触发不阻塞转存链；P3-3 持引用防 GC）
    try:
        _spawn(nastools_sync.nastools_sync)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] nastools_sync 事件触发失败: %s", exc)


async def _fail_download(dt_id, media_id, tq_id, episode, reason) -> None:
    """确定性失败（aria2 error/removed）：retry_count++ → ≥3 双表 failed / <3 双表回退。

    回退前清理夸克残留（alist.remove，失败仅告警不阻断）。
    """
    # 读当前 retry_count（防重权威源 = episode_state）
    async with async_session() as s:
        es = (
            await s.execute(
                select(EpisodeState).where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                )
            )
        ).scalars().first()
        retry = es.retry_count if es is not None else 0
        es_quark_path = es.quark_path if es is not None else None
    new_retry = retry + 1
    terminal = new_retry >= _RETRY_LIMIT

    # 清理夸克残留（网络 IO，事务外；失败仅告警）
    try:
        if es_quark_path:
            dir_part, names = _split_quark_path(es_quark_path)
            if names:
                await alist.remove(names, dir_part)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 失败回退前清理夸克残留失败: %s", exc)

    now = _now()
    err = f"{reason}（retry={new_retry}/{_RETRY_LIMIT}）" if terminal else reason
    async with async_session() as s:
        async with s.begin():
            await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                    EpisodeState.state == "downloading",
                )
                .values(
                    state="failed" if terminal else "queued",
                    retry_count=new_retry,
                    error=err,
                    updated_at=now,
                )
            )
            await s.execute(
                update(TransferQueue)
                .where(TransferQueue.id == tq_id, TransferQueue.status == "downloading")
                .values(
                    status="failed" if terminal else "pending",
                    error=err,
                    updated_at=now,
                )
            )
            await s.execute(
                update(DownloadTask)
                .where(DownloadTask.id == dt_id, DownloadTask.status == "downloading")
                .values(status="failed")
            )
            await record_task_run(
                s, "transfer", "error",
                f"{episode} 下载失败: {err}", media_id,
            )

    if terminal:
        await notifier.notify(NotifyEvent(
            event_type=EVENT_FLOW_ERROR,
            title=f"任务失败: {episode}",
            body=f"{reason}；已重试 {new_retry} 次达上限，任务标记 failed，请人工 retry。",
            recipient=None,
            extra={"media_id": media_id, "episode": episode},
        ))
    logger.warning(
        "[transfer] %s 下载失败 %s（retry=%d/%d）%s",
        media_id, episode, new_retry, _RETRY_LIMIT, "转 failed" if terminal else "回退 queued",
    )


async def _refresh_progress(tq_id, media_id, episode) -> None:
    """仍在下载（active/waiting/paused）→ 显式刷新 updated_at（防 recover 2h 误回退）。"""
    now = _now()
    async with async_session() as s:
        async with s.begin():
            await s.execute(
                update(TransferQueue)
                .where(TransferQueue.id == tq_id, TransferQueue.status == "downloading")
                .values(updated_at=now)
            )
            await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                    EpisodeState.state == "downloading",
                )
                .values(updated_at=now)
            )


# ---------------------------------------------------------------------------
# 阶段 B：取一个 pending 任务串行转存（交付 B）
# ---------------------------------------------------------------------------

async def _record_alert(media_id, message) -> None:
    """record task_run(error) + flow_error 通知（GID 校验 / 容量数据不可用等 fail-closed 分支）。"""
    logger.warning("[transfer] %s", message)
    async with async_session() as s:
        await record_task_run(s, "transfer", "error", message, media_id)
        await s.commit()
    await notifier.notify(NotifyEvent(
        event_type=EVENT_FLOW_ERROR,
        title="转存流程告警",
        body=message,
        recipient=None,
        extra={"media_id": media_id} if media_id is not None else {},
    ))


async def _process_one_pending() -> None:
    """阶段 B：取最早 pending 任务串行转存（交付 B，一次只处理一个）。"""
    # 1) 取最早 pending（enqueued_at, id 排序，保证 FIFO）
    async with async_session() as s:
        tq = (
            (
                await s.execute(
                    select(TransferQueue)
                    .where(TransferQueue.status == "pending")
                    .order_by(TransferQueue.enqueued_at, TransferQueue.id)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if tq is None:
            await record_task_run(s, "transfer", "skipped", "无 pending 任务待转存")
            await s.commit()
            return
        tq_id, media_id, episode = tq.id, tq.media_id, tq.episode
        file_name, file_size = tq.file_name, tq.file_size
        share_code, stoken = tq.share_code, tq.stoken
        fids, fid_tokens, folder_id = tq.fids, tq.fid_tokens, tq.folder_id
        es = (
            await s.execute(
                select(EpisodeState).where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                )
            )
        ).scalars().first()
        es_retry = es.retry_count if es is not None else 0
        es_quark_path = es.quark_path if es is not None else None

    # 2) GID 来源校验兜底（§12.2 简化版）：存在陌生 aria2 活动任务 → 本轮跳过并告警
    #    （不处理、不 ++quota_reject_count；防 n8n 被误启动时的双转存）
    try:
        actives = await aria2.client.tell_active() or []
    except Exception as exc:  # noqa: BLE001  Aria2Unavailable → 无法确认来源，fail-closed
        await _record_alert(media_id, f"aria2 状态不可用，暂停转存（GID 校验失败）: {exc}")
        return
    for t in actives:
        if not str(t.get("comment") or "").startswith(_COMMENT_PREFIX):
            await _record_alert(
                media_id,
                "检测到陌生 aria2 任务（无本系统 GID 来源标记），暂停转存（§12.2 冷切换兜底），"
                "请人工确认 n8n 未误启动",
            )
            return

    # 3) 容量门槛（fail-closed，§6.2/§6.3 模型 B：capacity.provider.check）
    try:
        capacity_ok = await capacity.provider.check(file_size)
    except Exception as exc:  # noqa: BLE001  CapacityUnavailable → 保持 pending，不耗 retry / quota
        await _record_alert(media_id, f"容量数据不可用，保持 pending（fail-closed）: {exc}")
        return
    if not capacity_ok:
        # 容量不足：保持 pending + quota_reject_count++（绝不消耗 retry_count，§4.5）
        now = _now()
        async with async_session() as s:
            async with s.begin():
                await s.execute(
                    update(TransferQueue)
                    .where(TransferQueue.id == tq_id, TransferQueue.status == "pending")
                    .values(
                        quota_reject_count=TransferQueue.quota_reject_count + 1,
                        updated_at=now,
                    )
                )
                await record_task_run(
                    s, "transfer", "skipped", f"容量不足等待释放: {file_name}", media_id,
                )
        return

    # 4) 条件更新抢占（单 worker 也做，防 recover 并发；任一行数=0 → 冲突跳过）
    now = _now()
    async with async_session() as s:
        async with s.begin():
            r1 = await s.execute(
                update(TransferQueue)
                .where(TransferQueue.id == tq_id, TransferQueue.status == "pending")
                .values(status="transferring", updated_at=now)
            )
            r2 = await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                    EpisodeState.state == "queued",
                )
                .values(state="transferring", updated_at=now)
            )
            if r1.rowcount != 1 or r2.rowcount != 1:
                await record_task_run(
                    s, "transfer", "error",
                    f"状态抢占冲突（tq 命中 {r1.rowcount} / es 命中 {r2.rowcount}），本轮跳过",
                    media_id,
                )
                return

    # 5) 转存链路：cloudSaver save → 等落盘可见 → alist 直链 → aria2 addUri（receiveCode 用 stoken，双语义 G4）
    try:
        await cloudsaver.save({
            "fids": json.loads(fids or "[]"),
            "fidTokens": json.loads(fid_tokens or "[]"),
            # folderId 缺失时回退 QUARK_DEFAULT_FOLDER（阶段 3 实证：folderId 为空 → 转存不落盘 /quark）
            "folderId": folder_id or settings.QUARK_DEFAULT_FOLDER or None,
            "shareCode": share_code,
            "receiveCode": stoken,  # G4：receiveCode 语义 = stoken（非提取码）
        })
        link = await _get_link_wait_visible(file_name)
        gid = await aria2.client.add_uri(
            link,
            out=file_name,
            comment=f"{_COMMENT_PREFIX}{media_id}:{episode}",
        )
    except Exception as exc:  # noqa: BLE001
        # 任一步失败（含转存成功但直链/aria2 提交失败）→ 重试路径；
        # 清理可能已转存的夸克残留（避免残留占用中转空间）
        try:
            dir_part, names = _split_quark_path(es_quark_path or f"/quark/{file_name}")
            if names:
                await alist.remove(names, dir_part)
        except Exception as e:  # noqa: BLE001  清理失败仅告警（P3-1），不阻断重试
            logger.warning("[transfer] 转存失败后清理夸克残留失败: %s", e)
        new_retry = es_retry + 1
        terminal = new_retry >= _RETRY_LIMIT
        logger.warning(
            "[transfer] 转存失败 media=%s %s（retry=%d/%d）: %s",
            media_id, episode, new_retry, _RETRY_LIMIT, exc,
        )
        now = _now()
        reason = f"转存失败: {exc}"
        async with async_session() as s:
            async with s.begin():
                await s.execute(
                    update(EpisodeState)
                    .where(
                        EpisodeState.media_id == media_id,
                        EpisodeState.episode == episode,
                        EpisodeState.state == "transferring",
                    )
                    .values(
                        state="failed" if terminal else "queued",
                        retry_count=new_retry,
                        error=reason,
                        updated_at=now,
                    )
                )
                await s.execute(
                    update(TransferQueue)
                    .where(TransferQueue.id == tq_id, TransferQueue.status == "transferring")
                    .values(
                        status="failed" if terminal else "pending",
                        error=reason,
                        updated_at=now,
                    )
                )
                await record_task_run(
                    s, "transfer", "error",
                    f"{episode} 转存失败（retry={new_retry}/{_RETRY_LIMIT}）: {exc}", media_id,
                )
        if terminal:
            await notifier.notify(NotifyEvent(
                event_type=EVENT_FLOW_ERROR,
                title=f"转存失败: {file_name}",
                body=f"{reason}；已重试 {new_retry} 次达上限，任务标记 failed，请人工 retry。",
                recipient=None,
                extra={"media_id": media_id, "episode": episode},
            ))
        else:
            # P2-6（Oracle 审查）：非终态回退（pending/queued）后触发下一轮消费续跑，
            # 防队列滞留（阶段 3 定时关闭，靠事件/手动触发，回退后需主动续跑）
            _spawn(process_transfer_queue)
        return

    # 6) 成功：建 download_task + 双表 downloading（aria2_gid / quark_path 一并落 es）
    quark_path = f"/quark/{file_name}"
    now = _now()
    async with async_session() as s:
        async with s.begin():
            s.add(DownloadTask(
                media_id=media_id,
                transfer_id=tq_id,
                episode=episode,
                file_name=file_name,
                aria2_gid=gid,
                status="downloading",
                quark_path=quark_path,
            ))
            await s.execute(
                update(TransferQueue)
                .where(TransferQueue.id == tq_id, TransferQueue.status == "transferring")
                .values(status="downloading", updated_at=now)
            )
            await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                    EpisodeState.state == "transferring",
                )
                .values(
                    state="downloading",
                    aria2_gid=gid,
                    quark_path=quark_path,
                    updated_at=now,
                )
            )
            await record_task_run(
                s, "transfer", "success",
                f"转存并提交 aria2 下载: {episode}（gid={gid}）", media_id,
            )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def process_transfer_queue() -> None:
    """转存队列消费主流程（APScheduler job 与 scan 事件触发共用）。

    阶段 A + 阶段 B 依序执行（阶段 A 释放容量后阶段 B 的容量检查更准）；
    阶段 B 一次只处理一个 pending，完成后由下一轮调度续跑（串行单任务）。

    P0-2（council）：全流程持 _process_lock——任意时刻只有一个 worker 执行
    （scan 事件 / 手动 retry / 定时 job / _spawn 续跑 均经此入口）；拿不到锁的
    调用方按 asyncio.Lock 语义等待而非跳过，保证容量检查-转存两步不并发。
    """
    async with _process_lock:
        try:
            await _poll_downloading_tasks()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[transfer] 阶段A（完成轮询）异常")
            async with async_session() as s:
                await record_task_run(s, "transfer", "error", f"阶段A轮询异常: {exc}")
                await s.commit()
        try:
            await _process_one_pending()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[transfer] 阶段B（串行转存）异常")
            async with async_session() as s:
                await record_task_run(s, "transfer", "error", f"阶段B转存异常: {exc}")
                await s.commit()


async def process_transfer_queue_job() -> None:
    """APScheduler job 包装（IntervalTrigger(minutes=1) 兜底）：异常不外泄。"""
    try:
        await process_transfer_queue()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[transfer] process_transfer_queue_job 异常")
        async with async_session() as s:
            await record_task_run(s, "transfer", "error", f"transfer job 异常: {exc}")
            await s.commit()


async def trigger_transfer() -> None:
    """scan 入队后的事件触发（调用 process_transfer_queue，异常捕获记日志，不阻塞调用方）。"""
    try:
        await process_transfer_queue()
    except Exception:  # noqa: BLE001
        logger.exception("[transfer] trigger_transfer 事件触发异常")
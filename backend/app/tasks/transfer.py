"""
转存队列消费任务（设计文档 §4.4 容量感知转存 / §4.5 状态机防重）。

阶段 3：完整转存链（交付 B「容量感知转存」+ 交付 D「下载完成即释放」）。

两阶段流程（由 process_transfer_queue 统一驱动，供 APScheduler job 与 scan 事件触发）：

- 阶段 A：downloading 完成轮询（交付 D）
    - complete      → 释放夸克残留 + 双表 done + download_complete 通知 + 触发 nastools_sync（带冷却，不阻塞）
    - error/removed → 确定性失败路径：retry_count++ → ≥3 双表 failed + flow_error 告警；
                        <3 双表回退（es queued / tq pending）+ 清理夸克残留
    - active/waiting（及未知状态）→ 仍在下载：显式刷新 updated_at（防 recover 2h 误回退）
    - paused（外部暂停）→ 不刷新进度，等待 recover 超时回退（P2-9）
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
import time as _time
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.config import settings
from app.database import async_session
from app.models import DownloadTask, EpisodeState, Media, TransferQueue
from app.services import alist, aria2, capacity, cloudsaver, config_store
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
# save 受理后等待转存文件在 alist 可见的超时上限（秒）。
# 阶段 3 实证 + 线上反馈：1.5-2.6G 大文件落盘耗时 60-180s，叠加 alist 同步延迟，
# 180s 上限偏紧（个别超时）；放宽至 300s 作兜底。超时抛 AlistUnavailable 走外层
# 重试路径（retry_count++，≥3 → failed），故上限放宽不造成「无限等」，只是多给一轮。
_LINK_WAIT_TIMEOUT = 300.0
# P0-1（council 兜底）：save 受理时间超时上限（秒）。save_task_id 存在但
# save_attempt_at 距今超过该值（或该列为空——旧数据/某清空路径漏写）→ 视为 stale，
# 强制重新 save。即使任何清空路径漏了，超 10 分钟也会强制重 save，杜绝盲等死循环。
_SAVE_ATTEMPT_MAX_SECONDS = 600
# P3-2（council）：quota 拒绝累计告警阈值——容量不足连续累计 ≥5 次触发
# flow_error 告警（复用 P2-2 的 capacity 类别节流，防每分钟 job 刷屏）。
# quota 拒绝只走 quota_reject_count，绝不消耗 retry_count（§4.5）。
_QUOTA_REJECT_ALERT_THRESHOLD = 5
# 转存/下载进行中态（recovery 超时回退候选，语义同 recovery.py 的 _PROGRESS_STATES）
_PROGRESS_STATES = ("transferring", "downloading")
# P3-3（Oracle 审查）：后台任务强引用集合——防 asyncio.create_task 的任务被 GC 回收未执行
_background_tasks: set[asyncio.Task] = set()
# P0-2（council）：全局串行锁——阶段 A 轮询 + 阶段 B 转存整条链路互斥。
# 防 scan 事件触发 / 手动 retry / 定时 job（阶段 4）三路并发各自消费不同 pending、
# 容量模型 B 双过检双双转存 → /quark 突破硬上限；违反「串行单任务」约定。
_process_lock = asyncio.Lock()

# P2-2（council）：flow_error 通知节流窗（秒）。GID 校验失败/容量不可用等
# fail-closed 场景由每分钟兜底 job 重复触发，同一告警 10 分钟内只 notify 一次，
# 防通知刷屏（task_run(error) 仍每次记录，仅通知节流）。
_ALERT_COOLDOWN_SECONDS = 600.0
# P2-2：告警节流表。key = f"{media_id}:{category}"；值 = (最近 notify 的
# monotonic 时间戳, 上次消息)。同一 key 在窗口内重复触发时，仅当消息与上次
# **完全相同**才跳过 notify（消息变化视为根因变化的新告警，必须通知）。
_alert_cooldown: dict[str, tuple[float, str]] = {}


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


async def _get_link_wait_visible(file_name: str, timeout: float = _LINK_WAIT_TIMEOUT) -> str:
    """save 受理后轮询等待转存文件在 alist /quark 可见，返回直链。

    阶段 3 实证：cloudSaver save 返回 task_id 受理后转存**异步落盘**，alist 同步存在
    延迟（阶段 1 实测 164KB srt 约 15s 落盘；实证 1.5-2.6G 文件落盘耗时 60-180s，
    n8n 用「Wait(10s)」节点兜底但大文件不够）。立即 get_link 会 object not found。
    此处每 5s 轮询直至可见或超时（默认 _LINK_WAIT_TIMEOUT=300s——线上反馈 2.6G
    个别落盘超 180s，放宽兜底）；超时抛 AlistUnavailable 走外层重试路径
    （retry_count++，≥3 → failed）。
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
    # P0-1（线上反馈「转存多次失败」）：save 返回 200 仅代表受理，文件异步落盘；
    # 超时说明文件始终未在 alist /quark 可见。抛错前先列 /quark 目录记录实际内容，
    # 便于区分「落盘到了别处（folderId 配置错）」与「文件名被云盘改名/仍在传输」。
    folder_id = config_store.get("quark_default_folder", settings.QUARK_DEFAULT_FOLDER)
    try:
        entries = await alist.list_dir("/quark")
        logger.warning(
            "[transfer] 转存落盘超时前 /quark 目录实际内容（%d 项）: %s",
            len(entries), [e.get("name") for e in entries],
        )
    except Exception as exc2:  # noqa: BLE001  列目录失败仅记录诊断，不阻断原异常抛出
        logger.warning("[transfer] 落盘超时后列 /quark 目录失败: %s", exc2)
    raise alist.AlistUnavailable(
        f"转存后等待落盘超时（{timeout:.0f}s）: {path}（folderId={folder_id}，{last_exc}）；"
        f"请用 alist 管理 API /api/admin/storage/list 核对 quark_default_folder 是否为 root_folder_id"
    )


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


def _extract_save_task_id(data) -> str | None:
    """从 cloudsaver.save 返回的 data 字典中健壮提取 task_id（P2-10）。

    save 返回结构多样（各版本 cloudSaver 字段不一）：优先取常见键
    （task_id / taskId / taskID / saveTaskId，兼容大小写变体）；取不到返回 None，
    调用方忽略（不落 save_task_id，退化回原重试行为）。

    注意（Oracle M1）：不做单键兜底——`{"error": ...}`/`{"msg": ...}` 等错误响应
    若被当 task_id 落库，会让下一轮重试跳过 save 并永远等不到文件（死循环到
    retry 上限标 failed）。宁可不落（重复 save 是安全行为），不可落假 id。
    """
    if not isinstance(data, dict):
        return None
    for key in ("task_id", "taskId", "taskID", "saveTaskId", "save_task_id"):
        val = data.get(key)
        if val:
            return str(val)
    return None


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
            # active / waiting（及未知状态按进行中处理）：刷新 updated_at
            if status == "paused":
                # P2-9（council）：aria2 任务被外部暂停 → 不再刷新 updated_at，
                # 让其超过 episode_state_timeout_hours 老化后由 recover_stale_tasks
                # 超时回退 queued（+ 清理残留）；此前 paused 也刷新进度导致
                # recover 2h 超时永不触发，任务永久卡在 downloading。
                logger.debug(
                    "[transfer] aria2 任务被外部暂停（gid=%s），不刷新进度，等待 recover %sh 超时回退",
                    gid, settings.EPISODE_STATE_TIMEOUT_HOURS,
                )
            else:
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
    """下载完成释放链：双表 done → 删夸克 → 通知 → 触发 nasTools 同步（不阻塞）。

    P0-2（council）顺序修复：**先双表 done、后删夸克文件**——若先删后双表
    done 校验失败（双表失联/重复处理），文件已删但状态未推进，下一轮重试时
    落盘文件已不存在（无法 get_link），等于彻底毁掉重试机会；先 done 则删夸克
    失败仅告警不阻断，已落 done 的任务无需再依赖夸克中转文件。
    """
    now = _now()
    async with async_session() as s:
        async with s.begin():
            # a) 双表联动 done + 中介 download_task complete（条件更新，幂等防重复处理）
            #    P0-3b（council）：校验 rowcount——若 tq/es 已非 downloading（被 recovery
            #    超时回退 / 人工 retry，双表失联），走下方回退分支。
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
                # P0-2（council）双表失联 / 重复处理：不再「仅置 dl complete 就返回」——
                # 显式回退 tq→pending、es→queued 并清空 save_task_id，让下一轮重新
                # 走完整转存链（重新 save → 转存 → 下载）。download_task 由上方条件
                # 更新尽力置 complete（未命中说明已被 _fail_download 置 failed / 已
                # complete，尊重并发方现状，不再覆盖）；**不广播完成事件**。
                # 回退按 rowcount 精确区分（避免覆盖并发方/历史状态）：
                #   rowcount==1 → 本事务刚把它置 done（半边失联竞态：对方已回退而
                #                  本方还是 downloading）→ 从 done 回退 pending/queued，
                #                  保持双表一致；
                #   rowcount==0 → 未被本事务 done（已被并发方回退为 pending/queued，
                #                  或本就 done 的重复轮询）→ 保持现状不覆盖。
                await s.execute(
                    update(TransferQueue)
                    .where(TransferQueue.id == tq_id)
                    .values(
                        save_task_id=None,  # 无条件清空，防幂等标记残留致盲等
                        save_attempt_at=None,
                    )
                )
                if r_tq.rowcount == 1:
                    await s.execute(
                        update(TransferQueue)
                        .where(TransferQueue.id == tq_id, TransferQueue.status == "done")
                        .values(status="pending", error="双表失联已回退待重试", updated_at=now)
                    )
                if r_es.rowcount == 1:
                    await s.execute(
                        update(EpisodeState)
                        .where(
                            EpisodeState.media_id == media_id,
                            EpisodeState.episode == episode,
                            EpisodeState.state == "done",
                        )
                        .values(state="queued", error="双表失联已回退待重试", updated_at=now)
                    )
                await record_task_run(
                    s, "transfer", "error",
                    f"下载完成但双表失联（tq={r_tq.rowcount}/es={r_es.rowcount}/"
                    f"dl={r_dl.rowcount}），已回退 tq→pending/es→queued 并清空 "
                    f"save_task_id，待下一轮重新转存",
                    media_id,
                )
                logger.warning(
                    "[transfer] 下载完成但 tq/es 已非 downloading（可能被 recovery 回退/"
                    "人工 retry），已回退待重试（media=%s %s）", media_id, episode,
                )
                return
            await record_task_run(
                s, "transfer", "success", f"下载完成: {episode} ({file_name})", media_id,
            )
            # P3-6（council）：es 转 done 后检查该 media 是否还有其他进行中 es，
            # 无则回 tracking（条件更新不覆盖 paused）。与 tq/es/dl 同一事务。
            await _sync_media_status(media_id, s)

    # b) 双表 done 已成功 → 删夸克文件（网络 IO，事务外；失败仅告警，不阻断 done）
    #    P0-2：置于双表 done 之后——删失败不影响已落 done 的状态推进。
    try:
        if quark_path:
            dir_part, names = _split_quark_path(quark_path)
            if names:
                await alist.remove(names, dir_part)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 下载完成清理夸克失败（不阻断）%s: %s", quark_path, exc)

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

    # A-1（P1）：下载完成即释放容量 → 触发转存续跑，容量恢复后保持 pending 的任务自动重试
    try:
        _spawn(process_transfer_queue)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 下载完成后转存续跑触发失败: %s", exc)


async def _fail_download(dt_id, media_id, tq_id, episode, reason) -> None:
    """确定性失败（aria2 error/removed）：retry_count++ → ≥3 双表 failed / <3 双表回退。

    回退前清理夸克残留（alist.remove，失败仅告警不阻断）。

    P2-5（council）：retry_count 增量改用 CAS 条件更新（WHERE 含
    retry_count=读到的旧值），替代「读-改-写」——recovery 并发回退同一任务时，
    写死 new_retry 会丢失对方增量；CAS 未命中（rowcount=0）视为并发冲突，
    本轮跳过状态转移、不重复计数（仅记 task_run(error)），交由并发方/下一轮推进。
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
            r_es = await s.execute(
                update(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.episode == episode,
                    EpisodeState.state == "downloading",
                    EpisodeState.retry_count == retry,  # P2-5 CAS：防读-改-写丢增量
                )
                .values(
                    state="failed" if terminal else "queued",
                    retry_count=new_retry,
                    error=err,
                    updated_at=now,
                )
            )
            if r_es.rowcount == 0:
                # P2-5：CAS 冲突（recovery 并发已回退/已计数）→ 不重复计数、不转移状态
                await record_task_run(
                    s, "transfer", "error",
                    f"下载失败但 retry_count CAS 冲突（并发回退?），本轮跳过不计数: {reason}",
                    media_id,
                )
                return
            await s.execute(
                update(TransferQueue)
                .where(TransferQueue.id == tq_id, TransferQueue.status == "downloading")
                .values(
                    status="failed" if terminal else "pending",
                    error=err,
                    # P0-1（council）：下载确定性失败同样清空 save_task_id——非终态
                    # 回退 pending 后下一轮会立即重新走转存链，残留的幂等标记会让
                    # 下一轮跳过 save 盲等（与转存失败路径同一清理语义）；
                    # save_attempt_at 与其同生同灭一并清空。
                    save_task_id=None,
                    save_attempt_at=None,
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
            # P3-6（council）：仅终态（es 转 failed）才回 tracking——非终态回退
            # queued 仍属进行中（排队中），media 保持 downloading。与双表同一事务。
            if terminal:
                await _sync_media_status(media_id, s)

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
    """仍在下载（active/waiting/未知状态）→ 显式刷新 updated_at（防 recover 2h 误回退）。

    P2-9：paused 不再刷新（由 recover 超时回退），故调用方只在非 paused 时调用。
    """
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


# P3-6（council）：media 上"进行中"的 episode_state 状态集合——转存排队中也算
# 处理中（前端/系统以此区分正在处理的影视）；failed/done 不算。
_ACTIVE_ES_STATES = ("queued", "transferring", "downloading")


async def _sync_media_status(media_id: int, session=None) -> int:
    """P3-6（council）：media 不再有任何进行中 es → 条件回退 status='tracking'。

    进行中 = EpisodeState.state in _ACTIVE_ES_STATES（queued 排队中也算处理中；
    failed/done 不算）。条件更新 WHERE media.status='downloading'：不覆盖用户手动
    paused，也不干扰其余状态；无匹配行（用户已 paused / 已非 downloading）返回 0 忽略。

    参数 session：传入时复用外部事务（由调用方统一提交，减少额外 session）；
    不传则自开短事务。返回回退 update 的行数（0 或 1）。
    """
    async def _run(s):
        active = (
            await s.scalar(
                select(func.count())
                .select_from(EpisodeState)
                .where(
                    EpisodeState.media_id == media_id,
                    EpisodeState.state.in_(_ACTIVE_ES_STATES),
                )
            )
        ) or 0
        if active:
            return 0
        result = await s.execute(
            update(Media)
            .where(Media.id == media_id, Media.status == "downloading")
            .values(status="tracking", updated_at=_now())
        )
        return result.rowcount

    if session is not None:
        return await _run(session)
    async with async_session() as s:
        async with s.begin():
            return await _run(s)


# ---------------------------------------------------------------------------
# 阶段 B：取一个 pending 任务串行转存（交付 B）
# ---------------------------------------------------------------------------

def _alert_bucket(message: str) -> str:
    """告警节流指纹：消息固定前缀（去掉 ': <exc>' 变量尾巴）。

    Oracle M4：GID/容量告警消息尾部的 exc 会随网络抖动变化（超时/拒连/解析失败…），
    直接整条比较会让节流对变量尾巴失效（每分钟 job 刷屏）。取冒号前固定前缀作
    比较指纹；不同前缀 = 不同根因，照常放行通知。
    """
    return (message or "").split(": ", 1)[0]


async def _record_alert(media_id, message, category=None, bucket=None) -> None:
    """record task_run(error) + flow_error 通知（GID 校验 / 容量数据不可用等 fail-closed 分支）。

    P2-2（council）：flow_error 通知节流——GID 校验失败/容量不可用由每分钟兜底
    job 重复触发会通知刷屏；此处按 (media_id, 告警类别) 在 _ALERT_COOLDOWN_SECONDS
    内去重：首次必须通知，窗口内**同类别且节流指纹（bucket）相同**的重复触发跳过
    notify（task_run(error) 仍每次记录）。告警类别：GID 校验失败用 "gid"、容量失败
    用 "capacity"、其他用消息前缀前 40 字符。

    bucket：节流指纹，默认 _alert_bucket(message) 推断（M4：截掉变量尾巴）。
    可显式传入使**不同消息共享同一指纹**——如 P3-2 容量不足告警（消息含累计次数、
    随计数变化）与容量不可用告警（消息含 exc 文本）协议统一传 bucket="capacity"，
    使"容量不足/容量不可用"10 分钟内对同一 media 只 notify 一次（任务 P3-2 要求）。
    """
    logger.warning("[transfer] %s", message)
    async with async_session() as s:
        await record_task_run(s, "transfer", "error", message, media_id)
        await s.commit()
    bucket = bucket if bucket is not None else _alert_bucket(message)
    key = f"{media_id}:{category or bucket[:40]}"
    now = _time.monotonic()
    last_ts, last_bucket = _alert_cooldown.get(key, (0.0, None))
    if last_bucket == bucket and (now - last_ts) < _ALERT_COOLDOWN_SECONDS:
        logger.info(
            "[transfer] flow_error 通知节流（%ds 内同类重复告警 %s）", _ALERT_COOLDOWN_SECONDS, key,
        )
        return
    _alert_cooldown[key] = (now, bucket)
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
        # P2-10（council）：快照 cloudSaver save 幂等标记（步骤 5 据此跳过重复 save）
        tq_save_task_id = tq.save_task_id
        # P0-1（council）：快照 save 受理时间（步骤 5 超时兜底判断用——
        # save_task_id 存在但受理时间过久 → 强制重新 save）
        tq_save_attempt_at = tq.save_attempt_at
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

    # 2) GID 来源校验兜底（§12.2 简化版）：存在陌生 aria2 活动/等待任务 → 本轮跳过并告警
    #    （不处理、不 ++quota_reject_count；防 n8n 被误启动时的双转存）
    #    P2-6（council）：合并校验 active + waiting 队列——waiting 中的陌生任务同样
    #    代表排队中的双转存，仅校验 active 会漏检；任一调用异常仍走 fail-closed。
    try:
        actives = await aria2.client.tell_active() or []
        tell_waiting = getattr(aria2.client, "tell_waiting", None)
        if tell_waiting is not None:
            actives = actives + (await tell_waiting() or [])
    except Exception as exc:  # noqa: BLE001  Aria2Unavailable → 无法确认来源，fail-closed
        await _record_alert(
            media_id, f"aria2 状态不可用，暂停转存（GID 校验失败）: {exc}", category="gid",
        )
        return
    for t in actives:
        if not str(t.get("comment") or "").startswith(_COMMENT_PREFIX):
            await _record_alert(
                media_id,
                "检测到陌生 aria2 任务（无本系统 GID 来源标记），暂停转存（§12.2 冷切换兜底），"
                "请人工确认 n8n 未误启动",
                category="gid",
            )
            return

    # 3) 容量门槛（fail-closed，§6.2/§6.3 模型 B：capacity.provider.check）
    try:
        capacity_ok = await capacity.provider.check(file_size)
    except Exception as exc:  # noqa: BLE001  CapacityUnavailable → 保持 pending，不耗 retry / quota
        # P3-2：bucket 与容量不足告警统一为 "capacity"，共享 P2-2 节流——
        # "容量数据不可用"/"容量不足"10 分钟内对同一 media 只 notify 一次。
        await _record_alert(
            media_id, f"容量数据不可用，保持 pending（fail-closed）: {exc}",
            category="capacity", bucket="capacity",
        )
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
                # P3-2（council）: 读取更新后的累计次数；达到阈值（≥5）→ 容量类
                # flow_error 告警。task_run(skipped) 仍每次记录，告警复用 P2-2 的
                # "capacity" 类别 + 统一指纹 bucket（与"容量数据不可用"共享，
                # 10 分钟内同类只 notify 一次，防每分钟 job 刷屏；消息含累计次数/
                # 文件名，故显式固定 bucket 而非按消息前缀推断）。
                qc = (
                    await s.scalar(
                        select(TransferQueue.quota_reject_count).where(
                            TransferQueue.id == tq_id
                        )
                    )
                ) or 0
        if qc >= _QUOTA_REJECT_ALERT_THRESHOLD:
            await _record_alert(
                media_id,
                f"容量不足已累计 {qc} 次，请人工检查夸克空间或配置: {file_name}",
                category="capacity",
                bucket="capacity",
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
            # P3-6（council）：media.status → downloading（转存/下载进行中的影视
            # 标记）。条件更新 WHERE status='tracking'：防覆盖用户手动 paused
            # （paused 只允许从 tracking 设置）；rowcount=0 无妨——可能已被本链路
            # 置过（多集连续转存）或用户已 paused，均非错误。
            await s.execute(
                update(Media)
                .where(Media.id == media_id, Media.status == "tracking")
                .values(status="downloading", updated_at=now)
            )

    # 5) 转存链路：cloudSaver save → 等落盘可见 → alist 直链 → aria2 addUri（receiveCode 用 stoken，双语义 G4）
    #    P2-10（council）：save 幂等——tq.save_task_id 已存在（上一轮 save 已受理此文件）
    #    则跳过 cloudsaver.save，直接等落盘/取直链；save 成功即把 task_id 持久化，
    #    后续 get_link / add_uri 失败重试时不再重复 save（防重复转存占空间/cloudSaver
    #    端重复任务）。P0-1：**仅成功路径保持幂等**——失败回退路径在下方事务中清空
    #    save_task_id，下一轮强制重新 save（防「已受理未落盘」被当完成导致盲等死循环）。
    save_task_id = None
    try:
        # P0-1（council 兜底）：save_task_id 存在但 save_attempt_at 距今超过
        # _SAVE_ATTEMPT_MAX_SECONDS（或该列为空——旧数据/某清空路径漏写）→ 视为
        # stale，先清 save_task_id 再走 save 分支，强制重新 save。任何清空路径漏清
        # 时，超 10 分钟也会自动恢复，杜绝「已受理未落盘」的盲等死循环。
        if tq_save_task_id and (
            tq_save_attempt_at is None
            or (_now() - tq_save_attempt_at).total_seconds() > _SAVE_ATTEMPT_MAX_SECONDS
        ):
            now_stale = _now()
            async with async_session() as s:
                async with s.begin():
                    await s.execute(
                        update(TransferQueue)
                        .where(TransferQueue.id == tq_id, TransferQueue.status == "transferring")
                        .values(save_task_id=None, save_attempt_at=None, updated_at=now_stale)
                    )
            tq_save_task_id = None
            tq_save_attempt_at = None
            logger.warning(
                "[transfer] save_task_id 受理已超 %ds 或时间缺失，强制重新 save 防盲等",
                _SAVE_ATTEMPT_MAX_SECONDS,
            )
        if not tq_save_task_id:
            # P0-1（线上反馈「转存多次失败」）：save 诊断日志——记录 file_name /
            # folderId / shareCode 关键参数，便于核对 folderId 是否与 alist Quark
            # 驱动 root_folder_id 一致（配置错 → 文件落盘到别处 /quark 永不可见）。
            # 严禁记录 stoken（receiveCode）等敏感值。
            folder_id_effective = (
                folder_id
                or config_store.get("quark_default_folder", settings.QUARK_DEFAULT_FOLDER)
                or None
            )
            logger.info(
                "[transfer] 提交 cloudsaver.save file_name=%s folderId=%s shareCode=%s",
                file_name, folder_id_effective, share_code,
            )
            save_res = await cloudsaver.save({
                "fids": json.loads(fids or "[]"),
                "fidTokens": json.loads(fid_tokens or "[]"),
                # folderId 缺失时回退 QUARK_DEFAULT_FOLDER（阶段 3 实证：folderId 为空 → 转存不落盘 /quark）
                # Phase 8：改读 config_store（system_config 优先，env fallback，保存即生效）
                "folderId": folder_id_effective,
                "shareCode": share_code,
                "receiveCode": stoken,  # G4：receiveCode 语义 = stoken（非提取码）
            })
            save_task_id = _extract_save_task_id(save_res)
            if save_task_id:
                # save 一受理即落库（条件更新 WHERE status='transferring'，行数未中
                # 则忽略），保证在 get_link / add_uri 之前 task_id 已可被重试读取。
                # P0-1：save_attempt_at 同时落库（=受理时间），供步骤 5 超时兜底判断。
                now_save = _now()
                async with async_session() as s:
                    async with s.begin():
                        await s.execute(
                            update(TransferQueue)
                            .where(TransferQueue.id == tq_id, TransferQueue.status == "transferring")
                            .values(
                                save_task_id=save_task_id,
                                save_attempt_at=now_save,
                                updated_at=now_save,
                            )
                        )
        else:
            save_task_id = tq_save_task_id
        link = await _get_link_wait_visible(file_name, timeout=_LINK_WAIT_TIMEOUT)
        gid = await aria2.client.add_uri(
            link,
            out=file_name,
            comment=f"{_COMMENT_PREFIX}{media_id}:{episode}",
        )
    except Exception as exc:  # noqa: BLE001
        # 任一步失败（含转存成功但直链/aria2 提交失败）→ 重试路径；
        # 清理可能已转存的夸克残留（避免残留占用中转空间）。
        # P2-10 / P0-1（线上反馈「转存多次失败」）：save_task_id 已落库代表「已受理」
        # 而非「已完成」——若不清空，下一轮会跳过 save 并永远等不到文件（盲等死循环
        # 到 retry 上限标 failed）。下方失败回退事务同时清空 save_task_id，强制下一轮
        # 重新 save；仅成功路径保持幂等。
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
                r_es = await s.execute(
                    update(EpisodeState)
                    .where(
                        EpisodeState.media_id == media_id,
                        EpisodeState.episode == episode,
                        EpisodeState.state == "transferring",
                        EpisodeState.retry_count == es_retry,  # P2-5 CAS：防读-改-写丢增量
                    )
                    .values(
                        state="failed" if terminal else "queued",
                        retry_count=new_retry,
                        error=reason,
                        updated_at=now,
                    )
                )
                if r_es.rowcount == 0:
                    # P2-5：CAS 冲突（recovery 并发已回退/已计数）→ 本轮不计数、不
                    # 转移状态、不触发续跑（避免与并发方争抢），交由对方/下一轮 job 推进
                    await record_task_run(
                        s, "transfer", "error",
                        f"{episode} 转存失败但 retry_count CAS 冲突（并发回退?），本轮跳过不计数: {exc}",
                        media_id,
                    )
                    # P0-1（council）：CAS 冲突分支（此前直接 return）也无条件清空
                    # save_task_id / save_attempt_at（WHERE id=tq_id，不依赖 CAS 结果）
                    # ——不在此处清空，残留的幂等标记会让下一轮跳过 save 盲等死循环。
                    await s.execute(
                        update(TransferQueue)
                        .where(TransferQueue.id == tq_id)
                        .values(save_task_id=None, save_attempt_at=None)
                    )
                    return
                await s.execute(
                    update(TransferQueue)
                    .where(TransferQueue.id == tq_id, TransferQueue.status == "transferring")
                    .values(
                        status="failed" if terminal else "pending",
                        error=reason,
                        # P0-1：失败重试路径清空 save_task_id，下一轮强制重新 save
                        # （打破「已受理未落盘」的盲等死循环）；成功路径才保持幂等。
                        save_task_id=None,
                        updated_at=now,
                    )
                )
                await record_task_run(
                    s, "transfer", "error",
                    f"{episode} 转存失败（retry={new_retry}/{_RETRY_LIMIT}）: {exc}", media_id,
                )
                # P3-6（council）：转存失败转 failed 与 _fail_download 终态同语义——
                # es 不再是进行中态时，media 若无其他进行中 es 则回 tracking。
                if terminal:
                    await _sync_media_status(media_id, s)
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
                .values(
                    status="downloading",
                    save_task_id=save_task_id if save_task_id else None,
                    updated_at=now,
                )
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

    # A-1（P1）：成功提交后触发下一 pending 消费续跑——与失败回退（:896）对称；
    # _process_lock 保证串行（续跑仅排队等待下一轮），解决「一次 scan 入队 N 集只处理 1 集」的静默积压
    _spawn(process_transfer_queue)


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
    t0 = _time.monotonic()  # Q8①：真实耗时
    try:
        await process_transfer_queue()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[transfer] process_transfer_queue_job 异常")
        async with async_session() as s:
            await record_task_run(  # Q8①：真实耗时
                s, "transfer", "error", f"transfer job 异常: {exc}",
                duration_seconds=_time.monotonic() - t0,
            )
            await s.commit()


async def trigger_transfer() -> None:
    """scan 入队后的事件触发（调用 process_transfer_queue，异常捕获记日志，不阻塞调用方）。"""
    try:
        await process_transfer_queue()
    except Exception:  # noqa: BLE001
        logger.exception("[transfer] trigger_transfer 事件触发异常")


# ---------------------------------------------------------------------------
# council 审查修复记录（P2-2 / P2-4 / P2-5 / P2-6 / P2-9 / P2-10 / P3-2 / P3-6）
# ---------------------------------------------------------------------------
# P2-2  ：flow_error 通知节流。_record_alert 按 (media_id, 告警类别) 在 10 分钟
#         （_ALERT_COOLDOWN_SECONDS）内去重：task_run(error) 每次记录，notify 仅在
#         「首次」或「消息变化（新根因）」时发出；每分钟兜底 job 重复触发同类告警
#         不再刷屏。类别：GID 校验失败 "gid" / 容量失败 "capacity" / 其他取消息前 40 字符。
# P2-4/P2-10：cloudsaver.save 幂等 + save_task_id 记录。TransferQueue 新增
#         save_task_id 列（迁移 0002），save 一受理即落库；重试时若非空则跳过
#         save，直接 _get_link_wait_visible 等落盘/取直链，防重复转存。
# P2-5  ：retry_count 增量改 CAS 条件更新（WHERE 含 retry_count=读到的旧值），
#         替代「读-改-写」；recovery 并发回退不丢增量，CAS 未命中本轮跳过不计数。
# P2-6  ：GID 来源校验合并 active + waiting 队列（aria2.tell_waiting）；
#         waiting 中陌生任务同样阻断转存并告警。
# P2-9  ：paused 不再刷新 updated_at，由 recover_stale_tasks 按
#         episode_state_timeout_hours 超时回退 queued + 清理残留。
# P3-2  ：quota 拒绝累计告警阈值 _QUOTA_REJECT_ALERT_THRESHOLD=5。容量不足更新
#         后累计次数 ≥5 → flow_error 告警（category/bucket 均 "capacity"，与
#         "容量数据不可用"共享 P2-2 节流，10 分钟内同类只 notify 一次）。
# P3-6  ：media.status=downloading 写入者。步骤 4 抢占成功 → tracking→downloading
#         （WHERE status='tracking' 不覆盖 paused）；_complete_download done、
#         _fail_download 终态 failed、转存失败终态 failed 后经 _sync_media_status
#         检查该 media 无任何进行中 es（queued/transferring/downloading）→ 回 tracking。
# ---------------------------------------------------------------------------
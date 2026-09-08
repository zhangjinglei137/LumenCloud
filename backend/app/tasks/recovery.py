"""
recover —— 超时任务恢复（影视下载两队列重设计 §4.2 全节点超时回退）。

- recover_on_boot()      启动恢复（lifespan startup 阶段：init_db 之后、scheduler.start 之前）
- recover_stale_tasks()  核心逻辑：运行期兜底（scheduler job recover_stale，每 1h）
                         与启动恢复共用同一实现

操作对象：download_queue 单表（承接旧 episode_state + transfer_queue + download_task
三表职责）。扫描 status∈('transferring','downloading','scrape','library') 且
updated_at 超时的记录 → CAS 回退 status='pending' + node_attempt++ + retry_count++，
随后 best-effort 清理夸克残留（alist.remove，_split_quark_path 拆路径）与 aria2 任务
（aria2.remove，gid 存在时）；失败仅记录，不阻塞回退。

阈值分级（议会 P0 裁决，§4.2）：
  - transferring/downloading：system_config `episode_state_timeout_hours`（默认 2h）
  - scrape：                 system_config `scrape_revert_timeout_hours`（默认 4h）
  - library：                system_config `library_revert_timeout_hours`（默认 6h），
                             且回退前先做一次 Emby 收录确认——已收录 → 直接
                             library_check._finalize_done 置 done（不回退，防 Emby 慢
                             收录被误杀 → 重复转存/重复占容量）；Emby 故障 → 本轮跳过
                             不误判（与 library_check 600s 轮询口径解耦）。

回退动作（CAS 条件更新，防并发）：WHERE id 且 status=阶段①快照 且
retry_count=阶段①快照 → status='pending', node_attempt++, retry_count++,
node_error=超时原因, save_task_id=None（防「已受理未落盘」被当完成导致盲等死循环）,
error=原因, node_started_at=None, node_finished_at=None。≥_RETRY_LIMIT 的终态语义由
transfer 消费端统一处理（§4.2：retry_count≥3 → failed 在 transfer 失败路径）——
recovery 只置 pending + 计数，不做终态判定。

B-3 语义（清理副作用后置）：夸克残留删除与 aria2.remove 均在 CAS 成功且事务提交后
执行——CAS 冲突/未回退的行绝不清理（防误杀仍被并发推进的任务）；失败仅 warning 记录。

幂等：回退后 status='pending' 不再命中进行中态候选，可重复执行。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.config import settings
from app.database import async_session
from app.models import DownloadQueue, Media, SystemConfig
from app.services import alist, aria2, emby
from app.tasks import record_task_run

logger = logging.getLogger(__name__)

# 执行状态机进行中四态（§4.2）：全节点可超时回退
_REVERTABLE_STATUSES = ("transferring", "downloading", "scrape", "library")

# 阈值分级配置键（system_config 优先，默认值仅 fallback）
_EPISODE_TIMEOUT_CONFIG_KEY = "episode_state_timeout_hours"
_SCRAPE_TIMEOUT_CONFIG_KEY = "scrape_revert_timeout_hours"
_LIBRARY_TIMEOUT_CONFIG_KEY = "library_revert_timeout_hours"
# 独立阈值默认值（议会 P0 裁决：scrape 4h / library 6h）
_SCRAPE_TIMEOUT_DEFAULT_HOURS = 4.0
_LIBRARY_TIMEOUT_DEFAULT_HOURS = 6.0


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _deadline(hours: float) -> datetime:
    return _now() - timedelta(hours=hours)


async def _load_timeout_hours(config_key: str, default: float) -> float:
    """读取超时阈值（system_config 键优先；缺失/非法/异常 → 传入默认值）。"""
    try:
        async with async_session() as session:
            row = await session.get(SystemConfig, config_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 失败，用默认超时 %.1fh: %s", config_key, default, exc)
        return default
    if row is None or not row.value:
        return default
    try:
        return float(row.value)
    except (TypeError, ValueError):
        logger.warning("%s 非数值 %r，用默认超时 %.1fh", config_key, row.value, default)
        return default


def _split_quark_path(path: str) -> tuple[str, list[str]]:
    """把夸克完整路径拆为 (dir, [name])，适配 alist.remove(names, dir) 契约。"""
    path = (path or "").strip()
    if not path:
        return "/", []
    path = path.rstrip("/")
    if "/" in path:
        dir_part, name = path.rsplit("/", 1)
        return (dir_part or "/") + "/", [name]
    return "/", [path]


async def _cleanup_quark(quark_path: str) -> None:
    """清理夸克残留（§4.2：回退后清理残留）。异常由调用方 try/except 兜底。"""
    dir_part, names = _split_quark_path(quark_path)
    if names:
        await alist.remove(names, dir_part)


async def _library_already_collected(row: DownloadQueue) -> bool | None:
    """library 超时回退前的 Emby 收录确认（§4.2 议会 P0 裁决：回退前先确认，已收录不回退）。

    - True  → Emby 已收录该集 → 直接 library_check._finalize_done 置 done（删夸克 +
              done + 通知 + media 回退 + 续跑），不进入回退；
    - False → Emby 未收录（find_emby_id 未命中 / 剧集仍在遗漏集）或无法确认
              （tmdb_id 缺失 / media 不存在）→ 正常回退 pending；
    - None  → Emby 故障（find_emby_id / get_missing_episodes 抛异常）→ 本轮跳过
              不误判（留给 library_check 30s 轮询继续判定）。
    """
    from app.tasks import library_check as lc  # 延迟导入：复用集级判定与 finalize
    from app.tasks import transfer as transfer_mod  # 延迟导入：防循环

    async with async_session() as s:
        media = await s.get(Media, row.media_id)
    if media is None or media.tmdb_id is None:
        return False
    try:
        emby_id = await emby.find_emby_id(media.tmdb_id, media.title)
    except Exception as exc:  # noqa: BLE001  Emby 故障
        logger.warning(
            "[recover] media=%s library 收录确认失败（本轮跳过，不误判回退）: %s",
            row.media_id, exc,
        )
        return None
    if not emby_id:
        return False
    if (media.media_type or "").strip().lower() != "movie":
        try:
            missing = await emby.get_missing_episodes(emby_id)
        except Exception as exc:  # noqa: BLE001  Emby 故障
            logger.warning(
                "[recover] media=%s library 遗漏集确认失败（本轮跳过，不误判回退）: %s",
                row.media_id, exc,
            )
            return None
        missing_codes: set[str] = {str(ep.get("code")) for ep in missing if ep.get("code")}
        if lc._episode_in_missing(row.episode, missing_codes):
            return False  # 仍在遗漏集 → Emby 尚未收录 → 回退
    # 已收录 → 直接 finalize done（不回退）
    await lc._finalize_done(
        row.id, row.media_id, row.episode, row.file_name, row.quark_path, transfer_mod
    )
    return True


async def recover_stale_tasks() -> int:
    """全节点超时回退（§4.2）：transferring/downloading/scrape/library → pending。

    流程（避免事务内网络 IO 长事务，council P2 两阶段 + B-3 强化）：
      阶段① 只读快照查询四态候选（status / retry_count 快照），显式覆盖 NULL
             updated_at（缺省用 enqueued_at 兜底，仍缺失视为超时）；按状态阈值分级过滤；
      阶段② 事务外对 status='library' 候选逐个做 Emby 收录确认（网络 IO 不进事务）：
             已收录 → finalize done（不回退）；Emby 故障 → 本轮跳过不误判；
      阶段③ 新事务逐行 CAS 回退（WHERE status=快照 且 retry_count=快照，冲突行跳过
             不覆盖，与 transfer 失败路径 P2-5 CAS 同一并发协议）+ record_task_run；
      阶段④ 事务提交后对 CAS 成功行 best-effort 清理夸克残留 + aria2 移除（B-3：
             仅在 CAS 成功且提交后执行副作用，失败仅记录）。

    幂等可重复执行（回退后 status='pending' 不再命中）。返回实际回退条数
    （CAS 冲突行 / library 已收录完成行 / Emby 故障跳过行不计）。
    """
    ep_hours = await _load_timeout_hours(
        _EPISODE_TIMEOUT_CONFIG_KEY, settings.EPISODE_STATE_TIMEOUT_HOURS
    )
    scrape_hours = await _load_timeout_hours(
        _SCRAPE_TIMEOUT_CONFIG_KEY, _SCRAPE_TIMEOUT_DEFAULT_HOURS
    )
    lib_hours = await _load_timeout_hours(
        _LIBRARY_TIMEOUT_CONFIG_KEY, _LIBRARY_TIMEOUT_DEFAULT_HOURS
    )
    t0 = time.monotonic()  # Q8①：真实耗时

    # 阶段①：只读快照查询（四态全部取出，阈值分级在内存判定——三种 deadline 不同，
    # 无法在 SQL 统一过滤；候选即进行中任务，数量有限）
    async with async_session() as session:
        rows = (
            (
                await session.execute(
                    select(DownloadQueue).where(
                        DownloadQueue.status.in_(_REVERTABLE_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

    def _ts(r: DownloadQueue) -> datetime:
        # updated_at 缺失 → enqueued_at 兜底；均缺失 → 视为最旧（超时）
        return r.updated_at or r.enqueued_at or datetime.min

    candidates: list[tuple[DownloadQueue, float]] = []
    for r in rows:
        if r.status == "scrape":
            ddl, hours = _deadline(scrape_hours), scrape_hours
        elif r.status == "library":
            ddl, hours = _deadline(lib_hours), lib_hours
        else:
            ddl, hours = _deadline(ep_hours), ep_hours
        if _ts(r) < ddl:
            candidates.append((r, hours))
    if not candidates:
        logger.info("[recover] 无超时任务")
        return 0

    # 阶段②：library 超时候选事务外 Emby 收录确认（网络 IO 不进事务，B-3 前置确认）
    to_revert: list[tuple[DownloadQueue, float]] = []
    finalized, skipped = 0, 0
    for r, hours in candidates:
        if r.status != "library":
            to_revert.append((r, hours))
            continue
        try:
            confirmed = await _library_already_collected(r)
        except Exception as exc:  # noqa: BLE001  finalize 过程异常 → 保守跳过本轮
            logger.warning("[recover] library 收录确认/收尾异常，本轮跳过 %s: %s", r.id, exc)
            skipped += 1
            continue
        if confirmed is True:
            finalized += 1  # 已收录 → finalize done，不回退
            continue
        if confirmed is None:
            skipped += 1  # Emby 故障 → 本轮跳过不误判
            continue
        to_revert.append((r, hours))

    # 阶段③：新事务逐行 CAS 回退（rows 为上一 session 快照，必须 execute(update)
    # 按 id 更新）。CAS 语义：WHERE 含 status+retry_count 双快照——并发方已推进状态
    # （如 transfer 把 downloading→scrape）或已回退（retry_count 已变）的行 rowcount=0
    # → 跳过不覆盖，交由并发方/下一轮 job 推进。
    now = _now()
    reverted = 0
    cas_conflicts = 0
    to_clean: list[tuple[str | None, str | None]] = []  # (quark_path, aria2_gid)
    async with async_session() as session:
        async with session.begin():
            for r, hours in to_revert:
                error = f"超时回退（{hours:g}h 无进展）"
                r_upd = await session.execute(
                    update(DownloadQueue)
                    .where(
                        DownloadQueue.id == r.id,
                        DownloadQueue.status == r.status,  # 快照门控：状态未被并发推进
                        DownloadQueue.retry_count == r.retry_count,  # CAS：计数未被并发自增
                    )
                    .values(
                        status="pending",
                        node_attempt=DownloadQueue.node_attempt + 1,
                        retry_count=DownloadQueue.retry_count + 1,
                        node_error=error,
                        # P0-1（transfer 失败路径同款语义）：回退即失败重试，清空
                        # save_task_id——防「已受理未落盘」被当完成导致盲等死循环
                        save_task_id=None,
                        node_started_at=None,
                        node_finished_at=None,
                        error=error,
                        updated_at=now,
                    )
                )
                if r_upd.rowcount == 0:
                    cas_conflicts += 1
                    logger.info(
                        "[recover] download_queue %s 回退 CAS 冲突"
                        "（状态/计数已被并发推进），跳过回退", r.id,
                    )
                    continue
                reverted += 1
                # B-3：CAS 成功才收集副作用（先校验、后副作用）；冲突行不入列
                to_clean.append((r.quark_path, r.aria2_gid))

            message = f"恢复 {reverted} 条超时任务（回退 pending + 计数）"
            if finalized:
                message += f"；library 已收录直接完成 {finalized} 条（不回退）"
            if skipped:
                message += f"；Emby 故障/异常跳过 {skipped} 条"
            if cas_conflicts:
                message += f"；CAS 冲突跳过 {cas_conflicts} 条（状态已被并发推进）"
            await record_task_run(  # Q8①：真实耗时
                session, "recover", "success", message, None,
                duration_seconds=time.monotonic() - t0,
            )

    # 阶段④：CAS 成功且事务提交后 best-effort 清理副作用（B-3；失败仅记录不阻断）
    cleaned = 0
    for quark_path, gid in to_clean:
        if quark_path:
            try:
                await _cleanup_quark(quark_path)
                cleaned += 1
            except Exception as exc:  # noqa: BLE001  清理失败仅记录，不阻塞回退
                logger.warning("[recover] 清理夸克残留失败 %s: %s", quark_path, exc)
        if gid:
            try:
                await aria2.client.remove(gid)
            except Exception as exc:  # noqa: BLE001  移除失败仅记录，不阻塞回退
                logger.warning("[recover] aria2 移除失败 %s: %s", gid, exc)

    logger.info(
        "[recover] 恢复完成：回退 %d 条超时任务（清理残留 %d 条，library 已收录完成 %d 条，"
        "Emby 故障跳过 %d 条）",
        reverted, cleaned, finalized, skipped,
    )
    return reverted


async def recover_on_boot() -> None:
    """启动恢复：lifespan startup 阶段调用核心逻辑（保持原行为，无返回值）。"""
    await recover_stale_tasks()
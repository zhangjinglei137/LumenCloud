"""
scan_all_media 巡检主流程（设计文档 §4.3 / 实施计划 §3.5）

阶段 2 范围：搜索 → 入队（不转存）。
阶段 3 改造：share-list 递归遍历到具体文件（_walk_share，对齐 n8n quarkRecursiveGetFiles，
           阶段 1 Q2 实证：文件夹 fid 不被生产版落盘，须逐文件转存）；
           入队成功后触发 transfer.trigger_transfer()（Task 6 起委托
            trigger_transfer_consume 互斥消费入口；transfer lane 未就绪静默跳过）。

入口：
- scan_media(media_id)   单影视巡检；API POST /api/media/{id}/scan 手动触发，
                         返回最近一条 task_run id（routers/media.py 契约）
- scan_all_media()       遍历到期 tracking/downloading 影视（per-media 间隔过滤）
- scan_all_media_job()   APScheduler job 包装（阶段2 不注册定时，仅保留供手动调用）

服务层调用（app.services，另一 lane 已产出）：
- emby.find_emby_id / emby.get_missing_episodes  → 防重基线（P11 模糊兜底内置）
- cloudsaver.search / cloudsaver.share_info / cloudsaver.share_list → 搜源/凭据/文件
"""
import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timezone

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session, engine
from app.models import DownloadQueue, Media, TaskQueue, TaskRun
# 注：episode_state / transfer_queue / download_task 旧三表已从 scan 移除——影视下载
# 两队列重设计：探测结果落 task_queue，经 promote 产出 download_queue（执行层），
# 防重权威源 = download_queue.UNIQUE(media_id, episode)。见 docs/影视下载两队列重设计.md §3。
from app.services import cloudsaver, config_store, emby, tmdb
from app.services.notifier import EVENT_FLOW_ERROR, NotifyEvent, notifier
from app.tasks import as_bool, get_config_value, record_task_run
# tasks 层公共纯函数（app.utils，仅标准库）：统一时间源与集级匹配函数
from app.utils import (
    episode_aired,
    fmt_episode,
    now_utc_naive as _now,
    parse_episode_num,
)

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

# queue-flow-rework Task 2 起：巡检入队只写 task_queue（status='ready'，转存凭据收集
# 完毕），不再同步 promote 双写 download_queue——下载队列随后从 task_queue 取件（Task 4）。
# Task 3 起：unmatched 静默机制移除——缺失集搜索失败不再写 status='unmatched' +
# silent_until，本轮跳过、下轮全局巡检自然重试（TaskQueue 状态集收敛为
# pending/ready/error/done）。scan 探测阶段由 _enqueue 事务内先查 task_queue 同键
# 记录（存在即跳过）防重；failed 需人工 retry，不可被 scan 自动重新入队——撞
# task_queue UNIQUE(media_id, episode) 即跳过。

# 巡检 5 阶段键（契约固定，前端按此渲染进度）：
#   check → Emby 基线（查缺/Emby 基线）；search → cloudSaver 搜索；
#   match → 遍历分享 + 文件名匹配 + 大小过滤 + 入队；enqueue → 入队阶段（并入 match 流程内标记）；
#   finish → 完成收尾（消息/耗时落库）。stage status 枚举：
#   wait（待办）/ process（进行中）/ done（完成）/ skipped（跳过）/ error（此阶段失败，供前端定位展示）。
# 巡检是快任务：内存打点，结束时一次性落库（运行中不实时写 DB）。
_PHASE_KEYS = ("check", "search", "match", "enqueue", "finish")

# 候选分享筛选上限（诊断实证：cloudSaver 搜索返回大量失效分享码——rank 排序前 20/60
# 全 share-info HTTP 500，真实有效分享（2c16748e7818 排名 #73）被失效码挤出前 20）。
# 语义对齐 n8n「遍历分享码直到找到」：不再在验证前硬截断前 20，而是放行排序后的
# 最多 _MAX_RANK_CANDIDATES 个候选，交由 _scan_one 逐个验证（失败跳过、继续后续）。
_MAX_RANK_CANDIDATES = 100   # rank 后放行上限（本次 230 去重候选内；防极端返回过大）
_MAX_SHARE_TRY = 80          # 单轮巡检最多验证候选数（80×~0.5s 串行 ≈ 40s 上限，
                             # 防止遍历 230 个过慢；成功分享也全 walk，不按成功数停）
# A4 全量模式单轮入队上限（少帅式搜索误匹配修复）：tv 未收录全量（Emby 未收录 +
# scan_baseline_required=False）时，搜索到的文件逐个入队——「少帅」一次入队 284 个
# 错误资源案例实证同名短剧/无关资源会撑爆入队。对齐 n8n 全量 MAX_BATCH 的放宽版
# （n8n 更严），防止一次入队爆炸；后续可做成 system_config 动态调。
_FULL_MODE_ENQUEUE_LIMIT = 20


class ScanSearchUnavailable(Exception):
    """cloudSaver 搜索整体不可用：全部搜索关键词调用均失败（静默降级无法区分）。

    选择自建异常而非复用 cloudsaver.CloudSaverUnavailable：
    - cloudsaver.search 内部已把各类底层错误统一包装为 CloudSaverUnavailable，再捕获
      后重抛同名异常会丢失「是本巡检关键词循环整体失败」这一语义层信息；
    - scan.py 需在 _scan_one 里对该异常做阶段级处理（phases.search=error、
      scan_detail.failed_phase="search"、message 人话），自建异常使捕获点语义唯一、
      与 check 阶段的 Emby 故障（直接 catch EmbyUnavailable 风格）对齐，也便于测试直接
      import 该异常构造确定性用例。
    """


def _new_phases() -> dict:
    """初始 5 阶段骨架：全部 wait，时间戳 None。"""
    return {
        key: {"status": "wait", "started_at": None, "finished_at": None}
        for key in _PHASE_KEYS
    }


def _ts_iso() -> str:
    """阶段打点时间：naive UTC ISO 字符串（可 null；快任务内存打点，结束一次性落库）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _phase_start(phases: dict, key: str) -> None:
    """阶段开始（wait → process，记 started_at）。"""
    phases[key].update(status="process", started_at=_ts_iso())


def _phase_done(phases: dict, key: str) -> None:
    """阶段完成（process/wait → done，记 finished_at；started_at 缺失时补同刻）。"""
    ts = _ts_iso()
    ph = phases[key]
    ph.update(status="done", finished_at=ts)
    if not ph.get("started_at"):
        ph["started_at"] = ts


def _phase_error(phases: dict, key: str) -> None:
    """阶段失败（→ error，记 finished_at；started_at 缺失时补同刻）。"""
    ts = _ts_iso()
    ph = phases[key]
    ph.update(status="error", finished_at=ts)
    if not ph.get("started_at"):
        ph["started_at"] = ts


def _phase_skip(phases: dict, key: str) -> None:
    """阶段跳过（仅 wait → skipped；正常短路径语义，如 downloading 本轮不入队）。"""
    if phases[key]["status"] == "wait":
        phases[key]["status"] = "skipped"


def _phase_skip_remaining(phases: dict, from_key: str) -> None:
    """从 from_key（含）起把尚未开始的阶段标记为 skipped（正常跳过收尾语义）。

    用于 paused/error 跳过、防重基线缺失跳过、无遗漏跳过等正常短路径：
    未执行到的阶段标 skipped（区别于 wait——运行结束后不应残留「待办」观感），
    出错路径的未达阶段保持 wait（由故障中止，非主动跳过）。
    """
    for key in _PHASE_KEYS:
        if key == from_key or _phase_index(key) >= _phase_index(from_key):
            _phase_skip(phases, key)


def _phase_index(key: str) -> int:
    return _PHASE_KEYS.index(key)


def _fail_phase(phases: dict) -> str | None:
    """意外异常时定位失败阶段：取最后一个 status='process' 的阶段键（倒序，finish 除外）。

    巡检大流程按 _PHASE_KEYS 顺序推进，同时只有一个阶段处于 process；
    异常发生在哪一段，该段必然仍为 process（前序已完成阶段均 done）。
    """
    for key in reversed(_PHASE_KEYS):
        if phases[key]["status"] == "process":
            return key
    return None


def _result_detail_skeleton(failed_phase: str | None = None,
                            search_status: str | None = None,
                            trigger_source: str | None = None) -> dict:
    """scan_detail 最小骨架（装配前失败时的 fallback，保证前端可解析 failed_phase）。

    search_status（可观测，供日志/排查；前端暂不读，加键不破坏既有结构）：
    - "ok"：搜索阶段有 ≥1 个关键词成功（后续可能无候选 → message 走缺集文案）
    - "failed"：全部搜索关键词失败（_search_and_rank 抛 ScanSearchUnavailable）
    - "no_candidates"：搜索成功但无任何候选分享

    归因键（scan-outcome-attribution D1/D1a）：所有结束分支（含四个提前 return 的
    短路分支）都必须落骨架 + `skip_reason`，否则前端与告警读不到本轮结论。
    - skip_reason：9 类取值见 _skip_message；正常产出（有入队/无需归因）为 None
    - gated_out：被已播门控剔除的集数与集号（count / episodes）
    - missing_aired_total：本轮已播缺失集数
    - trigger_source：auto / manual（手动触发留痕由 P2 填充，此处只预留键位）
    """
    return {
        "missing_total": 0,
        "missing_aired_total": 0,
        "gated_out": {"count": 0, "episodes": []},
        "skip_reason": None,
        "trigger_source": trigger_source,
        "enqueued": 0,
        "existing_skipped": 0,
        "size_filtered": 0,
        "unmatched": 0,
        "non_video": 0,
        "failed_phase": failed_phase,
        "search_status": search_status,
        # share-info 验证统计（诊断：cloudSaver 搜索返回大量失效分享码 share-info 500）。
        # 可观测字段，供日志/排查与 message 语义区分「候选全部失效」vs「候选可用但无匹配」。
        "share_info_ok": 0,
        "share_info_fail": 0,
        "walk_fail": 0,
        # 静默 unmatched 预过滤计数（queue-flow-rework Task 3 后恒 0——静默机制移除，
        # 缺失集不再静默跳过、下轮巡检自然重试；键保留兼容，JSON 键全集固定）
        "unmatched_silent_skipped": 0,
        "missing_items": [],
    }


def _result_message(*, enqueued: int, unmatched: int, size_filtered: int,
                    non_video: int, existing_skipped: int,
                    missing_episodes: list[str] | None = None,
                    share_info_all_failed_candidates: int = 0,
                    unrelated_filtered: int = 0, full_mode_limit: int = 0) -> str:
    """巡检结果「人话」消息（信息列改造）：保留计数 + 引导性结论。

    规则：
    - share_info_all_failed_candidates>0（候选分享全部验证失败）→ 优先报「搜索到 N 个
      候选分享，验证均失败（分享可能已失效/过期）」，区分于「搜索成功但无匹配」——
      诊断实证：cloudSaver 搜索返回大量失效分享码 share-info HTTP 500，此前误报
      「搜索无匹配候选」。该场景入队必为 0。
    - unrelated_filtered>0（A2 全量模式文件级校验拒绝数）→ 报「N 个无关/超集号文件过滤」。
    - full_mode_limit>0（tv 未收录全量限批生效）且 enqueued 达上限 → 追加限批提示。
    - enqueued>0         → 「已入队 N 个资源」开头，后接存在的
                          M 个文件未匹配 / K 个大小不符跳过 / J 个非视频 / 已有跳过
    - enqueued=0 & unmatched>0 → 未找到缺失集资源（搜索文件均不匹配：多为已收录旧集或其它版本，
                          可能尚未更新），括号补充其余过滤计数
    - enqueued=0 & unmatched=0 & 有过滤 → 未找到可入队资源 + 计数
    - 全部 0：缺集场景（missing_episodes 非空）→ 明示「未找到缺失集 … 的资源（搜索无匹配
      候选，资源可能尚未更新）」，杜绝误导性「无候选命中」；否则兜底「无候选命中」
    """
    if share_info_all_failed_candidates:
        return (
            f"搜索到 {share_info_all_failed_candidates} 个候选分享，验证均失败"
            "（分享可能已失效/过期），未能获取缺失集资源"
        )
    if enqueued > 0:
        parts = [f"已入队 {enqueued} 个资源"]
        if unmatched:
            parts.append(f"{unmatched} 个文件未匹配")
        if size_filtered:
            parts.append(f"{size_filtered} 个大小不符跳过")
        if unrelated_filtered:
            parts.append(f"{unrelated_filtered} 个无关/超集号文件过滤")
        if non_video:
            parts.append(f"{non_video} 个非视频")
        if existing_skipped:
            parts.append(f"{existing_skipped} 个已有任务跳过")
        if full_mode_limit and enqueued >= full_mode_limit:
            parts.append(f"已达全量模式单轮入队上限 {full_mode_limit}")
        return "；".join(parts) + "。"
    if unmatched > 0:
        msg = (
            f"未找到缺失集资源（搜索到 {unmatched} 个文件均不匹配，"
            "多为已收录旧集或其它版本，可能尚未更新）"
        )
        extra = []
        if size_filtered:
            extra.append(f"{size_filtered} 个大小不符跳过")
        if unrelated_filtered:
            extra.append(f"{unrelated_filtered} 个无关/超集号文件过滤")
        if non_video:
            extra.append(f"{non_video} 个非视频")
        if extra:
            msg += "（另有 " + "、".join(extra) + "）"
        return msg
    if size_filtered or non_video or existing_skipped or unrelated_filtered:
        parts = []
        if size_filtered:
            parts.append(f"{size_filtered} 个大小不符跳过")
        if unrelated_filtered:
            parts.append(f"{unrelated_filtered} 个无关/超集号文件过滤")
        if non_video:
            parts.append(f"{non_video} 个非视频")
        if existing_skipped:
            parts.append(f"{existing_skipped} 个已有任务跳过")
        return "未找到可入队资源：" + "、".join(parts) + "。"
    if missing_episodes:
        # 系统明确知道缺失集（missing_total>0），只是搜索无匹配候选 → 人话说明，
        # 勿再输出误导性「无候选命中」。
        return (
            f"未找到缺失集 {','.join(missing_episodes)} 的资源"
            "（搜索无匹配候选，资源可能尚未更新）"
        )
    return "无候选命中"


# skip_reason 枚举（design D1）：9 类互不混淆的巡检结论——status_paused / status_error /
# baseline_unavailable / airdate_unavailable / no_missing / not_aired / gated_out /
# no_candidates / skip_enqueue（文案见 _skip_message）


def _format_episode_list(codes: list[str] | tuple[str, ...] | None, limit: int = 10) -> str:
    """集号列表 → message 用短串（最多 limit 个，超出追加「等 N 集」）。

    仅约束人话文案；结构化数据（scan_detail.gated_out.episodes）保留完整列表。
    """
    codes = list(codes or ())
    shown = ",".join(codes[:limit])
    if len(codes) > limit:
        shown += f" 等 {len(codes)} 集"
    return shown


def _skip_message(reason: str, *, title: str | None = None, detail: str | None = None,
                  missing_aired_total: int = 0,
                  missing_episodes: list[str] | tuple[str, ...] | None = None,
                  gated_out_episodes: list[str] | tuple[str, ...] | None = None,
                  not_enqueued: int = 0) -> str:
    """按 skip_reason 生成结论文案（design D6：语义文案与 _result_message 的纯计数分离）。

    9 类 reason 各自一句、互不混淆；「无遗漏集」只用于真正的 no_missing，
    不得用于表达「搜不到资源」；「因下载中未入队」不得写成「已有任务跳过」。
    """
    name = f"「{title}」" if title else ""
    if reason == "status_paused":
        return f"media.status=paused，跳过巡检（{name}已暂停，本轮无需进一步动作）"
    if reason == "status_error":
        return f"media.status=error，跳过巡检（{name}状态异常，本轮无需进一步动作）"
    if reason == "baseline_unavailable":
        return (
            "Emby 未收录该剧集，防重基线强制（scan_baseline_required=True），本轮跳过"
            + (f"；{name}待 Emby 收录后恢复巡检" if title else "")
        )
    if reason == "airdate_unavailable":
        return f"TMDB 播出日期取数失败（{detail or '取数不可用'}），本轮跳过"
    if reason == "no_missing":
        return f"{name}无遗漏集（Emby 基线已覆盖），跳过巡检（本轮无需进一步动作）"
    if reason == "not_aired":
        return f"{name}缺失集均未播出，TMDB 无已播出集（未开播），本轮跳过"
    if reason == "gated_out":
        eps = _format_episode_list(gated_out_episodes) or "集号未知"
        return (
            f"{name}缺失集 {len(gated_out_episodes or ())} 集均未播出"
            f"（已播门控剔除：{eps}），本轮无需搜索"
        )
    if reason == "no_candidates":
        eps = _format_episode_list(missing_episodes)
        tail = f"（缺失集 {eps}）" if eps else ""
        return f"{name}存在已播缺失 {missing_aired_total} 集{tail}，本轮未获取到任何资源"
    if reason == "skip_enqueue":
        return (
            f"{name}因正在下载未入队 {not_enqueued} 集"
            f"（已播缺失 {missing_aired_total} 集）"
        )
    raise ValueError(f"未知 skip_reason: {reason}")

# 巡检告警冷却（design D3）：notifier 没有通用节流设施，故在 scan 维护模块级 TTL 表。
# key = f"{media_id}:{reason}"（reason 取 error / no_candidates），窗口 24h。
# 已知代价：进程重启后冷却丢失，最坏多推一条同类告警（连续轮次判定仍查 DB，准确）。
_SCAN_ALERT_COOLDOWN_SECONDS = 24 * 3600.0
_scan_alert_cooldown: dict[str, tuple[float, str]] = {}
# 连续 N 轮 no_candidates 告警阈值（system_config 可配；缺失/非法按默认 3）
_SCAN_NO_CANDIDATES_ALERT_ROUNDS_KEY = "scan_no_candidates_alert_rounds"
_DEFAULT_SCAN_NO_CANDIDATES_ALERT_ROUNDS = 3


def _scan_alert_allowed(key: str, bucket: str) -> bool:
    """同 key 同 bucket 在 24h 窗口内只允许告警一次（对齐 transfer._alert_cooldown）。

    TTL 清理：停留超过 2 倍窗口的条目不可能再命中，遍历删除防无界增长。
    """
    now = time.monotonic()
    for _key, (_ts, _b) in list(_scan_alert_cooldown.items()):
        if now - _ts > 2 * _SCAN_ALERT_COOLDOWN_SECONDS:
            _scan_alert_cooldown.pop(_key, None)
    last_ts, last_bucket = _scan_alert_cooldown.get(key, (0.0, None))
    if last_bucket == bucket and (now - last_ts) < _SCAN_ALERT_COOLDOWN_SECONDS:
        logger.info("[scan] 告警冷却中（24h 内同类重复 %s）", key)
        return False
    _scan_alert_cooldown[key] = (now, bucket)
    return True


async def _notify_scan_alert(title: str, body: str, media_id: int) -> None:
    """发巡检告警；任何通知异常只记日志，不影响巡检结果落库。"""
    try:
        await notifier.notify(NotifyEvent(
            event_type=EVENT_FLOW_ERROR,
            title=title,
            body=body,
            recipient=None,
            extra={"media_id": media_id},
        ))
    except Exception:  # noqa: BLE001  告警失败不得反噬巡检
        logger.exception("[scan] media=%s 巡检告警发送失败", media_id)


async def _alert_scan_error(media_id: int, title: str | None,
                            failed_phase: str | None, message: str) -> None:
    """巡检以 error 结束 → 立即告警（含影视名、失败阶段、原因），同因 24h 冷却。"""
    key = f"{media_id}:error"
    bucket = failed_phase or "error"
    if not _scan_alert_allowed(key, bucket):
        return
    await _notify_scan_alert(
        f"巡检失败：{title or media_id}",
        f"「{title or media_id}」巡检失败（阶段：{failed_phase or '未知'}）：{message}",
        media_id,
    )


async def _scan_no_candidates_alert_rounds() -> int:
    """读取连续无资源告警轮次阈值（system_config；缺失/非法回退默认 3）。"""
    try:
        async with async_session() as s:
            raw = await get_config_value(
                s, _SCAN_NO_CANDIDATES_ALERT_ROUNDS_KEY,
                _DEFAULT_SCAN_NO_CANDIDATES_ALERT_ROUNDS,
            )
        rounds = int(raw)
        return rounds if rounds > 0 else _DEFAULT_SCAN_NO_CANDIDATES_ALERT_ROUNDS
    except Exception:  # noqa: BLE001  配置读取失败按默认值运行
        return _DEFAULT_SCAN_NO_CANDIDATES_ALERT_ROUNDS


async def _recent_scan_details(media_id: int, limit: int) -> list[dict]:
    """该 media 最近 limit 条 scan_media 的 scan_detail（新→旧），用于连续轮次推导。

    连续轮次不靠内存计数：查 DB 保证进程重启后仍准确（design D3）。
    """
    async with async_session() as s:
        rows = (await s.execute(
            select(TaskRun.scan_detail)
            .where(TaskRun.media_id == media_id, TaskRun.task_type == "scan_media")
            .order_by(TaskRun.id.desc())
            .limit(limit)
        )).scalars().all()
    out: list[dict] = []
    for raw in rows:
        try:
            out.append(json.loads(raw) if raw else {})
        except (TypeError, ValueError):
            out.append({})
    return out


async def _maybe_alert_no_candidates(media_id: int, title: str | None,
                                     scan_detail: dict) -> None:
    """连续 N 轮 no_candidates 且 missing_aired_total>0 → 告警一次。

    - N 读 system_config（默认 3），连续轮次查该 media 最近 N 条 task_run 推导；
    - 冷却 key = f"{media_id}:no_candidates"，24h；
    - missing_aired_total 较上一轮下降 → 清除冷却键，使再次恶化能重新告警。
    """
    total = int(scan_detail.get("missing_aired_total") or 0)
    if total <= 0:
        return
    try:
        rounds = await _scan_no_candidates_alert_rounds()
        recent = await _recent_scan_details(media_id, rounds + 1)
        if len(recent) < rounds:
            return
        if any(r.get("skip_reason") != "no_candidates" for r in recent[:rounds]):
            return
        key = f"{media_id}:no_candidates"
        prev_total = (
            int(recent[1].get("missing_aired_total") or 0) if len(recent) > 1 else None
        )
        if prev_total is not None and total < prev_total:
            had_cooldown = key in _scan_alert_cooldown
            _scan_alert_cooldown.pop(key, None)
            logger.info(
                "[scan] media=%s 缺失集数量下降 %s→%s，清除无资源告警冷却",
                media_id, prev_total, total,
            )
            if had_cooldown:
                return
        if not _scan_alert_allowed(key, "no_candidates"):
            return
        await _notify_scan_alert(
            f"巡检告警：{title or media_id} 连续 {rounds} 轮未获取到资源",
            (
                f"「{title or media_id}」已连续 {rounds} 轮巡检存在已播缺失 {total} 集，"
                f"但未获取到任何资源；最近一次尝试：{_now():%Y-%m-%d %H:%M}。"
            ),
            media_id,
        )
    except Exception:  # noqa: BLE001  告警判定/发送失败不影响巡检结果
        logger.exception("[scan] media=%s 无资源告警判定失败", media_id)

# 宽限轮次上限（沿用 retry_count，对齐 transfer._RETRY_LIMIT=3）：
# _resolve_done_states 中 done→failed / failed→failed 各消耗一轮（retry_count +1），
# 用尽后进入自动重下（auto_redownload_count < _AUTO_REDOWNLOAD_LIMIT）或 failed 终态。
_DONE_FAIL_RETRY_LIMIT = 3
# Emby 侧删除后自动重下次数上限（宽限轮次用尽后按 retry_task 语义重置为 pending 的次数；
# 人工 retry_task 会清零该计数，重新给足预算）。达上限仍缺失 → failed 终态。
_AUTO_REDOWNLOAD_LIMIT = 3
# 转 failed 与达上限两种 error 文案（供测试与人工排查识别）
_DONE_FAIL_ERROR = "下载完成但 Emby 未入库，请人工确认"
_DONE_LIMIT_ERROR = "下载完成但 Emby 未入库已达循环上限，请人工核实 Emby 端"
# _resolve_done_states 只接管本规则产生的失败行（两个标记任一命中即可）
_DONE_RULE_ERRORS = (_DONE_FAIL_ERROR, _DONE_LIMIT_ERROR)


# ---------------------------------------------------------------------------
# 三重匹配
# ---------------------------------------------------------------------------

# 公共化（tasks 层 utils）：_fmt_episode / _ep_num 实现迁至 app.utils
# （scan.py 与 library_check.py 原实现完全一致，本文件实现原样搬移），
# 这里保留局部名称绑定使全部调用点不变；_RE_SXXEXX / _RE_CN_EP 常量保留。
_fmt_episode = fmt_episode
_ep_num = parse_episode_num


def match_missing_std(text: str, missing_keys: set[str]) -> str | None:
    """规范命名匹配（规则 1/2）：SxxExx / SxxExxx / 第N集（跨季按集号匹配）。

    与 match_missing 的规则 1/2 完全同源（2026-09 拆分：主循环改为两阶段，
    规则 3 纯数字兜底降级为「未被规范命名的缺失集」的最后手段，见 _scan_one）。
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


def _pure_numeric_match(text: str, missing_keys: set[str]) -> str | None:
    """纯数字兜底（规则 3）：文件名主体纯数字 + 缺失集全部同一季 → 构造集 key。

    双重收紧防歧义（与原 match_missing 规则 3 完全一致）：
      ① 文件名主体提取 1-3 位数字块（开头或独立数字块，前置 [xxx] 标签可忽略）；
      ② 缺失集全部属于同一季 S，且 SxxE{数字} ∈ missing_keys 才命中。
    多季缺失（{"S01E01","S02E05"}）、数字超范围、纯数字与缺失集不吻合 → None。
    """
    if not text or not missing_keys:
        return None
    basename = text.rsplit(".", 1)[0] if "." in text else text
    m = re.match(r"^(?:\[[^\]]*\])*(\d{1,3})(?=\D|$)", basename)
    if not m:
        return None
    ep = int(m.group(1))
    seasons = {_season_of_key(k) for k in missing_keys}
    if len(seasons) == 1:  # 多季缺失集：纯数字无法判定归属季 → 不匹配（防歧义）
        season = next(iter(seasons))
        key = _fmt_episode(season, ep)
        if key in missing_keys:
            return key
    return None


def match_missing(text: str, missing_keys: set[str]) -> str | None:
    """三重匹配缺失集：SxxExx / SxxExxx / 第N集（跨季按集号匹配）→ 纯数字兜底。

    保留原语义与签名（外部调用/既有单测兼容）：先规则 1/2（规范命名），
    未命中再规则 3（纯数字）。_scan_one 主循环已改两阶段调用拆分后的
    match_missing_std / _pure_numeric_match，本函数供独立场景/测试复用。

    纯数字兜底（凡人修仙传 S01E190 案例）：网盘资源文件常为纯数字命名
    （`190.mkv`、`189.mkv`），既非 SxxExx 也非「第N集」——若不兜底，真实有效
    分享即使遍历到也匹配不到。双重收紧防歧义：
      ① 文件名主体提取 1-3 位数字块（开头或独立数字块，前置 [xxx] 标签可忽略）；
      ② 缺失集全部属于同一季 S，且 SxxE{数字} ∈ missing_keys 才命中。
    多季缺失（{"S01E01","S02E05"}）、数字超范围、纯数字与缺失集不吻合 → None，
    不影响既有三条规则（放在最后作为兜底）。
    """
    key = match_missing_std(text, missing_keys)
    if key is not None:
        return key
    return _pure_numeric_match(text, missing_keys)


# ---------------------------------------------------------------------------
# Emby 防重基线（§4.3 步骤2 / P11）
# ---------------------------------------------------------------------------

async def _emby_missing_codes(media) -> list[dict | None] | None:
    """Emby 防重基线。

    返回缺失集列表；**None 表示基线不可用**（Emby 未收录该剧，无法探明遗漏）。
    - tv    → 缺失集 dict 列表，形如
      ``{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z" | None}``
      （code 为空/N提交被过滤；premiere_date 供 _filter_aired 已播出门控兜底）
    - movie → 已在库 []（无遗漏）；整部缺失 [None]（全量模式，episode=文件名）
    - tv 未收录 → 由配置项 scan_baseline_required 分流：
        True（强防重，旧行为）→ None（主流程本轮跳过，防盲入占中转空间）；
        False（默认软处理）→ [None]（全量模式等价表达：后续对每个搜索到的具体
        文件都视为缺失集入队——scan 搜索链 _walk_share 只收集具体文件并受
        max_files 与大小过滤约束，「盲入整部剧」顾虑已大幅缓解）。
    """
    emby_id = await emby.find_emby_id(media.tmdb_id, media.title)  # P11 二次模糊兜底内置
    # 回写 media.in_emby（影视库「未入库」误显示修复：创建时静态 False，巡检按 Emby
    # 实际收录更新；回写失败静默保留原值，绝不阻断巡检主流程）
    try:
        async with async_session() as s2:
            m2 = await s2.get(Media, media.id)
            if m2 is not None and bool(m2.in_emby) != (emby_id is not None):
                m2.in_emby = emby_id is not None
                await s2.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scan] media=%s in_emby 回写失败（忽略）: %s", media.id, exc)
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
    # 契约扩展（Task 4）：返回 dict 携带 premiere_date，供 _filter_aired 已播出门控
    # 以 Emby 首播日期作为 TMDB 之外的第二数据源兜底（TMDB 该集无数据时回退）。
    return [
        {"code": ep.get("code"), "premiere_date": ep.get("premiere_date")}
        for ep in missing if ep.get("code")
    ]


async def _filter_aired(media, missing) -> tuple[list[dict | None], set[int]]:
    """已播出门控：返回 (kept 缺失项, 本季取数失败的季集合)。

    - None 哨兵项原样保留（全量/电影）；
    - movie 不套用门控；
    - tv：按季取 TMDB air_date，逐集 episode_aired；TMDB 无该集时回退 Emby
      premiere_date；两源皆无 → 未播出剔除；
    - 某季 TMDB 取数失败 → 记入 failed（该季集不保留），由调用方决定 fail-safe；
    - media.tmdb_id 缺失 → 仅用 Emby premiere_date 单源判定（降级）。
    """
    from datetime import date as _date

    kept = []
    failed = set()
    seasons = {}
    for m in missing:
        if m is None or not m.get("code"):
            kept.append(m)  # None 哨兵原样保留（全量/电影）
            continue
        if (media.media_type or "").lower() == "movie":
            kept.append(m)
            continue
        s = _season_of_key(m["code"])
        seasons.setdefault(s, []).append(m)
    if (media.media_type or "").lower() == "movie":
        return kept, failed
    today = _date.today()
    for s, items in seasons.items():
        air_map = None
        if media.tmdb_id is not None:
            air_map = await tmdb.get_tv_season_air_dates(media.tmdb_id, s)
        if media.tmdb_id is not None and air_map is None:
            # 该季 TMDB 取数失败 → 记 failed（该季集不保留），由调用方 fail-safe
            failed.add(s)
            logger.warning(
                "[scan] media=%s 季 %s TMDB air_date 取数失败，本轮跳过该季", media.id, s,
            )
            continue
        for m in items:
            ep = _parse_episode_number(m["code"])
            air = (air_map or {}).get(ep) if (air_map is not None and ep is not None) else None
            if air is not None:
                if episode_aired(air, today):
                    kept.append(m)
            elif episode_aired(m.get("premiere_date"), today):
                kept.append(m)  # TMDB 无该集 → Emby 兜底
            # 否则剔除（未播出）
    return kept, failed


async def _resolve_done_states(media, missing_keys: set[str], movie_missing: bool) -> int:
    """done / 本规则失败行 防重解除与 Emby 侧删除后自动重下（P1-1 + change 车道 B / 3.1）。

    影视下载两队列重设计后防重权威源 = download_queue（UNIQUE(media_id, episode)，§3.2）。

    在 Emby 基线之后、搜索/入队之前执行（_scan_one 步骤 2b），接管范围：
    - status='done'；或
    - status='failed' 且 error/node_error 任一属于 _DONE_RULE_ERRORS
      （只接管本规则产生的失败，不碰其它失败类型）。

    对每条：
    - Emby 已确认收录（集不在 missing_keys，或 movie 且 movie_missing=False）→ 删除该行
      （done 与本规则 failed 行都适用），解除防重、修复失败行泄漏。
    - 仍缺失时按宽限轮次（retry_count，上限 _DONE_FAIL_RETRY_LIMIT=3）递进：
      1) retry_count < 上限 → 保持/转 failed 并 retry_count+1（done→failed 与
         failed→failed 都推进宽限计数）；
      2) 宽限用尽且 auto_redownload_count < _AUTO_REDOWNLOAD_LIMIT → 按 queue.py
         retry_task 的重置语义置为 pending，并 auto_redownload_count+1（复用已有行，
         不新建 task_queue，也不引入 _trigger_transfer 之外的新机制）；
      3) 自动重下次数用尽 → 写 failed 终态（_DONE_LIMIT_ERROR 文案），仅在该行当前
         不是该终态时才写（避免无谓改写 updated_at）；用户仍可人工 retry_task。

    全程条件更新/删除（WHERE status=读取时状态，CAS）：未命中即跳过该条，不中断巡检；
    异常由调用方 try/except 兜底，不阻断巡检。

    返回本轮重置为 pending 的自动重下条数（供调用方立即触发转存消费，避免只依赖
    每分钟兜底 job）。
    """
    async with async_session() as tx:
        async with tx.begin():
            rows = (
                (
                    await tx.execute(
                        select(DownloadQueue).where(
                            DownloadQueue.media_id == media.id,
                            or_(
                                DownloadQueue.status == "done",
                                and_(
                                    DownloadQueue.status == "failed",
                                    or_(
                                        DownloadQueue.error.in_(_DONE_RULE_ERRORS),
                                        DownloadQueue.node_error.in_(_DONE_RULE_ERRORS),
                                    ),
                                ),
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0
            now = _now()
            confirmed = 0
            to_retry = 0
            redownloaded = 0
            at_limit = 0
            for dq in rows:
                if movie_missing or dq.episode in missing_keys:
                    if (dq.retry_count or 0) < _DONE_FAIL_RETRY_LIMIT:
                        # 宽限期内：保持/转 failed 并推进宽限计数（done→failed / failed→failed）
                        r_dq = await tx.execute(
                            update(DownloadQueue)
                            .where(
                                DownloadQueue.media_id == media.id,
                                DownloadQueue.episode == dq.episode,
                                DownloadQueue.status == dq.status,
                                DownloadQueue.retry_count < _DONE_FAIL_RETRY_LIMIT,
                            )
                            .values(
                                status="failed",
                                error=_DONE_FAIL_ERROR,
                                retry_count=DownloadQueue.retry_count + 1,
                                updated_at=now,
                            )
                        )
                        if r_dq.rowcount > 0:
                            to_retry += 1
                        continue
                    if (dq.auto_redownload_count or 0) < _AUTO_REDOWNLOAD_LIMIT:
                        # 宽限用尽 → 按 retry_task 语义重置 pending（计数 +1，复用已有行）
                        r_dq = await tx.execute(
                            update(DownloadQueue)
                            .where(
                                DownloadQueue.media_id == media.id,
                                DownloadQueue.episode == dq.episode,
                                DownloadQueue.status == dq.status,
                                DownloadQueue.retry_count >= _DONE_FAIL_RETRY_LIMIT,
                                DownloadQueue.auto_redownload_count < _AUTO_REDOWNLOAD_LIMIT,
                            )
                            .values(
                                status="pending",
                                retry_count=0,
                                node_attempt=0,
                                node_error=None,
                                node_started_at=None,
                                node_finished_at=None,
                                save_task_id=None,
                                save_attempt_at=None,
                                error=None,
                                enqueued_at=now,
                                updated_at=now,
                                auto_redownload_count=DownloadQueue.auto_redownload_count + 1,
                            )
                        )
                        if r_dq.rowcount > 0:
                            redownloaded += 1
                        continue
                    # 自动重下次数用尽 → failed 终态；已是该终态则不重复改写
                    if (dq.status == "failed" and dq.error == _DONE_LIMIT_ERROR
                            and dq.node_error == _DONE_LIMIT_ERROR):
                        continue
                    r_dq = await tx.execute(
                        update(DownloadQueue)
                        .where(
                            DownloadQueue.media_id == media.id,
                            DownloadQueue.episode == dq.episode,
                            DownloadQueue.status == dq.status,
                        )
                        .values(
                            status="failed",
                            node_error=_DONE_LIMIT_ERROR,
                            node_attempt=_DONE_FAIL_RETRY_LIMIT,
                            error=_DONE_LIMIT_ERROR,
                            updated_at=now,
                        )
                    )
                    if r_dq.rowcount > 0:
                        at_limit += 1
                else:
                    # Emby 已确认入库 → 解除防重：删除 download_queue（条件删除，P1-4：
                    # 与 update(WHERE status=读取时状态) 对称，防误删其他状态记录）
                    await tx.execute(
                        delete(DownloadQueue).where(
                            DownloadQueue.media_id == media.id,
                            DownloadQueue.episode == dq.episode,
                            DownloadQueue.status == dq.status,
                        )
                    )
                    confirmed += 1
            logger.info(
                "[scan] media=%s done/自动重下解除：Emby 确认入库删除 %d 条 / 宽限内转 failed %d 条"
                " / 重置 pending 自动重下 %d 条 / 自动重下用尽转终态 %d 条",
                media.id, confirmed, to_retry, redownloaded, at_limit,
            )
            return redownloaded


# ---------------------------------------------------------------------------
# cloudSaver 搜源 / 分享凭据 / 文件列表
# ---------------------------------------------------------------------------

def _expand_share_codes(results: list[dict]) -> list[dict]:
    """从 search 结果展开 quark 分享码候选：{title, share_code}（P8 正则提取）。

    按 share_code 去重（保留首次出现的 title）——cloudSaver 搜索接口会在多个
    channel/条目里重复返回同一失效分享码（如「凡人修仙传 (2020) 4k 高码率
    [更新190集]」同码出现 5 次），不去重会占用多个候选位、浪费逐码 share-info
    验证配额（诊断发现：rank 前 20 里失效码 83025fed147e 出现 5 次）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in results:
        title = item.get("title") or ""
        for cl in item.get("cloud_links") or []:
            if (cl.get("cloud_type") or "").lower() != "quark":
                continue
            m = _SHARE_CODE_RE.search(str(cl.get("link") or ""))
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
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

    size 处理（cloudSaver share-list 实测不返回单文件 size，阶段 1 Q2 结论 3）：
        - 优先取 it.size / it.fileSize；缺失（0 或 None）时留 size_unknown=True，
          待整棵遍历完成后按「分享总大小 / 视频文件数」均摊估算（share_size 缺失
          时不估算，保持 fail-closed 跳过）
    """
    files: list[dict] = []
    stopped = False
    # P2-12（延后项）：已下钻目录判重（按 pdir_fid 字符串），
    # 防分享目录循环引用 / share-list 异常重复返回导致死递归（根目录 "" 一并入集）
    visited: set[str] = set()
    # P2-5（Oracle 审查）：share-info 文件总大小字段名容错（fileSize/file_size/size/totalSize）
    share_size = int(info.get("fileSize") or info.get("file_size")
                     or info.get("size") or info.get("totalSize") or 0)

    async def list_dir(pdir_fid: str, depth: int, rel_path: str) -> None:
        nonlocal stopped
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
            # 移除「顶层单条目 → 分享总大小兜底」：cloudSaver share-list 实测不返回
            # 单文件 size（条目仅 fileId/fileIdToken/fileName/isFolder），单顶层文件夹
            # 分享会把分享总大小（如 361GB）赋给每个文件 → 5GB 上限误杀真实合理的单集
            # （凡人修仙传 190.mkv 案例）。未知大小统一留待 walk 完成后按
            # 「分享总大小 / 文件数」均摊估算（见函数末尾）。
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

    # 均摊估算：cloudSaver share-list 实测不返回单文件 size，对 size_unknown 文件用
    # share-info 总大小 / 视频文件数估算（4K 剧集各集大小相对均匀，361GB/190≈1.9GB，
    # 偏差对单集可接受）。share_size 缺失时不估算 → 保持 size_unknown=True 走
    # fail-closed 跳过（语义不变，宁缺勿滥仍对「完全无大小信息」生效）。
    est_targets = [f for f in files if f.get("size_unknown")]
    if est_targets and share_size > 0 and files:
        estimated = max(1, share_size // len(files))
        for f in est_targets:
            f["file_size"] = estimated
            f["size_unknown"] = False
            f["size_estimated"] = True
        logger.info(
            "[scan] %s %d 个文件 size 缺失，按分享总大小 %d / %d 文件均摊估算 %d 字节/文件",
            share_code, len(est_targets), share_size, len(files), estimated,
        )

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

# 单次巡检搜索关键词总量上限（季词 + 纯标题兜底 + 别名词）
_MAX_SEARCH_KEYWORDS = 5


def _season_of_key(key: str) -> int:
    m = re.match(r"[Ss](\d{1,2})", key or "")
    return int(m.group(1)) if m else 1


def _build_keywords(media, missing_keys: set[str], aliases: list[str] | None = None) -> list[str]:
    """搜索关键词：movie 用标题；tv 按缺失集所在季聚合（P4：一次巡检每个关键词只搜一次）。

    季词后追加纯标题兜底词（未匹配10 案例根因修复）：按季聚合的「标题 Sxx」召回不到
    标题不含 Sxx 的资源——如「凡人修仙传 (2020) 4K [更新190集]」这类按集连载动画，
    网盘资源多以年份/更新集数标识；纯标题词保证召回完整，再交由排序加权选择正确版本。

    aliases（可选）：tmdb 别名（见 tmdb.get_by_tmdb_id 的 aliases 键），归一化
    （小写、去空格，对齐 _normalize_aliases 口径）后作为独立关键词，插在季词之后、
    纯标题兜底词之前；movie 不加别名词。纯函数不读 media 模型属性（ORM 无 aliases 列），
    保持可测——aliases 由调用方传入（_resolve_media_aliases 负责解析，关键词/A1/
    排序三处同源）。
    总量裁剪到 _MAX_SEARCH_KEYWORDS。
    """
    title = (media.title or "").strip()
    if not title:
        return []
    aliases = [a for a in (aliases or []) if isinstance(a, str) and a.strip()]
    if media.media_type == "movie":
        return [title]
    seasons = sorted({_season_of_key(k) for k in missing_keys})
    kws = [f"{title} S{se:02d}" for se in seasons] if seasons else []
    if title not in kws:
        kws.append(title)  # 纯标题兜底词（排在季词之后）
    # 别名词（≤2 个，插到纯标题兜底词前）；总量裁剪到上限
    for a in aliases[:2]:
        a_norm = a.strip().lower().replace(" ", "")
        if a_norm not in kws:
            kws.insert(-1, a_norm)
    return kws[:_MAX_SEARCH_KEYWORDS]


async def _build_keywords_async(media, missing_keys: set[str]) -> list[str]:
    """取别名后构造关键词；别名解析失败降级（_resolve_media_aliases 兜底），绝不阻断搜索。"""
    aliases = await _resolve_media_aliases(media)
    return _build_keywords(media, missing_keys, aliases=aliases)


def _title_words(title: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", title or "") if len(w) >= 2]


def _rank_candidates(
    media, items: list[dict], year: str | int | None = None,
    aliases: list[str] | None = None, target_seasons: set[int] | None = None,
) -> list[dict]:
    """加分匹配排序：标题精确包含高分，部分词命中加分（忽略空格/大小写）。

    year（媒体首播年份，可选）：候选标题含该年份（如「凡人修仙传 (2020)」）时
    额外加权——同一剧名存在多版本（2020 动画版 vs 2025 新版）时优先召回与订阅
    一致的版本，避免版本错位导致全量未匹配（未匹配10 案例根因之二）。

    aliases（可选，任务 7 增量）：别名/原名成员命中 +8——资源常以别名/原名
    标识（如「Soul.Land」对「斗罗大陆」），主标题（中文）未命中的候选靠别名
    加权排前，避免正确资源被挤出有限验证配额（判定用 _title_member_hit，
    允许词间分隔符，与 A1 过滤口径一致）。

    target_seasons（可选，任务 7 增量）：候选标题季号与目标缺失集所属季一致
    +3——错部/错季资源（如 S01 对目标 S02）不加分，正确季号资源优先；空集 /
    None 自然不加分，与 _candidate_season_ok 空目标季集合降级放行口径对齐。
    """
    title_norm = (media.title or "").replace(" ", "").lower()
    words = _title_words(media.title)
    year_str = str(year) if year is not None else None
    alias_norms = [a.replace(" ", "").lower() for a in (aliases or [])]
    scored = []
    for it in items:
        name = str(it.get("title") or "").replace(" ", "").lower()
        score = 0
        if title_norm and title_norm in name:
            score += 10
        score += sum(2 for w in words if w in name)
        if year_str and year_str in name:
            score += 5
        # 别名/原名命中 +8（成员匹配，非子串；_title_member_hit 内部拒空串）；
        # 季号与目标一致 +3（空 target_seasons 不做加法）
        alias_hit = any(_title_member_hit(name, m) for m in (alias_norms or []) if m)
        if alias_hit:
            score += 8
        s = _season_of_title(name)
        if target_seasons and s is not None and s in target_seasons:
            score += 3
        scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored]


async def _media_year(media) -> int | None:
    """TMDB 首播年份（排序加权用）；失败静默降级 None，绝不阻断搜索。"""
    if media.tmdb_id is None or media.media_type is None:
        return None
    try:
        meta = await tmdb.get_by_tmdb_id(media.tmdb_id, media.media_type)
        return meta.get("year")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scan] media=%s TMDB 年份获取失败（排序不加权）: %s", media.id, exc)
        return None


# ---------------------------------------------------------------------------
# 全量模式文件级校验（少帅式搜索误匹配修复 A1/A2/A3/A4）
# ---------------------------------------------------------------------------

async def _media_total_episodes(media) -> int | None:
    """TMDB 剧集总集数（A3 集号范围校验用）；失败降级 None，绝不阻断扫描。

    仅 tv 需要（movie 无 number_of_episodes）；tmdb_id 为空 / media_type!="tv"
    直接 None。TMDB 回源失败 → warning + None（降级后 _full_mode_accept 不做
    集号上限，仅靠标准命名/含剧名+数字判定）。
    """
    if media.tmdb_id is None or (media.media_type or "") != "tv":
        return None
    try:
        # force_refresh：tmdb_cache 行可能被 search_multi 写入的不完整数据占据
        # （缺 number_of_episodes），缓存命中会拿到 None 导致集号范围校验失效——
        # 强制回源补全（少帅误匹配根因之一）。
        meta = await tmdb.get_by_tmdb_id(media.tmdb_id, "tv", force_refresh=True)
        return meta.get("number_of_episodes")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scan] media=%s TMDB 总集数获取失败（全量模式不做集号上限）: %s",
                       media.id, exc)
        return None


def _parse_episode_number(text: str) -> int | None:
    """解析文件名集号：SxxExx → 集号；第N集 → N；独立数字 token → N；无则 None。

    规则（A2/A3 判定基础，只返回一个明确集号，不猜测）：
    ① SxxExx（如 S01E30）→ 30；
    ② 第N集/话（如 第30集）→ 30；
    ③ 文件名中的「独立数字 token」→ 该数字。token 界定：数字块前后必须是
       空白 / 括号 / 方括号 / 点 / 连字符 / 行首行尾等分隔（即数字自身成词）——
       `30.mp4`→30、`[字幕组]116.mp4`→116、`少帅 30.mkv`→30、`少帅 9.mp4`→9；
       紧贴中文的集数标记（`少帅将我宠上天(99集)` 的 99 后接「集」非分隔）
       → 不解析（该形态另由 ② 处理；无「第」则不识别为集号）。其余 → None。
    """
    if not text:
        return None
    # ① SxxExx / SxxExxx
    m = _RE_SXXEXX.search(text)
    if m:
        return int(m.group(2))
    # ② 第N集/话（无季号，跨季按集号）
    m = _RE_CN_EP.search(text)
    if m:
        return int(m.group(1))
    # ③ 独立数字 token：文件名主体（去掉扩展名）内一个成词的数字块
    basename = text.rsplit(".", 1)[0] if "." in text else text
    m = re.search(r"(?:^|\s|\[|\(|\])(\d{1,3})(?=$|[\s.\]\)-])", basename)
    if m:
        return int(m.group(1))
    return None


# A1 标题成员匹配：成员词间允许跳过的候选分隔符（点/空格/括号/横线/下划线/全角
# 标点等）——英文资源常以点分词命名（"Soul.Land.S02E167"），归一化后别名
# "soulland" 需命中点分隔候选。
_A1_TITLE_SEP = " .()[]-—_【】"
_A1_TITLE_SEP_SKIP = f"[{re.escape(_A1_TITLE_SEP)}]?"  # 词间可选分隔符（成员匹配用）


def _is_cjk_char(c: str) -> bool:
    """CJK 统一表意文字（含扩展 A）判定。

    曾用于 A1 边界拒绝（紧贴中文拒绝）；裁定收敛后 _title_member_hit 仅拒绝
    罗马数字续作标记（CJK 紧贴视为合法打包资源，近旧子串语义）。保留定义
    以防后续复用，勿删除。
    """
    o = ord(c)
    return 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF


def _is_roman_numeral_char(c: str) -> bool:
    """罗马数字（Ⅰ-ⅿ、ↀ-ↈ）——续作常见「斗罗大陆Ⅱ」标记。"""
    o = ord(c)
    return 0x2160 <= o <= 0x2188


def _title_member_hit(cand_norm: str, member: str) -> bool:
    """成员命中 + 后续字符边界：member 在 cand 中出现（词间可跳过分隔符）且命中
    末尾后一字符非罗马数字 → True（串尾/分隔符/标点/年份/季号/紧贴中文均放行）。

    如别名 "soulland" 命中候选 "soul.land.s02e167.2160p.mkv"（"soul" 与 "land"
    间跳过点）；"斗罗大陆" 命中 "斗罗大陆Ⅱ绝世唐门…" 后随「Ⅱ」（罗马数字）→
    拒绝（续作标记）；"斗罗大陆" 命中 "斗罗大陆S01E157…" 后随 "S"（季号标记）
    或 "少帅全集" 后随「全」→ 放行。边界仅拒绝罗马数字——CJK 紧贴视为合法
    打包资源（回归裁定：「少帅全集」「凡人修仙传动画版全集」须放行）。
    """
    if not member:
        return False
    pattern = _A1_TITLE_SEP_SKIP.join(re.escape(ch) for ch in member)
    for m in re.finditer(pattern, cand_norm):
        after = cand_norm[m.end():]
        if not after:
            return True  # 命中处为串尾
        if _is_roman_numeral_char(after[0]):
            continue  # 紧贴罗马数字（续作标记）→ 本处命中拒绝，继续找下一处
        return True  # 分隔符/标点/年份/季号/英文/数字/中文 → 放行
    return False


def _share_title_relevant(media_title: str, cand_title: str, aliases: list[str] | None = None) -> bool:
    """A1 分享标题相关性：剧名为空 → True（兜底不阻断）；否则归一化（去空格、lower）
    后与 [主标题] ∪ [别名集合] 做成员匹配（_title_member_hit，命中处后续字符边界：
    后随罗马数字续作标记（如「斗罗大陆Ⅱ」）拒绝，其余含紧贴中文均放行）。

    全部不中（如「凡人修仙传合集」对 title「少帅」）→ False，候选在排序前直接
    剔除。别名成员匹配允许词间分隔符（"soulland" ↔ "Soul.Land.S02E167" 的点）。
    aliases 可空（None/[] → 仅主标题成员）。同名短剧等标题相关的误匹配由 A2/A3
    文件级校验拦截。
    """
    if not media_title:
        return True
    cand_norm = (cand_title or "").replace(" ", "").lower()
    if not cand_norm:
        return True  # 候选标题为空：兜底放行，避免误杀无标题候选
    title_norm = media_title.replace(" ", "").lower()
    members = [
        title_norm,
        *(
            a.strip().lower().replace(" ", "")
            for a in (aliases or [])
            if isinstance(a, str) and a.strip()
        ),
    ]
    return any(_title_member_hit(cand_norm, m) for m in members if m)


def _normalize_alias_names(names: list[str] | None) -> list[str]:
    """别名集合统一归一化：strip + 小写 + 去空格 + 去 (YYYY) 括号版本后缀 + 去重，
    上限 20（对齐 services/tmdb._normalize_aliases 口径）。

    _resolve_media_aliases 的两个来源——tmdb.get_by_tmdb_id 详情 aliases
    （tmdb.py 已归一化，此处再归一化幂等）与 media.aliases（_media_aliases
    返回原始 JSON 值，未归一化）——在消费方（关键词构造 / A1 过滤 / 排序加权）
    保持同一语义（如 "Soul Land" / "soul land" / "Soul Land (2020)" → "soulland"）。
    """
    out: list[str] = []
    for a in names or []:
        if not isinstance(a, str):
            continue
        s = a.strip().lower().replace(" ", "")
        s = re.sub(r"\(\d{4}\)", "", s).strip()
        if s and s not in out:
            out.append(s)
    return out[:20]


def _media_aliases(media) -> list[str]:
    """从 media.aliases（JSON 数组字符串）解析别名集合；无属性/空/解析失败 → []。

    供 _search_and_rank A1 过滤等复用——tmdb_cache.aliases 以 JSON 数组字符串
    落库（可空，见 models.TmdbCache.aliases），历史脏数据/手改解析失败降级空
    列表，绝不阻断搜索（与 tmdb._read_cache 反序列化口径一致）。
    """
    raw = getattr(media, "aliases", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(a) for a in parsed if a]


async def _resolve_media_aliases(media) -> list[str]:
    """解析媒体别名集合——搜索关键词 / A1 过滤 / 排序加权的唯一别名来源（I-1 修复）。

    生产缺口背景：ORM Media 无 aliases 列，_media_aliases(media) 恒返回空集合，
    A1 过滤与排序此前拿不到别名，英文名候选（如「Soul.Land.S02E167」对「斗罗大陆」）
    仅凭主标题成员匹配失败 → 在 A1 层被误剔，change 核心收益在生产链路失效。

    解析顺序：
    - media.tmdb_id 有值：tmdb.get_by_tmdb_id 详情 aliases（list[str]，来自
      tmdb_cache.aliases 反序列化 / 详情回源归一化，见 services/tmdb）；
      ★ 缓存快路径命中但 aliases 为空（tmdb_cache 行可能由 search_multi 写入、
      aliases 列 NULL，search_multi 不落详情字段）→ 追加一次
      force_refresh=True 回源补全详情（also_known_as → _upsert_cache 落库
      aliases）——仅当缓存别名缺失时触发，不放大常规调用量；
      拉取失败 / force_refresh 后 aliases 仍为空 → 降级 _media_aliases(media)
      （警告日志，绝不阻断搜索）；
    - media.tmdb_id 为空：直接 _media_aliases(media)（测试层 SimpleNamespace 可经
      media.aliases 属性注入；ORM 无该列 → 空列表）。

    返回统一归一化（_normalize_alias_names：小写、去空格、去括号版本后缀、
    去重、上限 20）——tmdb 详情与 media.aliases 两来源在消费方语义一致。
    """
    raw: list[str] = []
    if media.tmdb_id:
        media_type = media.media_type or "tv"
        try:
            meta = await tmdb.get_by_tmdb_id(media.tmdb_id, media_type)
            raw = [str(a) for a in (meta.get("aliases") or []) if a]
            if not raw:
                # 缓存命中但 aliases 缺失（search_multi 写入的行 aliases 列 NULL）
                # → force_refresh 回源补全详情别名并刷新缓存（仅此场景追加一次调用）
                logger.info(
                    "[scan] media=%s tmdb 缓存 aliases 为空，force_refresh 回源补全",
                    getattr(media, "id", None),
                )
                meta = await tmdb.get_by_tmdb_id(
                    media.tmdb_id, media_type, force_refresh=True
                )
                raw = [str(a) for a in (meta.get("aliases") or []) if a]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[scan] media=%s 别名解析失败（降级 media.aliases 属性）: %s",
                getattr(media, "id", None), exc,
            )
            raw = []
    if not raw:
        raw = _media_aliases(media)
    return _normalize_alias_names(raw)


# ---------------------------------------------------------------------------
# 季号解析与候选硬校验（任务 6）
# ---------------------------------------------------------------------------

def _season_of_title(text: str) -> int | None:
    """候选标题季号：`_RE_SXXEXX` 的 Sxx 优先（"Soul.Land.S02E167"→2）；
    `第N季`→N；都无 → None（无法判季的候选按降级语义放行）。
    """
    m = _RE_SXXEXX.search(text or "")
    if m:
        return int(m.group(1))
    m = re.search(r"第\s*(\d{1,2})\s*季", text or "")
    if m:
        return int(m.group(1))
    return None


def _candidate_season_ok(cand_title: str, target_seasons: set[int]) -> bool:
    """候选季号硬校验：季号解析成功且不在目标季集合 → False（拒绝）；
    无季号 → True（降级放行，不误杀「凡人修仙传 (2020) 4K」类更新集资源）。

    空目标季集合（target_seasons 为空：movie_missing / tv 未收录全量路径下
    missing_keys 为空集）→ 整体降级放行——任何季号 `s in set()` 恒 False 会把
    带季号候选全拒，违反「不误杀更新集/合集」精神；与任务 7 评分侧
    `target_seasons and ...` 空集不加权口径对齐。
    """
    if not target_seasons:
        return True
    s = _season_of_title(cand_title)
    if s is None:
        return True
    return s in target_seasons


def _is_standard_ep_naming(text: str) -> bool:
    """标准集号命名：SxxExx / 第N集/话 任一命中即 True（A2 判定基础）。"""
    return bool(_RE_SXXEXX.search(text) or _RE_CN_EP.search(text))


def _full_mode_accept(media, file_name: str, total_episodes: int | None) -> bool:
    """A2/A3 全量模式文件级入队判定：True=入队 / False=拒绝（tv 未收录全量逐文件调用）。

    oracle 最终裁定——**强制可解析集号**，杜绝同名短剧/纯数字/无集号资源入队：
    1. 集号可解析且超 TMDB 总数 → False（拦「少帅将我宠上天(99集)」99>48）；
    2. 标准 SxxExx/第N集 命名且集号在范围内（或 TMDB 降级无上限）→ True；
    3. 含剧名 + 可解析数字且集号在范围内 → True（如「少帅 9.mp4」）；
    4. 其余（纯孤立数字 9.mp4、无集号无剧名、含剧名但集号超范围）→ False。

    total_episodes=None（TMDB 降级）时仅靠 2/3 的名称特征，不做集号上限
    （oracle 风险提示已权衡，MVP 接受：宁可从宽，避免误杀正常剧集文件）。
    """
    name_norm = (file_name or "").replace(" ", "").lower()
    title_norm = (media.title or "").replace(" ", "").lower()
    ep = _parse_episode_number(file_name)
    # 1) 集号超 TMDB 总数 → 拒绝（同名短剧集数 > 目标剧总集数即非目标剧）
    if ep is not None and total_episodes is not None and ep > total_episodes:
        return False
    # 2) 标准命名（SxxExx/第N集）且集号在范围内 → 入队
    #    （标准命名必然可解析出集号，ep 在此分支必非 None）
    if _is_standard_ep_naming(file_name):
        if total_episodes is None or (ep is not None and ep <= total_episodes):
            return True
    # 3) 含剧名 + 可解析数字（且集号在范围内）→ 入队（「少帅 9.mp4」）
    if (
        ep is not None
        and title_norm
        and title_norm in name_norm
        and (total_episodes is None or ep <= total_episodes)
    ):
        return True
    # 4) 其余一律拒绝
    return False


async def _search_and_rank(media, missing_keys: set[str]) -> list[dict]:
    """cloudSaver 搜索 → 展开分享码 → A1 分享标题过滤 → 加分匹配（TMDB 年份加权）→ 限数。

    - 多关键词并行（别名关键词 → asyncio.gather 并发搜索），
      结果合并后由 _expand_share_codes 按 share_code 去重（保留首现）——多个关键词
      召回同一失效分享码只占一个候选位，不浪费逐码验证配额。
    - 单关键词失败：记录 warning 并继续（一个词失败、其余成功 → 不中断整轮，
      保持既有降级语义）。
    - A1 分享标题过滤（斗罗大陆场景误匹配修复）：与 [主标题] ∪ [别名集合] 无成员
      命中（或命中处紧贴罗马数字续作标记）的分享候选在排序前直接剔除，剧名为空兜底
      放行——减少无关分享被验证/遍历的浪费；同名短剧等标题相关的误匹配由 A2/A3
      文件级校验（_full_mode_accept）拦截。
    - 季号硬校验（A1 之后）：候选标题季号（SxxExx 的 Sxx / 第N季）不在目标季集合
      （= {_season_of_key(k) for k in missing_keys}）→ 直接剔除（错部资源「斗罗大陆
      Ⅱ绝世唐门…S01E29」对目标 S02 场景）；无季号候选（如「凡人修仙传 (2020) 4K
      [更新190集]」更新集连载资源）降级放行不误杀；被拒候选 debug 级记录。
    - 全部关键词均失败（ok==0 且关键词数 ≥1）→ 抛 ScanSearchUnavailable：
      调用方（_scan_one）将 search 阶段标 error，区分「搜索故障」与「搜索成功但无候选」
      （后者返回 []，message 走缺集人话文案，不再误报「无候选命中」）。
    """
    aliases = await _resolve_media_aliases(media)  # 三处（关键词/A1/排序）唯一别名来源，只解析一次
    keywords = _build_keywords(media, missing_keys, aliases=aliases)
    raw: list[dict] = []
    ok = 0  # 成功关键词计数（调用无异常即计，空结果不算失败——区分「搜索成功但无候选」）

    async def _search_one(kw: str) -> tuple[str, list[dict] | None]:
        """单关键词搜索；成功返回 (kw, results)，失败返回 (kw, None) 并 warning。"""
        try:
            return kw, await cloudsaver.search(kw)
        except Exception as exc:
            logger.warning("[scan] cloudSaver 搜索 %s 失败: %s", kw, exc)
            return kw, None

    results = await asyncio.gather(*(_search_one(kw) for kw in keywords))
    for _kw, res in results:
        if res is not None:
            ok += 1
            raw.extend(res)
    if ok == 0 and keywords:
        # 所有搜索关键词都失败 → 搜索服务整体故障（静默降级会让 _scan_one 误以为
        # 「无缺集/无候选」），上抛由 _scan_one 记录 error + 阶段定位
        raise ScanSearchUnavailable("全部搜索关键词调用 cloudSaver 均失败")
    expanded = _expand_share_codes(raw)
    # A1 分享标题过滤：与 [主标题] ∪ [别名集合] 无成员命中（或命中处紧贴罗马数字
    # 续作标记）的候选直接剔除（排序前），减少无关分享被 share-info 验证/递归遍历的
    # 浪费；被过滤标题 info 级记录（量可能大，不刷屏）。别名集合与关键词/排序同源
    # （_resolve_media_aliases 解析一次，见函数首部）。
    kept: list[dict] = []
    for it in expanded:
        if _share_title_relevant(media.title, it.get("title") or "", aliases=aliases):
            kept.append(it)
        else:
            logger.info(
                "[scan] media=%s 分享标题与剧名无关，过滤候选: %s",
                media.id, (it.get("title") or "")[:60],
            )
    expanded = kept
    # 季号硬校验（A1 之后）：目标季集合由缺失集 key 解析；候选季号（SxxExx 的
    # Sxx / 第N季）解析成功且不在目标季集合 → 排序前直接剔除（错部/错季场景，
    # 如「斗罗大陆Ⅱ绝世唐门…S01E29」对目标 S02）；无季号候选（「凡人修仙传
    # (2020) 4K [更新190集]」更新集连载资源）降级放行不误杀。被拒候选 debug 级
    # 记录（量可能大，不刷屏）。
    target_seasons = {_season_of_key(k) for k in missing_keys}
    season_kept: list[dict] = []
    for it in expanded:
        cand_title = it.get("title") or ""
        if _candidate_season_ok(cand_title, target_seasons):
            season_kept.append(it)
        else:
            logger.debug(
                "[scan] media=%s 候选季号不在目标季集合，过滤候选: %s",
                media.id, cand_title[:60],
            )
    expanded = season_kept
    year = await _media_year(media)
    # 候选分享筛选：rank 排序后不再硬截断前 20（诊断：前 20/60 可能全是失效码，
    # 有效分享被挤出）——放行排序后最多 _MAX_RANK_CANDIDATES 个，验证/尝试上限由
    # _scan_one 按 share-info 成功数控制。排序带上 aliases（_resolve_media_aliases
    # 唯一来源，与关键词/A1 过滤同源）与 target_seasons（即下方季号硬校验用的同一
    # 集合）做别名/季号增量加权。
    return _rank_candidates(
        media, expanded, year=year, aliases=aliases, target_seasons=target_seasons,
    )[:_MAX_RANK_CANDIDATES]


# ---------------------------------------------------------------------------
# 大小过滤（§6.1）：media 覆盖 > system_config 全局 > settings 默认
# ---------------------------------------------------------------------------

async def _size_limits(session, media) -> tuple[float, float, float, float]:
    """返回 (ep_min, ep_max, movie_min, movie_max)，单位 GB。

    优先级：media 覆盖 > system_config 全局 > settings 默认；None/0 表示该侧不限制。
    """
    global_ep = await get_config_value(
        session, "max_episode_size_gb", settings.DEFAULT_MAX_EPISODE_SIZE_GB
    )
    global_movie = await get_config_value(
        session, "max_movie_size_gb", settings.DEFAULT_MAX_MOVIE_SIZE_GB
    )
    global_ep_min = await get_config_value(
        session, "min_episode_size_gb", settings.DEFAULT_MIN_EPISODE_SIZE_GB
    )
    global_movie_min = await get_config_value(
        session, "min_movie_size_gb", settings.DEFAULT_MIN_MOVIE_SIZE_GB
    )
    ep = media.max_episode_size_gb if media.max_episode_size_gb is not None else float(global_ep)
    movie = media.max_movie_size_gb if media.max_movie_size_gb is not None else float(global_movie)
    ep_min = (
        media.min_episode_size_gb
        if media.min_episode_size_gb is not None else float(global_ep_min)
    )
    movie_min = (
        media.min_movie_size_gb
        if media.min_movie_size_gb is not None else float(global_movie_min)
    )
    return ep_min, ep, movie_min, movie


async def _read_size_limits(media) -> tuple[float, float, float, float]:
    """独立短 session 读取大小过滤上下限（P1-1：主流程网络 IO 阶段不持有 DB session）。

    内部自行开启/关闭短 session 读取 system_config，不依赖外层长事务；
    media 为 detached 对象（expire_on_commit=False），其覆盖值属性可直接读取。
    """
    async with async_session() as s:
        return await _size_limits(s, media)


def _size_bounds_gb(media, ep_min: float, ep_max: float,
                    movie_min: float, movie_max: float) -> tuple[float, float]:
    """按媒体类型选生效的 (min_gb, max_gb)：movie 用电影组，其余用单集组。"""
    if media.media_type == "movie":
        return movie_min, movie_max
    return ep_min, ep_max


def _size_out_of_bounds(file_size: int, min_gb, max_gb) -> str | None:
    """大小越界判定：返回 "low"/"high"，放行返回 None。

    None/0 表示该侧不限制；边界值放行（< 下限 / > 上限才算越界）。
    主循环与纯数字兜底分支共用，保证两处语义一致。
    """
    if min_gb and file_size < min_gb * 1024 ** 3:
        return "low"
    if max_gb and file_size > max_gb * 1024 ** 3:
        return "high"
    return None


# ---------------------------------------------------------------------------
# 入队（事务内「检查并写入」+ UNIQUE 冲突捕获）
# ---------------------------------------------------------------------------

def _json_dumps(v):
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _episode_ref_key(episode_or_name: str) -> tuple[int | None, int] | None:
    """归一化集引用 → (season|None, ep)；无集号返回 None。

    供 download_queue 补集号防重（T8.8）使用：把多种集号表示收敛为可比元组。
    覆盖：
    - SxxExx / SxxExxx（'S01E01' / 'S1E1' / 'S01E100'）→ (season, ep)，与
      app.utils.fmt_episode 规范化（S 两位 + E 两位/三位）同源口径；
    - 第N集/第N话（'第1集'）→ (None, ep)，跨季按集号匹配；
    - 独立数字 token（'01'、'190.mkv' 等全量模式文件名键）→ (None, ep)。
    其余（电影键 'movie:标题' 等无集号形态）→ None（不参与集级防重）。
    """
    text = (episode_or_name or "").strip()
    if not text:
        return None
    m = _RE_SXXEXX.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    ep = _parse_episode_number(text)
    if ep is not None:
        return None, ep
    return None


def _same_episode_ref(a: str, b: str) -> bool:
    """两集引用是否同集（归一化比，T8.8 防重判定）。

    季号均已知 → (season, ep) 全等（防 S01E01↔S02E01 误判）；
    任一无季号 → 仅按集号跨季匹配（与 match_missing『第N集 跨季按集号』语义一致）；
    任一侧无法识别集号 → False（不拦截）。
    """
    ra, rb = _episode_ref_key(a), _episode_ref_key(b)
    if ra is None or rb is None:
        return False
    if ra[0] is not None and rb[0] is not None:
        return ra == rb
    return ra[1] == rb[1]


async def _enqueue(media_id: int, episode_key: str, file_name: str, file_size: int,
                   share_code: str, payload: dict, *,
                   size_estimated: bool = False) -> str:
    """巡检入队只写 task_queue（queue-flow-rework Task 2：只写队列，移除同步 promote 双写）。

    探测完成即产出：scan 搜索/匹配/大小过滤通过后，把探测结果快照写 task_queue
    （status='ready'——转存凭据收集完毕，等待下载队列取件）。同步 promote 双写
    download_queue 已移除（下载队列后续从 task_queue 取件，Task 4）；download_name
    落盘名生成一并移除（迁移至 Task 7）。

    size_estimated：file_size 是否为「分享总大小 / 文件数」均摊估算值（cloudSaver
    share-list 不返回单文件 size，_walk_share 估算后置标记）。取件/promote 全链路
    拷贝该标记，下载完成用 aria2 totalLength 回填真实大小后清除（Task 2）。

    幂等：事务内先查 task_queue 同键记录，命中则跳过；再查同 media 同 file_name
    记录（跨键防重，2026-09 重复下载事故修复：Emby 收录状态变化导致同一物理文件
    在两轮巡检产生不同防重键——全量模式文件名键 vs 标准模式 SxxExx 键——绕过
    UNIQUE(media_id, episode) 重复入队；同一文件无论以哪种键入队，第二次一律
    existing）；再查 download_queue 同 media 补集号归一化记录（T8.8 全量模式防重：
    该集跨文件名已在 download_queue 任意状态 → 视为已存在，不再重复入队——
    download_queue 为防重权威源，UNIQUE(media_id, episode)）；
    写入撞 UNIQUE(media_id, episode) 则捕获 IntegrityError 判定为并发冲突。
    返回 'enqueued' / 'existing' / 'conflict'。
    """
    async with async_session() as tx:
        try:
            async with tx.begin():
                has = (
                    await tx.execute(
                        select(TaskQueue.id).where(
                            TaskQueue.media_id == media_id,
                            TaskQueue.episode == episode_key,
                        )
                    )
                ).first()
                if has:
                    return "existing"

                # 跨键防重：同 media 下已有同 file_name 记录 → 视为已存在（同一物理文件，
                # 无论此前以文件名键还是 SxxExx 键入队，均不重复入队）。
                has_file = (
                    await tx.execute(
                        select(TaskQueue.id).where(
                            TaskQueue.media_id == media_id,
                            TaskQueue.file_name == file_name,
                        )
                    )
                ).first()
                if has_file:
                    return "existing"

                # 补集号归一化防重（T8.8）：同 media 下该集（归一化集号匹配，跨文件名）
                # 已在 download_queue 任意状态 → 视为已存在，不入队。download_queue 为防重
                # 权威源（UNIQUE(media_id, episode)），其 episode 列与 task_queue 同形态
                # （SxxExx 或全量模式文件名键），比对前统一经 _same_episode_ref 归一化。
                dq_eps = (
                    await tx.execute(
                        select(DownloadQueue.episode).where(
                            DownloadQueue.media_id == media_id,
                        )
                    )
                ).scalars().all()
                if any(_same_episode_ref(episode_key, dq_ep) for dq_ep in dq_eps):
                    return "existing"

                tx.add(TaskQueue(
                    media_id=media_id,
                    episode=episode_key,
                    file_name=file_name,
                    file_size=file_size,
                    size_estimated=size_estimated,
                    share_code=share_code,
                    status="ready",  # 凭据收集完毕，等待下载队列取件（queue-flow-rework Task 2）
                    pwd_id=payload.get("pwd_id") or payload.get("pwdId"),
                    stoken=payload.get("stoken"),
                    receive_code=payload.get("receive_code") or payload.get("receiveCode"),
                    fids=_json_dumps(payload.get("fids")),
                    fid_tokens=_json_dumps(payload.get("fid_tokens") or payload.get("fidTokens")),
                    folder_id=payload.get("folder_id") or payload.get("folderId")
                    or config_store.get("quark_default_folder", settings.QUARK_DEFAULT_FOLDER)
                    or None,
                    probe_attempt=0,
                    created_at=_now(),
                    updated_at=_now(),
                ))
                # 显式 flush 使 INSERT 立即执行（提交交由 `async with tx.begin()` 上下文
                # 管理器统一处理，不再显式 commit——审查 A17）。撞 UNIQUE(media_id, episode)
                # 时抛 IntegrityError 逃出 begin 块，由 begin 异常退出自动回滚（回滚清理
                # 本事务内 pending 对象，不残留半状态）——单条冲突跳过、整轮不中断。
                await tx.flush()
            return "enqueued"
        except IntegrityError:
            # 并发冲突补查 task_queue 记录（只写表，同键即已有任务；不做自动修复）。
            # 冲突已使写入事务回滚闭合（begin 上下文管理 rollback），补查在独立短事务
            # 内进行，不影响后续入队（整轮不中断）。
            async with tx.begin():
                try:
                    has_tq = (
                        await tx.execute(
                            select(TaskQueue.id).where(
                                TaskQueue.media_id == media_id,
                                TaskQueue.episode == episode_key,
                            )
                        )
                    ).first() is not None
                except Exception:  # noqa: BLE001
                    has_tq = None
            logger.warning(
                "[scan] media=%s episode=%s 并发冲突（UNIQUE），本轮跳过；task_queue 记录%s",
                media_id, episode_key,
                "存在（并发入队已完成，正常）" if has_tq else "不存在（请人工核查）",
            )
            return "conflict"


# queue-flow-rework Task 3：unmatched 静默机制已移除。缺失集搜索确认无资源后**不再**
# 写 status='unmatched' + silent_until（不再 2 天休眠）——本轮跳过，下一轮全局巡检
# 自然重试（TaskQueue 状态集收敛为 pending/ready/error/done，unmatched 不再产生）。
# scan_detail 保留 unmatched_marked / unmatched_silent_skipped 恒 0 键（JSON 结构稳定）。


async def _trigger_transfer() -> None:
    """入队成功后触发下载队列消费（queue-flow-rework Task 6 起：互斥事件消费入口）。

    transfer.trigger_transfer 自 Task 6 起委托 trigger_transfer_consume（带防重入锁的
    _admit_batch 消费轮：唤醒 quota_wait + TaskQueue 取件 + 有界准入）。fire-and-forget
    后台任务持引用防 GC，scan_media 立即返回 task_run_id；模块未就绪时静默跳过。
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


def trigger_scan_background(media_id: int, *, manual: bool = False) -> asyncio.Task:
    """单部巡检受理入口（受理建记录 + fire-and-forget 执行）。

    P2（design D5）：受理时先建 status='running' 的 task_run（trigger_source=manual/
    auto），再把 run_id 透传给 `_scan_one` 复用——**不得**再让 `_scan_one` 自建
    第二条记录。返回 asyncio.Task：路由 await 它即可拿到 run id（只等受理建记录，
    不等完整巡检，保持 E-1 防 504 语义）；巡检本身是另一个后台 task。
    既有 fire-and-forget 调用方（approvals / queue probe / add task）忽略返回值即可，
    受理与巡检仍会照常执行。内部持 per-media 锁（scan_media 自带）。

    manual=True（所有手动触发点：media 扫描按钮 / queue 探测 / 加集 / 审批通过）。
    queue-flow-rework Task 3 起：静默 unmatched 预过滤已移除，manual 不再改变
    _scan_one 行为（缺失集一律照常搜索，搜不到下轮自然重试）。
    """
    async def _run_scan(run_id: int) -> None:
        try:
            await scan_media(media_id, manual=manual, run_id=run_id)
        except Exception:  # noqa: BLE001  后台巡检异常：_scan_one 已收敛记录，此处只记日志
            logger.exception("[scan] 后台巡检异常 media=%s", media_id)

    async def _accept() -> int | None:
        try:
            run_id = await _create_scan_run(
                media_id, trigger_source="manual" if manual else "auto",
            )
        except Exception:  # noqa: BLE001  受理失败返回 None，由路由反馈 accepted=False
            logger.exception("[scan] media=%s 巡检受理建记录失败", media_id)
            return None
        if run_id is None:
            # media 不存在 / running 记录建立失败：不启动巡检，避免无主记录
            return None
        scan_task = asyncio.create_task(_run_scan(run_id))
        _background.add(scan_task)
        scan_task.add_done_callback(_background.discard)
        return run_id

    task = asyncio.create_task(_accept())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _media_lock(media_id: int) -> asyncio.Lock:
    return _scan_locks.setdefault(media_id, asyncio.Lock())


def _scan_lock_idle(lock: asyncio.Lock) -> bool:
    """锁是否无等待者（可安全从 _scan_locks 移除）。

    读取 asyncio.Lock._waiters 判断是否有等待中的调用方。该属性是 CPython 私有
    实现细节（3.14 懒初始化为 None / deque of futures），读取可能因实现变化抛异常
    （AttributeError 等）——此时**保守返回 False**（视为有等待者，保留锁）：宁可
    轻微泄漏锁对象（下轮 setdefault 复用），也不破坏并发互斥（等待者被丢下会产生
    「新锁 + 旧锁」并发巡检同一 media）。版本不兼容时每次读取失败都会 warning
    （该路径仅在 CPython 私有属性实现变化时触发，刷屏本身即暴露问题、便于及时
    升级适配，故不做节流）。返回 True（明确无等待者）才允许调用方 pop。
    """
    try:
        waiters = lock._waiters  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001  CPython 私有属性变化 → 保守保留锁
        logger.warning(
            "[scan] asyncio.Lock._waiters 读取失败（CPython 私有属性不兼容），"
            "保守保留 _scan_locks 锁（轻微泄漏，不破坏互斥）"
        )
        return False
    return not waiters


async def scan_media(media_id: int, *, manual: bool = False,
                     run_id: int | None = None) -> int | None:
    """单影视巡检（API /api/media/{id}/scan 手动触发入口），返回最近一条 task_run id。

    run_id（P2 design D5）：手动触发已在 `trigger_scan_background` 受理时建好
    status='running' 的记录，此处透传给 `_scan_one` 复用；None 表示定时路径，
    由 `_scan_one` 自建（trigger_source=auto）。

    manual（queue-flow-rework Task 3 起为兼容保留）：静默 unmatched 预过滤已移除，
    手动/定时触发行为一致——缺失集一律照常搜索，搜不到下轮自然重试。

    P2-11（延后项）：巡检完成后清理 _scan_locks 中该 media 的锁，防 media 删除后
    锁对象永久泄漏。清理必须保证不破坏并发互斥（等待中的调用方不可被丢下）。

    安全判定（单线程事件循环）说明：finally 在 `async with lock` 块**内**执行，此刻
    当前协程仍持有锁（release 在 finally 之后），持有期间到达的并发调用方全部进入
    asyncio.Lock 内部 `_waiters` 队列——因此仅当 `_waiters` 为空（无等待者）时才移除：
      - 有等待者：保留锁。等待者仍持有该锁引用，释放后照常串行执行；后续到达者复用
        同一把锁继续排队，不会出现「新锁 + 旧锁」并发巡检同一 media。
      - 无等待者：唯一执行者已结束，移除后新调用方会新建锁，同样无并发。
    判定委托 _scan_lock_idle（见其 docstring）：try 读取 asyncio.Lock._waiters 私有
    属性（CPython 3.14 实现，懒初始化为 None / deque of futures）；读取抛异常（实现
    变化）→ 保守返回 False 保留锁，宁可轻微泄漏也不破坏互斥。
    不用 lock.locked() 判断——它仅反映 _locked，与是否有人在等待无关，无法区分清理时机。
    """
    lock = _scan_locks.setdefault(media_id, asyncio.Lock())
    async with lock:
        try:
            if run_id is None:
                # 定时路径保持旧调用签名（既有测试/桩以 (media_id, *, manual) 替换）
                return await _scan_one(media_id, manual=manual)
            return await _scan_one(media_id, manual=manual, run_id=run_id)
        finally:
            # 清理：无等待者时才移除（新调用方会重新创建，等待中的调用方仍持有旧锁引用）
            if _scan_lock_idle(lock):
                _scan_locks.pop(media_id, None)


async def scan_all_media_job() -> None:
    """定时 tick 包装（B 定时：每分钟；调度粒度由本 job 触发周期控制）。

    restore-scan-interval-scheduling：job 保持每分钟 tick；到期过滤在
    scan_all_media 内部完成——每轮仅巡检已到期的 tracking/downloading 影视
    （per-media scan_interval_minutes，兜底 60 分钟），未到期影视轻量 SQL 跳过。

    M1（Oracle Gate2）：与同模块其它 job 一致（transfer.process_transfer_queue_job 等），
    DB 读取/执行异常时记录 task_run(error) 兜底而不是静默抛出——APScheduler 会吞
    任务内异常，若不记录则作业持续失败运维不可见。task_type 用 "scan_all_media"
    （作业级，对齐 transfer/cleanup/notify 的作业级短名惯例；区别于单部巡检的
    "scan_media"）。
    """
    t0 = time.monotonic()  # Q8①：真实耗时
    try:
        await scan_all_media()
    except Exception:  # noqa: BLE001
        logger.exception("[scan] scan_all_media 定时巡检异常")
        try:
            async with async_session() as s:
                await record_task_run(  # Q8①：真实耗时
                    s, "scan_all_media", "error", "定时巡检异常，见服务日志",
                    duration_seconds=time.monotonic() - t0,
                )
                await s.commit()
        except Exception:  # noqa: BLE001  兜底记录失败只告警，不再外泄
            logger.exception("[scan] scan_all_media 异常记录失败")


def _due_filter():
    """per-media 到期过滤条件（跨方言）：last_scan_at IS NULL OR last_scan_at <= now - 间隔。

    间隔 = COALESCE(media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)；
    时间基准与 _finish_scan_run 写入 last_scan_at 同源（_now()，naive UTC）。
    SQLite（测试）用 func.datetime 修饰符；PostgreSQL（生产）用 now 绑定参数 - interval。
    """
    from sqlalchemy import String, bindparam, func, literal, or_

    minutes = func.coalesce(Media.scan_interval_minutes, settings.SCAN_INTERVAL_MINUTES)
    if engine.dialect.name == "postgresql":
        # PG：timestamp 列 - interval 无隐式时区 cast，now 绑定参数与写入端同源（naive UTC）
        # make_interval 用位置参数（SQLAlchemy 2.0 的 func 不支持 kwargs）——签名
        # make_interval(years, months, weeks, days, hours, mins)，第 6 位即分钟数。
        # 旧写法 minutes * text("interval '1 minute'") 编译为 integer * interval，
        # PG 仅支持 interval * numeric（审查 A16，实库报错），此处修正为原生构造。
        due_expr = Media.last_scan_at <= (
            bindparam("now", _now())
            - func.make_interval(0, 0, 0, 0, 0, minutes)
        )
    else:
        # SQLite：datetime(now_iso, '-' || minutes || ' minutes') 修饰符
        now_iso = _now().strftime("%Y-%m-%d %H:%M:%S")
        modifier = literal("-") + minutes.cast(String) + literal(" minutes")
        due_expr = Media.last_scan_at <= func.datetime(now_iso, modifier)
    return or_(Media.last_scan_at.is_(None), due_expr)


async def scan_all_media(force: bool = False) -> None:
    """遍历到期 tracking/downloading 影视巡检（downloading 不跳过，防卡死，§3.1）。

    恢复 per-media 到期过滤（restore-scan-interval-scheduling）：每轮 tick 仅巡检
    「距上次成功巡检已超过其配置间隔（scan_interval_minutes，兜底全局默认 60 分钟）」
    或「从未巡检过（last_scan_at 为 NULL）」的 tracking/downloading 影视；未到期
    影视以轻量 SQL 过滤跳过（不执行 Emby 基线、搜索与 task_run 落库）。调度 job
    保持每分钟 tick（scan_all_media_job 的 APScheduler IntervalTrigger），过滤在本
    函数内完成。

    force（手动全量/CLI 入口契约）：force=True 绕过 per-media 到期过滤，立即巡检
    全部 tracking/downloading 影视。本 change 不新增 force 调用入口（仅恢复函数语义）。
    """
    async with async_session() as s:
        stmt = select(Media).where(Media.status.in_(("tracking", "downloading")))
        if not force:
            stmt = stmt.where(_due_filter())
        rows = (await s.execute(stmt)).scalars().all()
    for media in rows:
        try:
            await scan_media(media.id)
        except Exception:
            logger.exception("[scan] media=%s 巡检异常", media.id)


async def _create_scan_run(media_id: int, *, trigger_source: str = "auto") -> int | None:
    """插入一条 status='running' 的 task_run 并 commit，返回 id。

    触发巡检即落 running 中间态——HTTP 触发到巡检结束（数十秒）期间前端队列即可
    看到「正在巡检」记录。running 记录带 scan_detail 骨架（含 trigger_source），
    跑完由 `_finish_scan_run` 原地 UPDATE 为终态。

    media 不存在时返回 None（不建立无主记录）。
    """
    async with async_session() as s:
        if await s.get(Media, media_id) is None:
            logger.warning("[scan] media=%s 不存在，不建立巡检记录", media_id)
            return None
        detail = _result_detail_skeleton(trigger_source=trigger_source)
        run = TaskRun(
            task_type="scan_media",
            media_id=media_id,
            status="running",
            message="正在巡检",
            started_at=_now(),
            finished_at=None,
            duration_seconds=None,
            scan_detail=_json_dumps(detail),
        )
        s.add(run)
        await s.commit()
        return run.id


async def _finish_scan_run(
    run_id: int,
    media_id: int,
    status: str,
    message: str,
    *,
    phases: dict | None,
    scan_detail: dict | None,
    touch_last_scan_at: bool = False,
    duration_seconds: float,
) -> int:
    """巡检可见性改造（第二段）：按 id UPDATE 同一条 task_run 为终态并 commit。

    - 状态/消息/耗时：running → status（success/skipped/error）+ 人话 message + 真实耗时
    - phases / scan_detail：5 阶段进度 JSON 与结果摘要 JSON 序列化落库（巡检为快任务，
      内存打点结束一次性写入，运行中不实时写 DB）
    - 可选同步更新 media.last_scan_at（touch_last_scan_at=True）

    短事务：不借用外层长事务，写完即释放（沿用旧实现的 P1-1 约定）。
    """
    async with async_session() as s:
        media_title = None
        if status == "error":
            media_row = await s.get(Media, media_id)
            media_title = media_row.title if media_row is not None else None
        if touch_last_scan_at:
            await s.execute(
                update(Media).where(Media.id == media_id).values(last_scan_at=_now())
            )
        await s.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id)
            .values(
                status=status,
                message=message,
                finished_at=_now(),
                duration_seconds=duration_seconds,
                phases=_json_dumps(phases),
                scan_detail=_json_dumps(scan_detail),
            )
        )
        await s.commit()
    # 告警在落库之后发送：通知失败只记日志，不影响巡检结果（P2 2.1）
    if status == "error":
        await _alert_scan_error(
            media_id, media_title,
            (scan_detail or {}).get("failed_phase"),
            message,
        )
    return run_id


async def _scan_one(media_id: int, *, manual: bool = False,
                    run_id: int | None = None) -> int | None:
    """严格按设计文档 §4.3 巡检主流程，阶段2 只到入队为止（不转存）。

    巡检可见性改造：入口先建 status='running' 的 task_run（_create_scan_run），
    全部返回分支改走 _finish_scan_run 原地 UPDATE 同一条为终态，附 5 阶段进度
    （phases JSON）与结果摘要（scan_detail JSON）。巡检是快任务——阶段打点全部
    在内存进行（_phase_start/_phase_done），结束时一次性落库，运行中不实时写 DB。

    run_id（P2 design D5）：手动触发由 `trigger_scan_background` 受理时建好记录，
    此处复用；None 表示定时路径，自建 trigger_source=auto 的记录。不得重复建记录。

    P1-1（延后项）：长事务拆分——Emby 基线 / 搜索 / share-info / share-list 递归
    等网络 IO 全程**不持有 DB session**（SQLite 单连接被长事务占住会阻塞其他写）；
    仅 DB 读写使用短事务（读 media、task_run 写入、media.last_scan_at、
    _read_size_limits、_enqueue 均各自开启/关闭 session）。database.async_session
    expire_on_commit=False，开头短会话读出的 media 为 detached 对象，已加载属性
    （id/status/title/tmdb_id/media_type 等）可安全继续使用。

    manual（queue-flow-rework Task 3 起为兼容保留）：静默 unmatched 预过滤已移除，
    手动/定时调度行为一致——缺失集一律照常搜索，搜不到下轮全局巡检自然重试。

    scan-outcome-attribution 3.2：异常逃逸时把当前 task_run 收敛为 error（保留原因）
    后**继续上抛**，由调用方记录；进程内异常不再留下永久 running 记录。
    """
    t0 = time.monotonic()  # Q8①：真实耗时（单部巡检）
    trigger_source = "manual" if manual else "auto"

    # 0. 短事务读取 media（立即关闭；detached 属性后续安全）
    async with async_session() as s:
        media = await s.get(Media, media_id)
    if media is None:
        # 不存在 → 不建 running 记录，直接 return None（保持现状契约）
        logger.warning("[scan] media=%s 不存在", media_id)
        return None

    # 0b. 巡检可见性：触发即落 running 中间态（前端可立即看到「正在巡检」）。
    #     手动路径已由 trigger_scan_background 建好记录，此处复用（不产生第二条）。
    if run_id is None:
        run_id = await _create_scan_run(media_id, trigger_source=trigger_source)
    if run_id is None:
        # running 记录建立失败（极小概率）→ 保持旧行为，不阻断巡检主流程
        return None

    # state 供异常兜底读取「当前」phases / scan_detail（_scan_one_impl 内同步写回）
    state: dict = {"phases": _new_phases(), "scan_detail": None}
    try:
        return await _scan_one_impl(media_id, media, run_id, state, t0, trigger_source)
    except Exception as exc:  # noqa: BLE001  收敛后继续上抛，不吞异常
        phases = state["phases"]
        failed_phase = _fail_phase(phases)
        if failed_phase:
            _phase_error(phases, failed_phase)
        detail = state["scan_detail"] or _result_detail_skeleton(
            failed_phase, trigger_source=trigger_source,
        )
        detail["failed_phase"] = failed_phase
        try:
            await _finish_scan_run(
                run_id, media_id, "error", f"巡检异常中止: {exc}",
                phases=phases,
                scan_detail=detail,
                duration_seconds=time.monotonic() - t0,
            )
        except Exception:  # noqa: BLE001  收尾失败不得掩盖原始异常
            logger.exception("[scan] media=%s 异常收尾失败（run_id=%s）", media_id, run_id)
        raise


async def _scan_one_impl(media_id: int, media, run_id: int, state: dict,
                         t0: float, trigger_source: str = "auto") -> int | None:
    """_scan_one 主体（run 已建立；phases / scan_detail 同步写回 state 供异常兜底）。"""
    phases = state["phases"]
    scan_detail = state["scan_detail"]

    async def _finish(status: str, message: str, *,
                      touch_last_scan_at: bool = False) -> int:
        """原地收尾同一条 task_run（当前 phases / scan_detail 快照随调用一并落库）。"""
        return await _finish_scan_run(
            run_id, media_id, status, message,
            phases=phases,
            scan_detail=scan_detail,
            touch_last_scan_at=touch_last_scan_at,
            duration_seconds=time.monotonic() - t0,
        )

    # 1. 状态预检：paused/error 跳过；downloading 不排除出巡检集合（防卡死），
    #    但按其各自间隔到期判断（≠ 绕过冷却），仅本轮不入队（skip_enqueue）
    if media.status in ("paused", "error"):
        _phase_skip_remaining(phases, "check")  # 尚未开始的阶段标记 skipped（未达完成态）
        skip_reason = "status_paused" if media.status == "paused" else "status_error"
        scan_detail = state["scan_detail"] = _result_detail_skeleton(trigger_source=trigger_source)
        scan_detail["skip_reason"] = skip_reason
        return await _finish(
            "skipped", _skip_message(skip_reason, title=media.title),
        )

    # 2. Emby 防重基线（网络 IO，无 DB session）——phase: check
    _phase_start(phases, "check")
    try:
        missing = await _emby_missing_codes(media)
    except Exception as exc:
        # fail-safe（§4.3）：Emby 故障暂停新缺集发现，防止故障期重复转存/误占空间；
        # 既有 queued/failed 任务保留原样（阶段2 无转存逻辑，无需额外处理）
        _phase_error(phases, "check")  # 阶段定位 error
        # 基线失败发生在 scan_detail 装配前 → 落最小骨架，failed_phase 供前端定位
        scan_detail = state["scan_detail"] = _result_detail_skeleton(
            "check", trigger_source=trigger_source,
        )
        return await _finish(
            "error",
            f"Emby 故障，fail-safe 暂停新缺集发现: {exc}",
            touch_last_scan_at=False,
        )
    _phase_done(phases, "check")

    if missing is None:
        # 基线不可用：Emby 未收录该剧集，无法探明遗漏。
        # _emby_missing_codes 已按开关分流——仅当 scan_baseline_required=True（强防重，
        # 旧行为）才返回 None；False（默认）返回全量模式 [None]，走下方搜索入队路径。
        # 此处保留旧行为（本轮跳过），并区分文案以与全量模式日志区分。
        _phase_skip_remaining(phases, "search")  # search/match/enqueue/finish 未执行 → skipped
        scan_detail = state["scan_detail"] = _result_detail_skeleton(trigger_source=trigger_source)
        scan_detail["skip_reason"] = "baseline_unavailable"
        return await _finish(
            "skipped",
            _skip_message("baseline_unavailable", title=media.title),
            touch_last_scan_at=False,
        )

    # 2a. 已播出门控（Task 5）：按 TMDB air_date（缺失回退 Emby premiere_date）剔除
    #     「未来才播」的集，避免为未播出资源搜索/入队占中转空间。None 哨兵项
    #     （全量/电影）由 _filter_aired 原样保留、不套用门控。
    missing_before = [m["code"] for m in missing if m and m.get("code")]
    missing, aired_failed = await _filter_aired(media, missing)
    kept_codes = {m["code"] for m in missing if m and m.get("code")}
    # 归因：被已播门控剔除的集号（TMDB 取数失败季的集不算门控剔除，单独由 fail-safe 处理）
    gated_out_episodes = [
        c for c in missing_before
        if c not in kept_codes and _season_of_key(c) not in aired_failed
    ]
    missing_aired_total = len(kept_codes)  # 已播缺失集数（全量模式下方按实际条目兜底）
    if aired_failed and not missing:
        # fail-safe（Ruling 2 简化）：TMDB 某季取数失败且门控后无可保留项 → 本轮不
        # 搜索（否则会把「取数失败」误当「无遗漏」而错失补齐机会），message 标明季号。
        _phase_skip_remaining(phases, "search")
        scan_detail = state["scan_detail"] = _result_detail_skeleton(trigger_source=trigger_source)
        scan_detail["skip_reason"] = "airdate_unavailable"
        return await _finish(
            "skipped",
            _skip_message("airdate_unavailable", detail=f"季 {sorted(aired_failed)}"),
            touch_last_scan_at=False,
        )

    missing_keys = {m["code"] for m in missing if m and m.get("code")}
    # movie_missing 真 = 全量模式（Emby 基线缺失 soft 全量）：
    #   - 真电影（media_type=="movie"）整部缺失 → [None]；
    #   - tv 未收录（Emby 未收录 + scan_baseline_required=False 软处理）→ [None]，
    #     即「tv 未收录全量」= movie_missing 且 media_type!="movie"——少帅式搜索误匹配
    #     修复（A2/A3/A4）只对该分支生效：每文件校验（_full_mode_accept）+ 单轮限批，
    #     防同名短剧/无关资源一次入队爆炸；真电影全量语义不同（单文件常带片名），保留
    #     原行为不套用校验（见下方 match 阶段注释）。
    movie_missing = any(m is None for m in missing)

    # 2b. done 防重解除（P1-1，Oracle 审查）：Emby 二次确认入库 → 删除 es/tq/dl 解除防重；
    #     仍未入库 → 宽限递进 / 自动重下 / 终态（见 _resolve_done_states）。异常仅告警，不阻断巡检。
    #     _resolve_done_states 自带短 session，此处不涉及任何长事务。
    redownloaded = 0
    try:
        redownloaded = await _resolve_done_states(media, missing_keys, movie_missing)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scan] media=%s done 防重解除失败（不阻断巡检）: %s", media_id, exc)

    # 2b-1. 自动重下重置为 pending 后立即触发转存消费：与其他触发点同款事件触发，
    #       避免「本轮无新入队」时只能依赖每分钟兜底 job 或下一轮巡检。
    if redownloaded:
        await _trigger_transfer()

    # scan_detail 装配：拿到 aired-only 后的 Emby 基线即可填充 missing_items
    # （每个缺失集 episode → result 初始 not_found；入队成功后改 enqueued）。
    # queue-flow-rework Task 3：静默 unmatched 预过滤已移除——缺失集一律照常搜索，
    # 本轮搜不到下轮全局巡检自然重试（不再有 silent 剔除，scan_missing 即全体 missing）。
    # 全量模式（movie_missing / tv 未收录软处理）：episode 用文件名；搜索结果在
    # 主循环里实时补入（缺失集实体不预知，missing_total 以基线长度表达 + 动态追加）。
    scan_missing = list(missing)
    missing_total = len(scan_missing)
    missing_items: list[dict] = []
    missing_items_by_key: dict[str, dict] = {}
    if not movie_missing:
        for m in scan_missing:
            if m is None:
                continue
            item = {"episode": m["code"], "result": "not_found"}
            missing_items.append(item)
            missing_items_by_key[m["code"]] = item
    scan_detail = state["scan_detail"] = _result_detail_skeleton(trigger_source=trigger_source)
    scan_detail["missing_total"] = missing_total
    scan_detail["missing_items"] = missing_items
    scan_detail["missing_aired_total"] = missing_aired_total
    scan_detail["gated_out"] = {
        "count": len(gated_out_episodes),
        "episodes": gated_out_episodes,
    }

    if not missing_keys and not movie_missing:
        # 无遗漏 → 短路结束（消灭 P3 空跑）。区分「本来就没有缺失集」与
        # 「有缺失集但全部被已播门控剔除」（后者不得报成「无遗漏集」）。
        _phase_skip_remaining(phases, "search")  # 后续阶段未执行 → skipped
        skip_reason = "gated_out" if gated_out_episodes else "no_missing"
        scan_detail["skip_reason"] = skip_reason
        return await _finish(
            "skipped",
            _skip_message(skip_reason, title=media.title,
                          gated_out_episodes=gated_out_episodes),
            touch_last_scan_at=True,
        )

    # 2c. 全量兜底路径门控（Task 6）：tv 未收录全量（movie_missing 且 media_type!="movie"）
    #     的缺失集实体不预知（[None] 哨兵），无法像常规路径（_filter_aired）那样按集过滤
    #     未来播出；改为整剧维度预判——先取 TMDB 全集播出日期，若缺 tmdb_id / 取数失败 /
    #     全未播出则本轮直接跳过（搜索前短路，不搜索、不入队），避免为未开播剧浪费搜索与
    #     中转空间。真电影全量（media_type=="movie"）语义不同，不套用本门控。
    #     已播出剧不短路：文件循环内再按 SxxExx 逐文件拒绝未播出集（aired_by_key）。
    aired_by_key: dict[tuple[int, int], bool] = {}
    full_mode_aired_total = 0  # 全量模式已播缺失数（缺失集实体不预知，供归因兜底）
    if movie_missing and (media.media_type or "").strip().lower() != "movie":
        if media.tmdb_id is None:
            _phase_skip_remaining(phases, "search")  # search/match/enqueue/finish 未执行 → skipped
            scan_detail["skip_reason"] = "airdate_unavailable"
            return await _finish(
                "skipped",
                _skip_message("airdate_unavailable",
                              detail="缺 tmdb_id，无法判定播出日期"),
                touch_last_scan_at=False,
            )
        all_eps = await tmdb.get_tv_all_episodes(media.tmdb_id)
        if all_eps is None:
            _phase_skip_remaining(phases, "search")
            scan_detail["skip_reason"] = "airdate_unavailable"
            return await _finish(
                "skipped",
                _skip_message("airdate_unavailable", detail="全集取数失败"),
                touch_last_scan_at=False,
            )
        today = date.today()
        aired_by_key = {
            (e["season"], e["episode"]): episode_aired(e.get("air_date"), today)
            for e in all_eps
        }
        # 搜索零候选时 missing_items 为空，用门控确认的已播集数兜底归因
        full_mode_aired_total = sum(1 for aired in aired_by_key.values() if aired)
        if aired_by_key and not any(aired_by_key.values()):
            _phase_skip_remaining(phases, "search")
            scan_detail["skip_reason"] = "not_aired"
            return await _finish(
                "skipped",
                _skip_message("not_aired", title=media.title),
                touch_last_scan_at=False,
            )

    # 3. cloudSaver 搜索 → 展开分享码 → 加分匹配 → 限数 20（网络 IO，无 DB session）
    #                                                        —— phase: search
    _phase_start(phases, "search")
    try:
        candidates = await _search_and_rank(media, missing_keys)
    except Exception as exc:  # noqa: BLE001
        # 搜索服务整体故障（全部关键词失败，_search_and_rank 抛 ScanSearchUnavailable）
        # 或内部意外异常 → 定位 search 阶段为 error（区别于「搜索成功但无候选」的 skipped）
        _phase_error(phases, "search")
        scan_detail["failed_phase"] = "search"
        scan_detail["search_status"] = "failed"
        return await _finish(
            "error",
            f"搜索服务异常（cloudSaver 不可达或超时），未能查找缺失集资源，请稍后重试: {exc}",
            touch_last_scan_at=False,
        )
    _phase_done(phases, "search")
    scan_detail["search_status"] = "ok" if candidates else "no_candidates"

    # 4-6. share-info(500ms 串行) → share-list 递归遍历文件 → 大小过滤 → 三重匹配 → 入队
    #      （大小上下限经独立短 session _read_size_limits 读取，网络 IO 阶段不持 DB session）
    #                                                 —— phase: match + enqueue
    try:
        ep_min, ep_max, movie_min, movie_max = await _read_size_limits(media)
    except Exception as exc:  # noqa: BLE001  大小上下限读取失败，fail-closed 中止
        _phase_error(phases, "match")
        scan_detail["failed_phase"] = "match"
        return await _finish(
            "error", f"巡检中止: 大小过滤上下限读取失败: {exc}",
            touch_last_scan_at=False,
        )
    min_gb, max_gb = _size_bounds_gb(media, ep_min, ep_max, movie_min, movie_max)
    skip_enqueue = media.status == "downloading"  # 有进行中任务本轮不入队，但仍检查遗漏

    # A2/A3/A4：tv 未收录全量模式（Emby 未收录 + scan_baseline_required=False，
    # movie_missing 且 media_type!="movie"）→ 文件级校验 + 单轮限批，防同名短剧/无关
    # 资源一次入队爆炸（「少帅」搜索入队 284 个错误资源案例修复）。真电影全量模式
    # 语义不同（整部缺失的单文件常带片名），保留原行为不套用校验。
    tv_full_mode = movie_missing and (media.media_type or "").strip().lower() != "movie"
    total_episodes = await _media_total_episodes(media) if tv_full_mode else None

    enqueued = existing_skipped = size_filtered = unmatched = non_video = 0
    downloading_skipped_keys: set[str] = set()  # 因 downloading 本轮未入队的集（按集去重）
    unrelated_filtered = 0  # A2：全量模式文件级校验拒绝计数（无关/超集号文件）
    enqueue_limited = False  # A4：全量模式限批触发标记（停止遍历后续分享/文件）
    unmatched_files: list[str] = []  # 未匹配文件名样例（至多收集 3 个，供 message 定位）
    share_info_ok = share_info_fail = walk_fail = tried = 0
    # 两阶段匹配（2026-09）：规范命名（SxxExx/第N集）命中登记 + 纯数字候选暂存——
    # 规则3 纯数字降级为「未被规范命名的缺失集」的最后兜底（优先规范命名资源）
    std_matched_keys: set[str] = set()       # 被规则1/2（规范命名）命中的缺失集
    numeric_candidates: dict[str, list[dict]] = {}  # 纯数字候选：episode_key → 候选文件
    _phase_start(phases, "match")
    _phase_start(phases, "enqueue")  # 匹配+入队同循环内推进；先统一标 process
    for cand in candidates:
        # A4 全量限批：已达单轮入队上限即停止遍历后续候选（防一次入队爆炸）
        if tv_full_mode and enqueued >= _FULL_MODE_ENQUEUE_LIMIT:
            break
        # 验证尝试上限：对齐 n8n「遍历分享码直到找到」——失效码逐个跳过、继续后续候选，
        # 但单轮最多验证 _MAX_SHARE_TRY 个（80×~0.5s≈40s 上限，防 230 候选过慢）
        if tried >= _MAX_SHARE_TRY:
            break
        tried += 1
        share_code = cand["share_code"]
        try:
            info = await _cloudsaver_share_info(share_code)
            share_info_ok += 1
        except Exception as exc:
            share_info_fail += 1
            logger.warning("[scan] share-info %s 失败（跳过，继续后续候选）: %s", share_code, exc)
            continue
        finally:
            await asyncio.sleep(0.5)  # 逐码 500ms 间隔串行（§4.3 步骤3）

        try:
            files = await _walk_share(share_code, info)
        except Exception as exc:
            walk_fail += 1
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
                # 全量模式（Emby 基线缺失 soft 全量）：episode 键取——
                #   真电影（media_type==movie）→ movie:<title> 归一化键（§7 审阅 P1）：
                #     多版本（不同分辨率/来源文件名）映射同一键，防重复下载+同名覆盖；
                #   tv 未收录全量（media_type!=movie，Emby 未收录整部追的软全量）→
                #     文件名含标准 SxxExx → 归一化集号键（2026-09 重复下载事故修复：
                #       同集多命名版本共享一键，防重复入队；此前 P9 用文件名键导致
                #       friDay.mkv / 全称.mp4 / 中文名.mp4 同集各入队一次）；
                #     无标准集号（第N集/纯数字/无集号）→ 回退文件名键（P9 原权衡
                #       防丢集：集号未知，逐文件入队，不归一化）。
                # 展示 episode 仍用实际文件名供定位。
                if tv_full_mode:
                    # A2/A3 全量模式文件级校验（仅 tv 未收录全量生效；真电影全量
                    # 保留原行为）：无关/超集号文件拒绝入队（「少帅将我宠上天(99集)」
                    # 99>48、「9.mp4」纯孤立数字等）——杜绝同名短剧/无关资源入队爆炸。
                    if not _full_mode_accept(media, file_name, total_episodes):
                        unrelated_filtered += 1
                        shown = file_name if len(file_name) <= 80 else file_name[:80] + "…"
                        logger.info(
                            "[scan] media=%s 全量模式拒绝无关/超集号文件: %s",
                            media_id, shown,
                        )
                        continue
                    # Task 6 全量兜底播出日期门控：SxxExx 可解析且命中 TMDB 全集
                    # 未播出集 → 拒绝（未开播资源不搜索/不入队）；解析不到 (s,e)
                    # 时按既有策略放行，不误杀。
                    m_sxx = _RE_SXXEXX.search(file_name)
                    if m_sxx:
                        key = (int(m_sxx.group(1)), int(m_sxx.group(2)))
                        if key in aired_by_key and not aired_by_key[key]:
                            unrelated_filtered += 1
                            continue  # 未播出集文件，拒绝
                    # A4 全量限批：单轮入队达上限即停止遍历（防一次入队爆炸）
                    if enqueued >= _FULL_MODE_ENQUEUE_LIMIT:
                        enqueue_limited = True
                        break
                if (media.media_type or "").strip().lower() == "movie":
                    matched_key = "movie:" + (media.title or "").strip()
                    if matched_key == "movie:":
                        matched_key = file_name  # 标题缺失回退文件名（兜底，防键空）
                else:
                    m_sxx = _RE_SXXEXX.search(file_name)
                    if m_sxx:
                        matched_key = _fmt_episode(int(m_sxx.group(1)), int(m_sxx.group(2)))
                    else:
                        matched_key = file_name
                item = {"episode": file_name, "result": "not_found"}
                missing_items.append(item)
                missing_items_by_key[matched_key] = item
            else:
                # 两阶段匹配（2026-09 优化）：
                #   阶段 A（本循环）：规则1/2 规范命名（SxxExx/第N集）命中 → 立即入队，
                #                     并登记 std_matched_keys（该集已有规范命名资源）；
                #                     规则3 纯数字命中 → 仅暂存候选（不立即入队）
                #   阶段 B（循环后）：仅对「未被规范命名命中的缺失集」用纯数字候选兜底，
                #                     优先规范命名资源，纯数字是最后手段
                matched_key = match_missing_std(file_name, missing_keys)
                if matched_key is not None:
                    std_matched_keys.add(matched_key)
                else:
                    num_key = _pure_numeric_match(file_name, missing_keys)
                    if num_key is not None:
                        numeric_candidates.setdefault(num_key, []).append({
                            "info": info, "f": f, "share_code": share_code,
                            "file_name": file_name, "file_size": file_size,
                        })
                        continue
                    unmatched += 1
                    if len(unmatched_files) < 3:
                        unmatched_files.append(file_name)
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
            out_of_bounds = _size_out_of_bounds(file_size, min_gb, max_gb)
            if out_of_bounds:
                size_filtered += 1
                if out_of_bounds == "low":
                    logger.info(
                        "[scan] media=%s %s 低于下限 %.2fG < %.1fG，跳过",
                        media_id, file_name, file_size / 1024 ** 3, min_gb,
                    )
                else:
                    logger.info(
                        "[scan] media=%s %s 高于上限 %.2fG > %.1fG，跳过",
                        media_id, file_name, file_size / 1024 ** 3, max_gb,
                    )
                continue

            if skip_enqueue:
                # 因该影视正在下载而未入队：单独计数（不得表述为「已有任务跳过」）
                downloading_skipped_keys.add(matched_key)
                continue

            payload = _enqueue_payload(info, f)
            res = await _enqueue(media_id, matched_key, file_name, file_size, share_code, payload,
                                 size_estimated=bool(f.get("size_estimated")))
            if res == "enqueued":
                enqueued += 1
                item = missing_items_by_key.get(matched_key)
                if item is not None:
                    item["result"] = "enqueued"
            elif res == "existing":
                existing_skipped += 1  # 防重命中：该集已有任务（视为有资源，不标 unmatched）
            else:
                existing_skipped += 1  # conflict（行级并发冲突）视为跳过；不标 matched，
                #                      防误把并发中任务标 unmatched（下轮会重新入队）

        # A4 全量限批：已达单轮入队上限 → 停止遍历后续分享候选（防一次入队爆炸）
        if enqueue_limited:
            break

    # ---- 阶段 B：纯数字兜底入队（规则 3 仅作最后手段）----
    # 对「未被规范命名（SxxExx/第N集）命中」的缺失集，用暂存的纯数字候选按序入队：
    #   优先规范命名资源——只要某集在任意分享被规范命名命中（std_matched_keys），
    #   即使该规范文件因大小过滤被拒，也不再降级纯数字（防误配，下轮自然重试）。
    #   同一集多个纯数字候选：取第一个通过大小过滤且入队成功的。
    # movie_missing 分支不收集 numeric_candidates（走文件名键），此处自然为空。
    if numeric_candidates:
        fallback_keys = sorted(missing_keys - std_matched_keys)
        for ep_key in fallback_keys:
            for cand in numeric_candidates.get(ep_key) or ():
                # 大小过滤（与主循环同语义：fail-closed）
                if cand["f"].get("size_unknown"):
                    size_filtered += 1
                    continue
                out_of_bounds = _size_out_of_bounds(cand["file_size"], min_gb, max_gb)
                if out_of_bounds:
                    size_filtered += 1
                    continue
                if skip_enqueue:
                    downloading_skipped_keys.add(ep_key)  # downloading 本轮不入队（同主循环语义）
                    break
                payload = _enqueue_payload(cand["info"], cand["f"])
                res = await _enqueue(
                    media_id, ep_key, cand["file_name"], cand["file_size"],
                    cand["share_code"], payload,
                    size_estimated=bool(cand["f"].get("size_estimated")),
                )
                if res == "enqueued":
                    enqueued += 1
                    item = missing_items_by_key.get(ep_key)
                    if item is not None:
                        item["result"] = "enqueued"
                    break  # 该集已入队，不再尝试其它候选
                elif res == "existing":
                    existing_skipped += 1  # 已有任务（视为有资源），无需继续尝试
                    break
                else:
                    existing_skipped += 1  # conflict → 尝试下一个候选（下轮重试兜底）

    _phase_done(phases, "match")
    _phase_done(phases, "enqueue")

    # queue-flow-rework Task 3：unmatched 静默机制已移除——缺失集搜索失败不再写
    # status='unmatched' + silent_until，本轮跳过、下轮全局巡检自然重试。
    # unmatched_marked 保留恒 0（JSON 结构稳定，不再有「本轮新标静默」语义）。
    unmatched_marked = 0

    # 7. 原地 UPDATE 同一条 task_run 终态 + 更新 last_scan_at（独立短事务）——phase: finish
    #     message 缺集语义修复：parts 全空（无候选/无入队/无计数）且缺集 >0 时，
    #     明示缺失集并解释「搜索无匹配候选」（此前误报「无候选命中」）；候选分享
    #     share-info 验证全部失败（诊断实证：cloudSaver 搜索返回大量失效分享码）
    #     时优先报「候选分享验证均失败」。episodes 取 scan_detail.missing_items 的
    #     episode（如 S01E190；全量模式含文件名）。N 用实际尝试过的候选数
    #     （share_info_fail：candidates 可能有 _MAX_SHARE_TRY 上限截断，未验证的不计入）。
    # 归因结论（D1）：本轮无产出时区分「因下载中未入队」与「未获取到资源」。
    # 已有任务（existing）视为有资源，不算 no_candidates（否则告警会误报）；
    # unmatched / size_filtered / non_video > 0 表示已找到过资源（只是未匹配/被过滤），
    # 同样不得置 no_candidates，否则 P2 会误报「资源尚未更新」。
    if movie_missing:
        # 全量模式缺失集实体不预知：优先按实际收集条目数；搜索零候选时回退已播缺失数
        # （真电影整缺 = 1 个缺失实体；tv 未收录软全量 = 2c 门控确认的已播集数），
        # 避免「有已播缺集但零候选」被归因为 None（P2 告警会系统性漏报）。
        missing_aired_total = len(missing_items) or (
            full_mode_aired_total if tv_full_mode else 1
        )
    if downloading_skipped_keys:
        skip_reason = "skip_enqueue"
    elif (enqueued == 0 and missing_aired_total > 0 and existing_skipped == 0
          and unmatched == 0 and size_filtered == 0 and non_video == 0
          and unrelated_filtered == 0):
        skip_reason = "no_candidates"
    else:
        skip_reason = None

    share_info_all_failed = share_info_fail if (candidates and share_info_ok == 0) else 0
    message = _result_message(
        enqueued=enqueued, unmatched=unmatched,
        size_filtered=size_filtered, non_video=non_video,
        existing_skipped=existing_skipped,
        unrelated_filtered=unrelated_filtered,
        full_mode_limit=_FULL_MODE_ENQUEUE_LIMIT if tv_full_mode else 0,
        missing_episodes=[item["episode"] for item in scan_detail["missing_items"]]
        if scan_detail["missing_items"] else None,
        share_info_all_failed_candidates=share_info_all_failed,
    )
    if skip_reason:
        skip_clause = _skip_message(
            skip_reason, title=media.title,
            missing_aired_total=missing_aired_total,
            missing_episodes=[item["episode"] for item in scan_detail["missing_items"]]
            if scan_detail["missing_items"] else None,
            not_enqueued=len(downloading_skipped_keys),
        )
        # 因下载中未入队时计数消息会误报「未找到缺失集资源」，只保留归因文案
        message = skip_clause if skip_reason == "skip_enqueue" else f"{message}；{skip_clause}"
    scan_detail.update({
        # 全量模式：基线表达为 missing（[None]），实际缺失实体是搜索到的具体文件，
        # 此处以实际收集到的缺失集条目数兜底（缺失集实体不预知，契约允许宽松填充）。
        "missing_total": len(missing_items) if movie_missing else missing_total,
        "missing_aired_total": missing_aired_total,
        "gated_out": {
            "count": len(gated_out_episodes),
            "episodes": gated_out_episodes,
        },
        "skip_reason": skip_reason,
        "trigger_source": trigger_source,
        "enqueued": enqueued,
        "existing_skipped": existing_skipped,
        "size_filtered": size_filtered,
        "unmatched": unmatched,
        "unmatched_marked": unmatched_marked,  # 恒 0（Task 3 静默机制移除后不再新标）
        "non_video": non_video,
        "unrelated_filtered": unrelated_filtered,  # A2：全量模式无关/超集号文件过滤数
        "share_info_ok": share_info_ok,
        "share_info_fail": share_info_fail,
        "walk_fail": walk_fail,
        "unmatched_silent_skipped": 0,  # 恒 0（Task 3 静默机制移除，键保留兼容）
    })
    _phase_start(phases, "finish")
    _phase_done(phases, "finish")
    rid = await _finish(
        "success" if enqueued else "skipped", message,
        touch_last_scan_at=True,
    )
    # P2 2.2：连续 N 轮无资源告警（落库后判定；失败只记日志，不影响巡检结果）
    if skip_reason == "no_candidates":
        await _maybe_alert_no_candidates(media_id, media.title, scan_detail)
    # 6b. 入队成功后触发转存消费（§4.4 事件触发；transfer lane 未就绪时静默跳过）
    #     fire-and-forget 在 DB session 外触发（_background 强引用集合防 GC）
    if enqueued:
        await _trigger_transfer()
    return rid

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
import time
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.models import DownloadQueue, Media, TaskQueue, TaskRun
# 注：episode_state / transfer_queue / download_task 旧三表已从 scan 移除——影视下载
# 两队列重设计：探测结果落 task_queue，经 promote 产出 download_queue（执行层），
# 防重权威源 = download_queue.UNIQUE(media_id, episode)。见 docs/影视下载两队列重设计.md §3。
from app.services import cloudsaver, config_store, emby, tmdb
from app.tasks import as_bool, get_config_value, record_task_run
# tasks 层公共纯函数（app.utils，仅标准库）：统一时间源与集级匹配函数
from app.utils import fmt_episode, now_utc_naive as _now, parse_episode_num

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
                            search_status: str | None = None) -> dict:
    """scan_detail 最小骨架（装配前失败时的 fallback，保证前端可解析 failed_phase）。

    search_status（可观测，供日志/排查；前端暂不读，加键不破坏既有结构）：
    - "ok"：搜索阶段有 ≥1 个关键词成功（后续可能无候选 → message 走缺集文案）
    - "failed"：全部搜索关键词失败（_search_and_rank 抛 ScanSearchUnavailable）
    - "no_candidates"：搜索成功但无任何候选分享
    """
    return {
        "missing_total": 0,
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
                          M 个文件未匹配 / K 个超大小限制跳过 / J 个非视频 / 已有跳过
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
            parts.append(f"{size_filtered} 个超大小限制跳过")
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
            extra.append(f"{size_filtered} 个超大小限制跳过")
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
            parts.append(f"{size_filtered} 个超大小限制跳过")
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

# P3-1 done→failed 循环上限——对齐 transfer._RETRY_LIMIT（3 次）。
# _resolve_done_states 中 done→failed 算一次循环（retry_count +1），达到上限后
# 转为 node='failed'/state='failed' 终态（P1-5：与其它失败终态对齐），经 queue.py
# retry_task 人工解锁继续（此前保持 done 仅写 error，retry 只认 failed 无法干预卡死）。
_DONE_FAIL_RETRY_LIMIT = 3
# 转 failed 与达上限两种 error 文案（供测试与人工排查识别）
_DONE_FAIL_ERROR = "下载完成但 Emby 未入库，请人工确认"
_DONE_LIMIT_ERROR = "下载完成但 Emby 未入库已达循环上限，请人工核实 Emby 端"


# ---------------------------------------------------------------------------
# 三重匹配
# ---------------------------------------------------------------------------

# 公共化（tasks 层 utils）：_fmt_episode / _ep_num 实现迁至 app.utils
# （scan.py 与 library_check.py 原实现完全一致，本文件实现原样搬移），
# 这里保留局部名称绑定使全部调用点不变；_RE_SXXEXX / _RE_CN_EP 常量保留。
_fmt_episode = fmt_episode
_ep_num = parse_episode_num


def match_missing(text: str, missing_keys: set[str]) -> str | None:
    """三重匹配缺失集：SxxExx / SxxExxx / 第N集（跨季按集号匹配）→ 纯数字兜底。

    纯数字兜底（凡人修仙传 S01E190 案例）：网盘资源文件常为纯数字命名
    （`190.mkv`、`189.mkv`），既非 SxxExx 也非「第N集」——若不兜底，真实有效
    分享即使遍历到也匹配不到。双重收紧防歧义：
      ① 文件名主体提取 1-3 位数字块（开头或独立数字块，前置 [xxx] 标签可忽略）；
      ② 缺失集全部属于同一季 S，且 SxxE{数字} ∈ missing_keys 才命中。
    多季缺失（{"S01E01","S02E05"}）、数字超范围、纯数字与缺失集不吻合 → None，
    不影响既有三条规则（放在最后作为兜底）。
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
    # 3) 纯数字兜底：文件名主体为纯数字（190.mkv → 190），前置 [xxx] 标签可忽略。
    #    「190.2020.2160p.mkv」首个数字块 190 亦可；「风起天南1」首字符非数字不匹配。
    basename = text.rsplit(".", 1)[0] if "." in text else text
    m = re.match(r"^(?:\[[^\]]*\])*(\d{1,3})(?=\D|$)", basename)
    if m and missing_keys:
        ep = int(m.group(1))
        seasons = {_season_of_key(k) for k in missing_keys}
        if len(seasons) == 1:  # 多季缺失集：纯数字无法判定归属季 → 不匹配（防歧义）
            season = next(iter(seasons))
            key = _fmt_episode(season, ep)
            if key in missing_keys:
                return key
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
    return [ep.get("code") for ep in missing if ep.get("code")]


async def _resolve_done_states(media, missing_keys: set[str], movie_missing: bool) -> None:
    """done 防重解除（P1-1 语义迁移至 DownloadQueue）：Emby 二次确认入库 → 解除防重；仍未入库 → 转 failed 人工确认。

    影视下载两队列重设计后防重权威源 = download_queue（UNIQUE(media_id, episode)，§3.2），
    此处操作对象从 episode_state/transfer_queue/download_task 旧三表迁移为 download_queue。

    在 Emby 基线之后、搜索/入队之前执行（_scan_one 步骤 2b），对该 media 全部
    status='done' 的 download_queue 逐条判定：

    - tv 模式：episode key 不在 missing_keys → Emby 已确认入库 → 删除 download_queue
      （解除防重，允许后续探测重新入队）；仍在 missing_keys（Emby 仍缺失）→ 未入库 →
      条件更新转 failed。
    - movie 全量模式：episode=文件名。movie_missing=True（Emby 整部缺失）→ 未入库 → 转 failed；
      movie 已入库（missing=[]，movie_missing=False）→ 全部视为确认 → 删除。

    P3-1 done→failed 循环上限：转 failed 消耗一次 retry_count（SQL 表达式 +1），
    条件 `retry_count < _DONE_FAIL_RETRY_LIMIT`（对齐 transfer._RETRY_LIMIT=3）——未达上限
    才转 failed；已达上限的记录同样写 status='failed'（P1-5：与其它失败终态对齐，
    node_error/node_attempt 同步），使 queue.py retry_task 可人工解锁重试（此前保持 done
    仅写 error，retry 只认 failed 无法干预 → 卡死）。上限分支不通知（巡检避免噪音），
    task_run 由 _scan_one 主流程统一记录。

    全程条件删除/更新（WHERE 当前状态）不影响其他数据；异常由调用方 try/except 兜底，不阻断巡检。
    """
    async with async_session() as tx:
        async with tx.begin():
            done_rows = (
                (
                    await tx.execute(
                        select(DownloadQueue).where(
                            DownloadQueue.media_id == media.id,
                            DownloadQueue.status == "done",
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
            for dq in done_rows:
                if movie_missing or dq.episode in missing_keys:
                    if dq.retry_count >= _DONE_FAIL_RETRY_LIMIT:
                        # P3-1 + P1-5：已达循环上限 → 写 status='failed' 终态（与其它
                        # 失败终态对齐：node_error/node_attempt 同步），使 queue.py
                        # retry_task 可人工解锁。node_attempt 置上限值（retry 后由
                        # queue.py 重置为 0）；error 与 node_error 均写上限文案。
                        # 防无限循环语义不变：转 failed 消耗本轮，须人工 retry 才可再次
                        # done→failed，杜绝自动无限循环。status 改 failed 后下一轮
                        # select(status='done') 天然不命中。
                        await tx.execute(
                            update(DownloadQueue)
                            .where(
                                DownloadQueue.media_id == media.id,
                                DownloadQueue.episode == dq.episode,
                                DownloadQueue.status == "done",
                                DownloadQueue.retry_count >= _DONE_FAIL_RETRY_LIMIT,
                            )
                            .values(
                                status="failed",
                                node_error=_DONE_LIMIT_ERROR,
                                node_attempt=_DONE_FAIL_RETRY_LIMIT,
                                error=_DONE_LIMIT_ERROR,
                                updated_at=now,
                            )
                        )
                        at_limit += 1
                        continue
                    # Emby 仍未入库且未达上限 → 转 failed 进人工 retry 路径（防重保留）；
                    # P3-1：done→failed 算一次循环，retry_count SQL 自增（对齐 transfer 上限语义）
                    r_dq = await tx.execute(
                        update(DownloadQueue)
                        .where(
                            DownloadQueue.media_id == media.id,
                            DownloadQueue.episode == dq.episode,
                            DownloadQueue.status == "done",
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
                else:
                    # Emby 已确认入库 → 解除防重：删除 download_queue（条件删除，P1-4：
                    # 与同块 update(WHERE status='done') 对称，防误删 downloading 等
                    # 非 done 记录）
                    await tx.execute(
                        delete(DownloadQueue).where(
                            DownloadQueue.media_id == media.id,
                            DownloadQueue.episode == dq.episode,
                            DownloadQueue.status == "done",
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

def _season_of_key(key: str) -> int:
    m = re.match(r"[Ss](\d{1,2})", key or "")
    return int(m.group(1)) if m else 1


def _build_keywords(media, missing_keys: set[str]) -> list[str]:
    """搜索关键词：movie 用标题；tv 按缺失集所在季聚合（P4：一次巡检每个关键词只搜一次）。

    季词后追加纯标题兜底词（未匹配10 案例根因修复）：按季聚合的「标题 Sxx」召回不到
    标题不含 Sxx 的资源——如「凡人修仙传 (2020) 4K [更新190集]」这类按集连载动画，
    网盘资源多以年份/更新集数标识；纯标题词保证召回完整，再交由排序加权选择正确版本。
    """
    title = (media.title or "").strip()
    if not title:
        return []
    if media.media_type == "movie":
        return [title]
    seasons = sorted({_season_of_key(k) for k in missing_keys})
    kws = [f"{title} S{se:02d}" for se in seasons] if seasons else []
    if title not in kws:
        kws.append(title)  # 纯标题兜底词（排在季词之后）
    return kws


def _title_words(title: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", title or "") if len(w) >= 2]


def _rank_candidates(media, items: list[dict], year: str | int | None = None) -> list[dict]:
    """加分匹配排序：标题精确包含高分，部分词命中加分（忽略空格/大小写）。

    year（媒体首播年份，可选）：候选标题含该年份（如「凡人修仙传 (2020)」）时
    额外加权——同一剧名存在多版本（2020 动画版 vs 2025 新版）时优先召回与订阅
    一致的版本，避免版本错位导致全量未匹配（未匹配10 案例根因之二）。
    """
    title_norm = (media.title or "").replace(" ", "").lower()
    words = _title_words(media.title)
    year_str = str(year) if year is not None else None
    scored = []
    for it in items:
        name = str(it.get("title") or "").replace(" ", "").lower()
        score = 0
        if title_norm and title_norm in name:
            score += 10
        score += sum(2 for w in words if w in name)
        if year_str and year_str in name:
            score += 5
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


def _share_title_relevant(media_title: str, cand_title: str) -> bool:
    """A1 分享标题相关性：剧名为空 → True（兜底不阻断）；否则归一化（去空格、lower）
    后精确相等 / cand 以 title 开头（前缀）/ title 是 cand 的子串，任一命中 → True。

    全部不中（如「凡人修仙传合集」对 title「少帅」）→ False，候选在排序前直接剔除。
    注意 title 是子串即放行（「少帅将我宠上天…」对「少帅」仍通过）——A1 只过滤
    明显无关的分享；同名短剧等由 A2/A3 文件级校验拦截。
    """
    if not media_title:
        return True
    title_norm = media_title.replace(" ", "").lower()
    cand_norm = (cand_title or "").replace(" ", "").lower()
    if not cand_norm:
        return True  # 候选标题为空：兜底放行，避免误杀无标题候选
    return (
        cand_norm == title_norm
        or cand_norm.startswith(title_norm)
        or title_norm in cand_norm
    )


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

    - 单关键词失败：记录 warning 并继续（一个词失败、其余成功 → 不中断整轮，
      保持既有降级语义）。
    - A1 分享标题过滤（少帅式搜索误匹配修复）：剧名完全无关的分享候选在排序前直接
      剔除（精确/前缀/子串判定，剧名为空兜底放行）——减少无关分享被验证/遍历的
      浪费；同名短剧等标题相关的误匹配由 A2/A3 文件级校验（_full_mode_accept）拦截。
    - 全部关键词均失败（ok==0 且关键词数 ≥1）→ 抛 ScanSearchUnavailable：
      调用方（_scan_one）将 search 阶段标 error，区分「搜索故障」与「搜索成功但无候选」
      （后者返回 []，message 走缺集人话文案，不再误报「无候选命中」）。
    """
    keywords = _build_keywords(media, missing_keys)
    raw: list[dict] = []
    ok = 0  # 成功关键词计数
    for kw in keywords:
        try:
            raw.extend(await cloudsaver.search(kw))
            ok += 1
        except Exception as exc:
            logger.warning("[scan] cloudSaver 搜索 %s 失败: %s", kw, exc)
    if ok == 0 and keywords:
        # 所有搜索关键词都失败 → 搜索服务整体故障（静默降级会让 _scan_one 误以为
        # 「无缺集/无候选」），上抛由 _scan_one 记录 error + 阶段定位
        raise ScanSearchUnavailable("全部搜索关键词调用 cloudSaver 均失败")
    expanded = _expand_share_codes(raw)
    # A1 分享标题过滤：与剧名完全无关的候选直接剔除（排序前），减少无关分享被
    # share-info 验证/递归遍历的浪费；被过滤标题 debug 级记录（量可能大，不刷屏）。
    kept: list[dict] = []
    for it in expanded:
        if _share_title_relevant(media.title, it.get("title") or ""):
            kept.append(it)
        else:
            logger.info(
                "[scan] media=%s 分享标题与剧名无关，过滤候选: %s",
                media.id, (it.get("title") or "")[:60],
            )
    expanded = kept
    year = await _media_year(media)
    # 候选分享筛选：rank 排序后不再硬截断前 20（诊断：前 20/60 可能全是失效码，
    # 有效分享被挤出）——放行排序后最多 _MAX_RANK_CANDIDATES 个，验证/尝试上限由
    # _scan_one 按 share-info 成功数控制。
    return _rank_candidates(media, expanded, year=year)[:_MAX_RANK_CANDIDATES]


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
    """巡检入队只写 task_queue（queue-flow-rework Task 2：只写队列，移除同步 promote 双写）。

    探测完成即产出：scan 搜索/匹配/大小过滤通过后，把探测结果快照写 task_queue
    （status='ready'——转存凭据收集完毕，等待下载队列取件）。同步 promote 双写
    download_queue 已移除（下载队列后续从 task_queue 取件，Task 4）；download_name
    落盘名生成一并移除（迁移至 Task 7）。

    幂等：事务内先查 task_queue 同键记录，命中则跳过；写入撞
    UNIQUE(media_id, episode) 则捕获 IntegrityError 判定为并发冲突。
    返回 'enqueued' / 'existing' / 'conflict'。
    """
    async with async_session() as tx:
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

            tx.add(TaskQueue(
                media_id=media_id,
                episode=episode_key,
                file_name=file_name,
                file_size=file_size,
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
            try:
                await tx.commit()
                return "enqueued"
            except IntegrityError:
                await tx.rollback()
                # 并发冲突后补查 task_queue 记录（只写表，同键即已有任务；不做自动修复）
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
    """入队成功后触发下载队列消费（两队列重设计：process_download_queue 事件触发）。

    原 process_transfer_queue 更名为下载队列消费入口（消费 download_queue），
    语义与 §4.4 容量感知转存一致。fire-and-forget 后台任务持引用防 GC，
    scan_media 立即返回 task_run_id；模块未就绪时静默跳过。
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


def trigger_scan_background(media_id: int, *, manual: bool = False) -> None:
    """E-1（P1）：单部巡检 fire-and-forget 触发（HTTP 层不再同步等待完整巡检，
    防止 504 与 FastAPI 取消协程中断巡检）。复用 _background 强引用集合防 GC
    （与 _trigger_transfer 同模式）；内部持 per-media 锁（scan_media 自带）。

    manual=True（所有手动触发点：media 扫描按钮 / queue 探测 / 加集 / 审批通过）。
    queue-flow-rework Task 3 起：静默 unmatched 预过滤已移除，manual 不再改变
    _scan_one 行为（缺失集一律照常搜索，搜不到下轮自然重试）；参数保留仅为
    兼容既有调用契约（路由层仍按 manual=True 触发）。
    """
    async def _run() -> None:
        try:
            await scan_media(media_id, manual=manual)
        except Exception:  # noqa: BLE001
            logger.exception("[scan] 后台巡检异常 media=%s", media_id)

    task = asyncio.create_task(_run())
    _background.add(task)
    task.add_done_callback(_background.discard)


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


async def scan_media(media_id: int, *, manual: bool = False) -> int | None:
    """单影视巡检（API /api/media/{id}/scan 手动触发入口），返回最近一条 task_run id。

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
            return await _scan_one(media_id, manual=manual)
        finally:
            # 清理：无等待者时才移除（新调用方会重新创建，等待中的调用方仍持有旧锁引用）
            if _scan_lock_idle(lock):
                _scan_locks.pop(media_id, None)


async def scan_all_media_job() -> None:
    """定时 tick 包装（B 定时：每分钟；全局巡检间隔由本 job 触发周期控制）。

    queue-flow-rework Task 1：全局间隔不再由 per-media last_scan_at + 周期过滤，
    也不再读取 scan_interval_minutes 配置——每轮 tick 由 scan_all_media 遍历全部
    tracking/downloading 影视，调度粒度收敛到本 job 的 APScheduler IntervalTrigger。

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


async def scan_all_media(force: bool = False) -> None:
    """遍历全部 tracking/downloading 影视巡检（downloading 不跳过，防卡死，§3.1）。

    统一巡检调度（queue-flow-rework Task 1）：**移除 per-media 冷却过滤**——
    不再按各 media `last_scan_at + 周期` 到期判断，每轮 tick 直接遍历全部
    tracking/downloading 影视逐一巡检；全局巡检间隔仅由 job 触发周期
    （scan_all_media_job 的 APScheduler IntervalTrigger）控制。
    media.scan_interval_minutes / system_config "scan_interval_minutes" /
    settings.SCAN_INTERVAL_MINUTES 字段仅作兼容读取保留，不再参与调度。

    force（手动全量/CLI 入口）参数保留以兼容既有调用契约；因本函数已无到期
    过滤，force 不再改变遍历行为——全部 tracking/downloading 影视一律巡检。
    """
    async with async_session() as s:
        rows = (
            await s.execute(select(Media).where(Media.status.in_(("tracking", "downloading"))))
        ).scalars().all()
    for media in rows:
        try:
            await scan_media(media.id)
        except Exception:
            logger.exception("[scan] media=%s 巡检异常", media.id)


async def _create_scan_run(media_id: int) -> int | None:
    """巡检可见性改造（第一段）：插入一条 status='running' 的 task_run 并 commit，返回 id。

    触发巡检即落 running 中间态——HTTP 触发到巡检结束（数十秒）期间前端队列即可
    看到「正在巡检」记录。running 记录不含 phases/scan_detail（跑完才一次性落库）。

    media 不存在时调用方（_scan_one）在预检前即 return None，不建立记录（保持现状）。
    """
    async with async_session() as s:
        run = TaskRun(
            task_type="scan_media",
            media_id=media_id,
            status="running",
            message="正在巡检",
            started_at=_now(),
            finished_at=None,
            duration_seconds=None,
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
        return run_id


async def _scan_one(media_id: int, *, manual: bool = False) -> int | None:
    """严格按设计文档 §4.3 巡检主流程，阶段2 只到入队为止（不转存）。

    巡检可见性改造：入口先建 status='running' 的 task_run（_create_scan_run），
    全部返回分支改走 _finish_scan_run 原地 UPDATE 同一条为终态，附 5 阶段进度
    （phases JSON）与结果摘要（scan_detail JSON）。巡检是快任务——阶段打点全部
    在内存进行（_phase_start/_phase_done），结束时一次性落库，运行中不实时写 DB。

    P1-1（延后项）：长事务拆分——Emby 基线 / 搜索 / share-info / share-list 递归
    等网络 IO 全程**不持有 DB session**（SQLite 单连接被长事务占住会阻塞其他写）；
    仅 DB 读写使用短事务（读 media、task_run 写入、media.last_scan_at、
    _read_size_limits、_enqueue 均各自开启/关闭 session）。database.async_session
    expire_on_commit=False，开头短会话读出的 media 为 detached 对象，已加载属性
    （id/status/title/tmdb_id/media_type 等）可安全继续使用。

    manual（queue-flow-rework Task 3 起为兼容保留）：静默 unmatched 预过滤已移除，
    手动/定时调度行为一致——缺失集一律照常搜索，搜不到下轮全局巡检自然重试。
    """
    t0 = time.monotonic()  # Q8①：真实耗时（单部巡检）

    # 0. 短事务读取 media（立即关闭；detached 属性后续安全）
    async with async_session() as s:
        media = await s.get(Media, media_id)
    if media is None:
        # 不存在 → 不建 running 记录，直接 return None（保持现状契约）
        logger.warning("[scan] media=%s 不存在", media_id)
        return None

    # 0b. 巡检可见性：触发即落 running 中间态（前端可立即看到「正在巡检」）。
    #     此后所有分支（含异常）都以 _finish_scan_run 原地 UPDATE 同一记录为终态。
    run_id = await _create_scan_run(media_id)
    if run_id is None:
        # running 记录建立失败（极小概率）→ 保持旧行为，不阻断巡检主流程
        return None
    phases = _new_phases()
    scan_detail = None  # 无遗漏/被跳过等短路分支无结果摘要；正常流程开始后才装配

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

    # 1. 状态预检：paused/error 跳过；downloading 不跳过（防卡死），仅本轮不入队
    if media.status in ("paused", "error"):
        _phase_skip_remaining(phases, "check")  # 尚未开始的阶段标记 skipped（未达完成态）
        return await _finish(
            "skipped", f"media.status={media.status}，跳过巡检",
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
        scan_detail = _result_detail_skeleton("check")
        return await _finish(
            "error",
            f"Emby 故障，fail-safe 暂停新缺集发现: {exc}",
            touch_last_scan_at=True,
        )
    _phase_done(phases, "check")

    if missing is None:
        # 基线不可用：Emby 未收录该剧集，无法探明遗漏。
        # _emby_missing_codes 已按开关分流——仅当 scan_baseline_required=True（强防重，
        # 旧行为）才返回 None；False（默认）返回全量模式 [None]，走下方搜索入队路径。
        # 此处保留旧行为（本轮跳过），并区分文案以与全量模式日志区分。
        _phase_skip_remaining(phases, "search")  # search/match/enqueue/finish 未执行 → skipped
        return await _finish(
            "skipped",
            "Emby 未收录该剧集，防重基线强制（scan_baseline_required=True），本轮跳过",
            touch_last_scan_at=True,
        )

    missing_keys = {m for m in missing if m is not None}
    # movie_missing 真 = 全量模式（Emby 基线缺失 soft 全量）：
    #   - 真电影（media_type=="movie"）整部缺失 → [None]；
    #   - tv 未收录（Emby 未收录 + scan_baseline_required=False 软处理）→ [None]，
    #     即「tv 未收录全量」= movie_missing 且 media_type!="movie"——少帅式搜索误匹配
    #     修复（A2/A3/A4）只对该分支生效：每文件校验（_full_mode_accept）+ 单轮限批，
    #     防同名短剧/无关资源一次入队爆炸；真电影全量语义不同（单文件常带片名），保留
    #     原行为不套用校验（见下方 match 阶段注释）。
    movie_missing = any(m is None for m in missing)

    # 2b. done 防重解除（P1-1，Oracle 审查）：Emby 二次确认入库 → 删除 es/tq/dl 解除防重；
    #     仍未入库 → 转 failed 人工确认。异常仅告警，不阻断巡检。
    #     _resolve_done_states 自带短 session，此处不涉及任何长事务。
    try:
        await _resolve_done_states(media, missing_keys, movie_missing)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scan] media=%s done 防重解除失败（不阻断巡检）: %s", media_id, exc)

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
            item = {"episode": m, "result": "not_found"}
            missing_items.append(item)
            missing_items_by_key[m] = item
    scan_detail = _result_detail_skeleton()
    scan_detail["missing_total"] = missing_total
    scan_detail["missing_items"] = missing_items

    if not missing_keys and not movie_missing:
        # 无遗漏 → 短路结束（消灭 P3 空跑）
        _phase_skip_remaining(phases, "search")  # 后续阶段未执行 → skipped
        return await _finish(
            "skipped", "无遗漏集（Emby 基线已覆盖），跳过",
            touch_last_scan_at=True,
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
            touch_last_scan_at=True,
        )
    _phase_done(phases, "search")
    scan_detail["search_status"] = "ok" if candidates else "no_candidates"

    # 4-6. share-info(500ms 串行) → share-list 递归遍历文件 → 大小过滤 → 三重匹配 → 入队
    #      （大小上限经独立短 session _read_size_limits 读取，网络 IO 阶段不持 DB session）
    #                                                 —— phase: match + enqueue
    try:
        ep_limit, movie_limit = await _read_size_limits(media)
    except Exception as exc:  # noqa: BLE001  大小上限读取失败，fail-closed 中止
        _phase_error(phases, "match")
        scan_detail["failed_phase"] = "match"
        return await _finish(
            "error", f"巡检中止: 大小过滤上限读取失败: {exc}",
            touch_last_scan_at=True,
        )
    limit_gb = _size_limit_gb(media, ep_limit, movie_limit)
    skip_enqueue = media.status == "downloading"  # 有进行中任务本轮不入队，但仍检查遗漏

    # A2/A3/A4：tv 未收录全量模式（Emby 未收录 + scan_baseline_required=False，
    # movie_missing 且 media_type!="movie"）→ 文件级校验 + 单轮限批，防同名短剧/无关
    # 资源一次入队爆炸（「少帅」搜索入队 284 个错误资源案例修复）。真电影全量模式
    # 语义不同（整部缺失的单文件常带片名），保留原行为不套用校验。
    tv_full_mode = movie_missing and (media.media_type or "").strip().lower() != "movie"
    total_episodes = await _media_total_episodes(media) if tv_full_mode else None

    enqueued = existing_skipped = size_filtered = unmatched = non_video = 0
    unrelated_filtered = 0  # A2：全量模式文件级校验拒绝计数（无关/超集号文件）
    enqueue_limited = False  # A4：全量模式限批触发标记（停止遍历后续分享/文件）
    unmatched_files: list[str] = []  # 未匹配文件名样例（至多收集 3 个，供 message 定位）
    share_info_ok = share_info_fail = walk_fail = tried = 0
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
                matched_key = match_missing(file_name, missing_keys)
                if not matched_key:
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
    scan_detail.update({
        # 全量模式：基线表达为 missing（[None]），实际缺失实体是搜索到的具体文件，
        # 此处以实际收集到的缺失集条目数兜底（缺失集实体不预知，契约允许宽松填充）。
        "missing_total": len(missing_items) if movie_missing else missing_total,
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
    # 6b. 入队成功后触发转存消费（§4.4 事件触发；transfer lane 未就绪时静默跳过）
    #     fire-and-forget 在 DB session 外触发（_background 强引用集合防 GC）
    if enqueued:
        await _trigger_transfer()
    return rid

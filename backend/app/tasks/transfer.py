"""
下载队列消费任务（影视下载两队列重设计 P5：旧三表 → DownloadQueue 单表）。

设计依据：docs/影视下载两队列重设计.md §3.2（表结构）/ §4.2（执行层状态机）/
§5（容量预算并发）/ §6.2（aria2 回调链路）。

消费对象为 download_queue 单表（防重权威源 = UNIQUE(media_id, episode)，由 scan
探测 → task_queue 取件生成：全部分享快照；download_name 取件时为空，转存落盘后
由 _ensure_download_name 后置生成落库，Task 7）。本模块替换旧
episode_state + transfer_queue + download_task 三表联动实现，保留全部既有逻辑
语义（GID 来源校验 / /quark 挂载预检 / 容量模型 B fail-closed / save 幂等
P2-10·P0-1 / _get_link_wait_visible / addUri / 失败回退 CAS / 节点级状态机）。

执行状态机（§4.2）：
    pending ──转存──▶ transferring ──提交aria2──▶ downloading ──hook/轮询──▶ scrape
        │                  │                          │
        │                  └── 失败（确定性/超时）──▶ failed
        └── 容量不足 ──▶ pending（quota_reject_count++，不消耗 retry_count）
    scrape ──(刮削执行器/Emby 入库确认，见 library_check lane)──▶ library ──▶ done

容量预算并发（§5，议会 P0 裁决）：
- reserved 唯一可信源 = DB 聚合：SELECT SUM(file_size) WHERE status IN
  ('transferring','scrape','library')——**不含 downloading**（P1-4 收紧：该状态已
  落盘，由容量 check 内层 used 覆盖，避免双重计算假性容量不足）。无独立账本，
  随状态迁移自动增减（释放点 = 离开集合的任一转移：→downloading/done/failed/
  回退 pending）。
- 准入并发：process_transfer_queue 每轮可准入多个任务，准入唯一约束 = 网盘容量
  （不再设并发数上限）。每个任务准入前在同一事务内完成
  「读 reserved 聚合 + 容量 check + CAS 抢占（pending→transferring）」；
  容量不足 → quota_wait 按网盘空间排队（不消耗 retry），空间释放后唤醒重试。
  原子性 = 进程内 _admission_lock（单 worker 可靠）+ SQLite 单写者
  （事务内先写锁行触发写锁，准入段跨进程串行）+ 行级 CAS 条件更新
  （记预留 = 抢占本身，绝不重复准入同一任务）。
- 容量判定入参 = reserved + file_size（check 语义「used+candidate+margin≤quota」
  展开为「used+reserved+本集+margin≤quota」，§5.1 模型 B 推广）。
- 暂停开关（§8.2）：system_config download_queue_paused=true → 本轮不取新任务
  （在途继续完成），且不唤醒 quota_wait。
- quota_wait（§4.2，P1 落地）：容量不足置 quota_wait + wait_since + quota_reject_count++
  （不消耗 retry）；消费入口统一唤醒回 pending，>24h 持续等待发 flow_error 告警。

aria2 回调链路（§6.2）：
- trigger_download_complete(gid)：P6 回调端点延迟导入调用，按 aria2_gid 反查
  downloading → 条件更新 downloading→scrape（幂等，二次回调返回 False）。
- 事件丢失兜底：轮询（_poll_downloading_tasks）不变，回调与轮询并发推进由
  条件更新幂等兜底。

核心约定（全系统正确性相关，勿破坏）：
- 防重权威源 = download_queue；所有状态转移用「条件更新（WHERE 当前状态）」捕获
  行级冲突，幂等可重复执行（不重复计数/通知/触发同步）。
- 每次转移显式写 updated_at=now（SQLAlchemy `onupdate` 只在 ORM 赋值时生效，
  execute(update) 必须显式传值）；recovery 依赖 updated_at 判定超时回退。
- retry_count 仅「确定性失败/超时回退」消耗；quota 拒绝只走 quota_reject_count
  （§4.5，绝不消耗 retry/node_attempt）。
- 转存链路（save → get_link → add_uri）每步失败走「重试路径」，只在 node_attempt
  ≥3 时转 failed（需人工 retry）。
"""
import asyncio
import json
import logging
import re
import time as _time
from datetime import timedelta

from sqlalchemy import DateTime, Text, case, exists, func, insert, literal, select, update

from app.config import settings
from app.database import async_session
from app.models import DownloadQueue, Media, SystemConfig, TaskQueue
from app.services import alist, aria2, capacity, cloudsaver, config_store
from app.services.notifier import (
    EVENT_DOWNLOAD_COMPLETE,
    EVENT_DOWNLOAD_STARTED,
    EVENT_FLOW_ERROR,
    NotifyEvent,
    notifier,
)
from app.tasks import as_bool, record_task_run
from app.tasks.library_check import scrape_runner
# tasks 层公共纯函数（app.utils，仅标准库）：统一时间源与夸克路径拆分
from app.utils import now_utc_naive as _now, split_quark_path

# 公共化（tasks 层 utils）：_split_quark_path 实现迁至 app.utils（本文件原实现
# 原样搬移，与 recovery.py 原实现一致），保留局部名称使调用点不变
# （library_check 等以 transfer_mod._split_quark_path 延迟导入引用）。
_split_quark_path = split_quark_path

logger = logging.getLogger(__name__)

_IMPLEMENTED = True

# aria2 任务 comment 来源标记前缀（add_uri 透传用；2026-09 修订：aria2 1.36.0
# 静默丢弃 comment option，GID 来源校验已改用 DB gid 白名单（_admit_batch 段 2），
# comment 仅作未来 aria2 版本兼容的冗余标记，不再参与校验）
_COMMENT_PREFIX = "lumencloud:"
# P2（影视下载两队列重设计 §7）：aria2 落盘名格式化正则（对齐 n8n formatFileName，
# SxxExx 命中 → 「剧名 - SxxExx - 第 N 集.ext」；SxxExxx 三位集数保留）。
# queue-flow-rework Task 7：download_name 改为转存落盘后置生成（_ensure_download_name），
# 不再由 scan enqueue / 取件时计算——DQ 行创建时该列为空，转存链中首次格式化落库，
# 之后消费（aria2 out / quark_path / local_path / 入库）直接复用 dq.download_name。
_RE_FORMAT_SE = re.compile(r"(S\d+)E(\d+).*\.([^.]+)$", re.IGNORECASE)


def _format_download_name(file_name: str, title: str | None, media_type: str | None,
                          episode_key: str | None = None) -> str:
    """aria2 落盘名（out 参数）格式化（影视下载两队列重设计 §7，对齐 n8n formatFileName）。

    规则：
    - 剧集（media_type != movie，文件名含 SxxExx）→ `{title} - {SxxExx} - 第 {N} 集.{ext}`
    - 文件名无 SxxExx（如分享内纯数字命名 `190.mkv`）→ 用 episode_key 兜底规范化
      `{title} - {SxxExx} - 第 {N} 集.{ext}`——下载必须携带集号标识（n8n 实际下载
      均带 SxxExx，如「仙逆 - S01E150 - 第 150 集.mkv」）
    - 电影/全量模式（media_type == movie）→ `{title}.{ext}`（用户确认统一格式化，
      去掉夸克杂乱分享名前缀/后缀）
    - 其余（剧集但文件名与 episode_key 都取不到集号 / 标题缺失）→ 保持原名
      （n8n fallback，不误改）

    注意：只影响 aria2 本地落盘名，quark 网盘原文件与 episode 防重键均不动
    （§7「防重键与落盘名分离」，改名永不回写防重键）。
    调用方：_ensure_download_name（转存后置生成落库，Task 7）；本模块消费
    dq.download_name 作为 addUri out，不再二次格式化。
    """
    if not file_name:
        return file_name
    if (media_type or "").strip().lower() == "movie":
        if not title:
            return file_name
        ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
        return f"{title}.{ext}" if ext else title
    m = _RE_FORMAT_SE.search(file_name)
    if m and title:
        full_se = m.group(1) + "E" + m.group(2)
        episode_num = int(m.group(2))
        return f"{title} - {full_se} - 第 {episode_num} 集.{m.group(3)}"
    # 文件名无 SxxExx（纯数字命名等）→ 用 episode_key 兜底规范化（§7 线下反馈：
    # 不改名下载的裸文件名无法在媒体库识别集号）。
    if episode_key and title:
        m2 = re.match(r"\A(S\d+)E(\d+)\Z", episode_key.strip(), re.IGNORECASE)
        if m2:
            se = m2.group(1).upper() + "E" + m2.group(2)
            ep_ext = file_name.rsplit(".", 1)[-1] if "." in file_name else ""
            base = f"{title} - {se} - 第 {int(m2.group(2))} 集"
            return f"{base}.{ep_ext}" if ep_ext else base
    return file_name


async def _ensure_download_name(dq_id: int, media_id: int, episode: str,
                                file_name: str) -> str | None:
    """为 DQ 行生成并落库 download_name（转存后置生成，Task 7，CAS 幂等）。

    背景（queue-flow-rework Task 2/4）：download_name 不再在入队/取件时生成——
    DownloadQueue 行创建时该列为空，由本函数在转存链中**落盘可见/改名 前**首次
    计算并落库（设计 D5「下载名称后置生成」）。

    - 计算源：media.title / media.media_type + episode_key，规则原样复用
      _format_download_name（「影视名 - SxxExx - 第 N 集」，对齐 n8n）；
    - 落库用条件更新 WHERE status='transferring'（rowcount 门控，CAS 防重写）：
      并发方（recovery 超时回退 / 人工 retry）已变动 → 命中 0 行 → **回读该行
      当前 download_name**：并发方已写入则返回之（rename/out 与并发方保持一致，
      防命名分叉）；行被重置且无 download_name → 返回 None（沿用原始名兜底）；
    - 重试幂等：调用方仅在 download_name 为空时调用本函数；已生成则跳过——
      绝不重复格式化/改名（改名动作由 _get_link_wait_visible(rename_to=...) 完成，
      这里只负责计算与落库）。

    返回格式化结果（CAS 命中）或回读的已落库名 / None（CAS 未命中且行内无
    download_name / 无可格式化名）。
    """
    async with async_session() as s:
        media = await s.get(Media, media_id)
    formatted = _format_download_name(
        file_name,
        media.title if media is not None else None,
        media.media_type if media is not None else None,
        episode,
    )
    if not formatted:
        return None
    now = _now()
    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id, DownloadQueue.status == "transferring")
                .values(download_name=formatted, updated_at=now)
            )
    if r.rowcount != 1:
        # CAS 未命中（并发方已改动该行，如 recovery 回退 / 人工 retry）：
        # 回读该行当前 download_name——并发方已写入则返回之，rename/out 与其
        # 保持一致（不返回 None 导致 aria2 out / DB download_name / quark_path
        # 三方命名分叉）；行被重置且无 download_name（recovery 只回退状态未写名）
        # → 返回 None 沿用原始名（保守兜底，后续 _commit_downloading 同类 CAS
        # 兜底冲突处理）。
        async with async_session() as s:
            row = await s.get(DownloadQueue, dq_id)
        if row is not None and row.download_name:
            logger.info(
                "[transfer] 转存后 download_name CAS 未命中，回读已落库名: %s",
                row.download_name,
            )
            return row.download_name
        return None
    logger.info("[transfer] 转存后生成 download_name: %s", formatted)
    return formatted


# 确定性失败 / 超时回退消耗 retry_count 的上限：≥3 转 failed，需人工 retry（§4.5）
_RETRY_LIMIT = 3
# save 受理后等待转存文件在 alist 可见的超时上限（秒）。
# 阶段 3 实证 + 线上反馈：1.5-2.6G 大文件落盘耗时 60-180s，叠加 alist 同步延迟，
# 180s 上限偏紧（个别超时）；放宽至 300s 作兜底。超时抛 AlistUnavailable 走外层
# 重试路径（node_attempt++，≥3 → failed），故上限放宽不造成「无限等」，只是多给一轮。
_LINK_WAIT_TIMEOUT = 300.0
# P0-1（council 兜底）：save 受理时间超时上限（秒）。save_task_id 存在但
# save_attempt_at 距今超过该值（或该列为空——旧数据/某清空路径漏写）→ 视为 stale，
# 强制重新 save。即使任何清空路径漏了，超 10 分钟也会强制重 save，杜绝盲等死循环。
_SAVE_ATTEMPT_MAX_SECONDS = 600
# P3-2（council）：quota 拒绝累计告警阈值——容量不足连续累计 ≥5 次触发
# flow_error 告警（复用 P2-2 的 capacity 类别节流，防每分钟 job 刷屏）。
# quota 拒绝只走 quota_reject_count，绝不消耗 retry_count（§4.5）。
_QUOTA_REJECT_ALERT_THRESHOLD = 5
# P3-3（Oracle 审查）：后台任务强引用集合——防 asyncio.create_task 的任务被 GC 回收未执行
_background_tasks: set[asyncio.Task] = set()
# P5（§5 容量预算并发）：进程内准入锁——串行化「读 reserved → 容量 check → CAS 抢占」
# 段（单 worker 部署可靠；多 worker 由 SQLite 单写者 + 行级 CAS 条件更新兜底，
# 见 _try_admit_one 事务级锁注释）。
_admission_lock = asyncio.Lock()
# queue-flow-rework Task 6：事件消费触发互斥锁——scan 入队成功 / 入库完成（容量释放）
# 等多路事件 fire-and-forget 触发下载队列消费时，同一时刻只允许一轮消费在跑（防重入；
# 多路事件的重叠遗漏由每分钟 process_transfer_queue_job 兜底）。与每分钟 job / 手动
# 重试的重叠并行仍由 _try_admit_one 的「进程锁 + 事务级锁 + 行级 CAS」保证正确。
_consume_trigger_lock = asyncio.Lock()

# P2-2（council）：flow_error 通知节流窗（秒）。GID 校验失败/容量不可用等
# fail-closed 场景由每分钟兜底 job 重复触发，同一告警 10 分钟内只 notify 一次，
# 防通知刷屏（task_run(error) 仍每次记录，仅通知节流）。
_ALERT_COOLDOWN_SECONDS = 600.0
# P2-2：告警节流表。key = f"{media_id}:{category}"；值 = (最近 notify 的
# monotonic 时间戳, 上次消息)。同一 key 在窗口内重复触发时，仅当消息与上次
# **完全相同**才跳过 notify（消息变化视为根因变化的新告警，必须通知）。
_alert_cooldown: dict[str, tuple[float, str]] = {}

# fix-transfer-flow-reliability Task 4（design T2 双层之二）：陌生 gid 连续跳过哨兵。
# 白名单未命中（不在 DB 任何行）的 aria2 活动/等待任务每轮 strikes += 1，连续
# _GID_STRIKE_LIMIT(3) 轮未消失 → best-effort aria2.remove 清理 + 告警并清计数——
# 防 recovery 回退时 aria2.remove 失败遗留的孤儿 gid 永久阻断转存（自锁）；remove
# 成功后下轮 actives 不再含该 gid → 自动恢复转存。计数为进程内共享状态（单 worker
# 部署可靠；重启即清零，重启后至多多计数 3 轮，不影响正确性）。
_GID_STRIKE_LIMIT = 3
_unknown_gid_strikes: dict[str, int] = {}

# P5（§5 容量预算并发）：reserved 聚合口径（议会验证 P1-4 收紧）——
# **不含 downloading**：该状态已落盘，容量由 capacity.check 内层 used（alist /quark
# 递归）覆盖，再计入会双重计算导致假性容量不足（安全但过度保守）。reserved =
# 「未落盘在途预留」（transferring/scrape/library），离开即自动释放。
_INFLIGHT_STATUSES = ("transferring", "scrape", "library")
# 进行中态判定（媒体不再有任一进行中任务 → 回 tracking）：排队/配额等待也算处理中。
_ACTIVE_STATUSES = ("pending", "transferring", "downloading", "scrape", "library", "quota_wait")
# 准入原子段事务级锁行（system_config 表键；SQLite 单写者下写即持写锁，
# PG 多 worker 下需预置该行方可 SELECT FOR UPDATE 串行化准入段）
_ADMISSION_LOCK_KEY = "_transfer_admission_lock"
# 下载队列暂停开关（§8.2 暂停=不取新+在途继续；queue.py 的 pause/resume API 写入
# system_config，不刷新进程内缓存 → 本模块必须直读 DB 而非 config_store）
_PAUSE_CONFIG_KEY = "download_queue_paused"
# quota_wait 超时告警阈值（>1 天持续等待 → flow_error 通知，议会验证 P1）
_QUOTA_WAIT_ALERT_HOURS = 24


def _spawn(coro_factory) -> None:
    """创建后台任务并持引用（事件触发续跑 / 刮削执行器触发共用）。

    coro_factory：返回 coroutine 的可调用对象（如 scrape_runner）。
    任务完成/取消后从 _background_tasks 移除（回调 discard）。
    """
    task = asyncio.create_task(coro_factory())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _normalize_name(name: str) -> str:
    """文件名归一：去全部空白字符、全/半角括号统一、转小写（L4 跨目录匹配用）。"""
    return re.sub(r"\s+", "", name or "").replace("（", "(").replace("）", ")").lower()


def _name_without_ext(name: str) -> str:
    """归一后去掉扩展名（最后一个 '.' 后视为扩展名），供模糊匹配比较。"""
    norm = _normalize_name(name)
    if "." in norm:
        return norm.rsplit(".", 1)[0]
    return norm


def _find_real_name(entries: list[dict], file_name: str) -> str | None:
    """在 alist 目录条目中查找 file_name 对应的真实文件名（L4）。

    三级匹配：
      1) 原样精确匹配（大小写敏感，最严格）；
      2) 归一后精确匹配（去空格 / 全半角括号统一 / 小写）；
      3) 去扩展名差异模糊匹配（归一后再忽略扩展名，容忍
         "ep.MKV" vs "ep.mkv" 之类差异）。
    命中返回条目真实名（供 alist.get_link(f"/quark/{真实名}") 使用——
    文件名可能被夸克规范化改名，必须用列表里的真实名取直链）；
    找不到返回 None。
    """
    target = (file_name or "").strip()
    if not target:
        return None
    norm_target = _normalize_name(target)
    stem_target = _name_without_ext(target)
    for e in entries:
        name = (e.get("name") or "").strip()
        if name and name == target:
            return name
    for e in entries:
        name = (e.get("name") or "").strip()
        if name and _normalize_name(name) == norm_target:
            return name
    for e in entries:
        name = (e.get("name") or "").strip()
        if name and _name_without_ext(name) == stem_target:
            return name
    return None


async def _get_link_wait_visible(file_name: str, timeout: float = _LINK_WAIT_TIMEOUT,
                                 *, rename_to: str | None = None) -> tuple[str, str]:
    """save 受理后轮询等待转存文件在 alist /quark 可见，返回 (直链, 最终 quark 路径)。

    阶段 3 实证：cloudSaver save 返回 task_id 受理后转存**异步落盘**，alist 同步存在
    延迟（阶段 1 实测 164KB srt 约 15s 落盘；实证 1.5-2.6G 文件落盘耗时 60-180s，
    n8n 用「Wait(10s)」节点兜底但大文件不够）。立即 get_link 会 object not found。

    rename_to（用户需求：转存后立即在 alist/quark 改名，下载前即规范化集号命名）：
      落盘真实名确认后、取直链前，把 `/quark/{真实名}` 重命名为 rename_to
      （如 `凡人修仙传 - S01E190 - 第 190 集.mkv`）；成功则轮询目标切换为新名、
      直链取新名路径；失败仅告警（fail-open，沿用原名继续，不阻断下载）。
      返回的第二项 = 最终 quark 路径（改名成功为新名，否则原名）——调用方需据此
      同步 download_queue.quark_path（后续清理/删除以最终名定位）。

    L4（五节点模型，oracle 决策「转存全失败」根因修复）：原实现每 5s 直接
    `alist.get_link(f"/quark/{file_name}")` 轮询 300s——get_link 调 /api/fs/get
    不带 refresh，吃 AList 缓存索引，且文件名可能被夸克规范化改名，导致文件实际
    落盘却永远拿不到直链。改造后每轮：
      1) 先 `alist.list_dir("/quark")`（该函数带 refresh=True 即时刷新）拿真实目录；
      2) 精确匹配当前目标名（file_name 或改名后的 rename_to）→ 用真实名直链；
      3) 精确不中 → 模糊匹配（_find_real_name：去空格 / 全半角括号统一 / 去扩展名
         差异），命中用真实名取直链；
      4) 仍不中（或 list_dir 异常）→ 退化按原路径 get_link 再试一次——目录列表可能
         因同步延迟暂未含该文件而 /api/fs/get 缓存已可见，成功即返回；失败仅记录，
         sleep 5s 进入下一轮（总超时 _LINK_WAIT_TIMEOUT=300s 不变）；
      5) 超时抛 AlistUnavailable，错误信息按最后一次 list_dir 是否命中目标文件区分：
         - 已落盘但直链失败（real_name 命中）→ 提示文件已可见、url/raw_url 均空（缓存或
           夸克直链生成问题），不再误导性提示核对 folderId；
         - 未落盘（list_dir 一直看不到目标）→ 保留原「等待落盘超时」语义并附 folderId
           核对提示（诊断「落盘到了别处（folderId 配置错）」与「文件名被云盘改名/仍在传输」）。
    """
    import time as _time

    path = f"/quark/{file_name}"
    expected = file_name  # 轮询目标名；改名成功后切换到新名（否则改名后按旧名找不到）
    deadline = _time.monotonic() + timeout
    last_exc: Exception | None = None
    last_entries: list[dict] = []
    while _time.monotonic() < deadline:
        try:
            entries = await alist.list_dir("/quark")
        except Exception as exc:  # noqa: BLE001  list_dir 故障 → 记诊断后退化原路径直链
            last_exc = exc
            entries = []
        last_entries = entries or []
        real_name = _find_real_name(last_entries, expected)
        try:
            if real_name is not None:
                final_name = real_name
                if rename_to and rename_to != real_name:
                    try:
                        await alist.rename(f"/quark/{real_name}", rename_to, overwrite=True)
                        expected = rename_to
                        final_name = rename_to
                        logger.info(
                            "[transfer] 转存落盘后改名 %s → %s",
                            real_name, rename_to,
                        )
                    except Exception as exc:  # noqa: BLE001 改名失败 fail-open 沿用原名
                        logger.warning(
                            "[transfer] quark 改名 %s → %s 失败（沿用原名继续）: %s",
                            real_name, rename_to, exc,
                        )
                return await alist.get_link(f"/quark/{final_name}"), f"/quark/{final_name}"
            # 退化兜底：原文件名直链（缓存可能已可见）；失败进入下一轮等待
            return await alist.get_link(path), path
        except Exception as exc:  # noqa: BLE001  直链暂不可用（未同步/瞬时失败）→ 继续等待
            last_exc = exc
            await asyncio.sleep(5)
    # 超时诊断：抛错前先按最后一次 list_dir 是否命中目标文件区分两种情况——
    # 命中 → 文件已落盘，失败在直链；未命中 → 文件未落盘（可能落盘到别处/仍在传输）。
    # last_entries 在循环内已保存，这里重算一次 real_name 取最后状态。
    real_name = _find_real_name(last_entries, file_name)
    recent_names = [e.get("name") for e in last_entries[:10]]
    if real_name is not None:
        # 文件已落盘但直链获取失败：folderId 核对提示会误导（folderId 配错则文件
        # 根本不会出现在 /quark），改为提示 AList 缓存/夸克直链生成问题。
        raise alist.AlistUnavailable(
            f"转存后文件已落盘但 AList 直链获取失败（{timeout:.0f}s）: {path}（{last_exc}）；"
            f"最近 list_dir 已可见目标文件（前 {len(recent_names)} 条目: {recent_names}）；"
            f"url 与 raw_url 均为空，可能为 AList 缓存或夸克直链生成问题"
        )
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
        f"最近 list_dir 前 {len(recent_names)} 条目: {recent_names}；"
        f"请用 alist 管理 API /api/admin/storage/list 核对 quark_default_folder 是否为 root_folder_id"
    )


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
    t0 = _time.monotonic()  # Q8①：真实耗时
    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(DownloadQueue).where(DownloadQueue.status == "downloading")
                )
            )
            .scalars()
            .all()
        )
        snap = [
            (dq.id, dq.media_id, dq.episode, dq.file_name, dq.quark_path,
             dq.aria2_gid, dq.retry_count or 0, dq.node_attempt or 0)
            for dq in rows
        ]
    if not snap:
        return

    aria2_errors: list[str] = []
    for dq_id, media_id, episode, file_name, quark_path, gid, retry_c, node_attempt in snap:
        try:
            st = await aria2.client.tell_status(gid)
        except Exception as exc:  # aria2 故障 → 本轮跳过（不误判失败，交给 recover 超时兜底）
            aria2_errors.append(f"media={media_id} ep={episode}: {exc}")
            logger.warning("[transfer] aria2 轮询失败（本轮跳过，不误判失败）: %s", exc)
            continue

        status = (st or {}).get("status")
        if status == "complete":
            # Task 2：用 aria2 totalLength 回填真实 file_size 并清估算标记；
            # totalLength 缺失/非法/为 0 → real_size=None（不回填，保持估算值）。
            # totalLength=0（0 字节文件）视为无真实大小：影视场景几乎不存在，
            # 且保持估算值比回填 0 更安全（0 字节会误导「空文件」展示）。
            try:
                real_size = int((st or {}).get("totalLength") or 0) or None
            except (TypeError, ValueError):
                real_size = None
            await _complete_download(dq_id, media_id, episode, file_name, quark_path,
                                     retry_c, node_attempt, real_size=real_size)
        elif status in ("error", "removed"):
            await _fail_download(dq_id, media_id, episode, file_name, quark_path,
                                 retry_c, node_attempt, f"aria2 任务状态 {status}")
        else:
            # active / waiting（及未知状态按进行中处理）：刷新 updated_at
            if status == "paused":
                # P2-9（council）：aria2 任务被外部暂停 → 不再刷新 updated_at，
                # 让其超过 episode_state_timeout_hours 老化后由 recover 超时回退
                # pending（+ 清理残留）；此前 paused 也刷新进度导致 recover 超时
                # 永不触发，任务永久卡在 downloading。
                logger.debug(
                    "[transfer] aria2 任务被外部暂停（gid=%s），不刷新进度，等待 recover %sh 超时回退",
                    gid, settings.EPISODE_STATE_TIMEOUT_HOURS,
                )
            else:
                await _refresh_progress(dq_id)

    if aria2_errors:
        async with async_session() as s:
            await record_task_run(
                s, "transfer", "error",
                "aria2 状态轮询失败（本轮跳过，不误判失败; recover 超时兜底）: "
                + "; ".join(aria2_errors),
                duration_seconds=_time.monotonic() - t0,
            )
            await s.commit()


async def _after_complete_promote(media_id: int, episode: str, file_name: str) -> None:
    """downloading→scrape 推进后的统一动作（轮询与 aria2 回调共用，§6.2/§6.3）：

    - download_complete 通知（站内 + PushPlus，全体）；
    - 触发刮削执行器（scrape_runner：nastools_sync force=True，事件触发不阻塞；
      P3-3 持引用防 GC）。G6：**不删夸克**——入库确认（library 节点完成）后才删，
      由后续 lane 执行。
    """
    await notifier.notify(NotifyEvent(
        event_type=EVENT_DOWNLOAD_COMPLETE,
        title=f"下载完成: {file_name}",
        body=f"媒体 {media_id} · 集 {episode} · {file_name} 下载完成，已推送刮削；"
             f"夸克中转文件将在 Emby 入库确认后释放。",
        recipient=None,
        extra={"media_id": media_id, "episode": episode},
    ))
    try:
        _spawn(scrape_runner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 刮削执行器事件触发失败: %s", exc)


async def _complete_download(dq_id, media_id, episode, file_name, quark_path,
                             retry_snapshot, node_attempt_snapshot, *,
                             real_size: int | None = None) -> None:
    """下载完成推进（G6 决策 / §4.2）：downloading → scrape（触发刮削，不删夸克）。

    与旧三表版（双表 done + 删夸克）的差异：
      a. 单表条件更新 downloading→scrape（rowcount=0 → 已被并发方推进，幂等返回，
         不重复计数/通知——回调与轮询并发推进由条件更新兜底，§6.2）；
      b. 触发刮削执行器（_after_complete_promote：nastools force 同步 + 通知）；
      c. **不删夸克文件**（G6：入库确认后由后续 lane 删除）；
      d. media 离开 downloading 后检查是否还有其他进行中任务，无则回 tracking。

    real_size（Task 2）：aria2 tell_status 的 totalLength（真实字节数）。
    非 None 时回填 DownloadQueue.file_size 并清除 size_estimated 估算标记
    （size_estimated 列可空，但 file_size NOT NULL——real_size 缺失时必须跳过
    file_size 写入，避免把 NULL 写进 NOT NULL 列导致整条 UPDATE 失败）。
    """
    t0 = _time.monotonic()  # Q8①：真实耗时
    now = _now()
    async with async_session() as s:
        async with s.begin():
            vals = {
                "status": "scrape",
                "node_attempt": 0,
                "node_started_at": now,
                "node_finished_at": now,
                "node_error": None,
                "updated_at": now,
            }
            if real_size is not None:
                vals.update(file_size=real_size, size_estimated=False)
            r = await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id, DownloadQueue.status == "downloading")
                .values(**vals)
            )
            if r.rowcount == 0:
                # 幂等：已被 aria2 回调（trigger_download_complete）/并发轮询推进，
                # 或已被 recovery 回退——本轮不重复推进、不通知、不触发刮削。
                logger.debug("[transfer] 下载完成但 dq 已非 downloading（rowcount=0），幂等跳过")
                return
            await record_task_run(
                s, "transfer", "success", f"下载完成: {episode} ({file_name})", media_id,
                duration_seconds=_time.monotonic() - t0,
            )
            # P3-6：es 离开 downloading 后检查该 media 是否还有其他进行中任务，
            # 无则回 tracking（条件更新不覆盖 paused）。与推进同一事务。
            await _sync_media_status(media_id, s)

    # 通知 + 触发刮削 + 不删夸克（G6）
    await _after_complete_promote(media_id, episode, file_name)


async def _node_failure(dq_id, media_id, episode, file_name, retry_snapshot,
                        node_attempt_snapshot, reason, *, clear_save=True, clear_gid=False,
                        t0, notify_title="任务失败") -> str:
    """节点级失败回退（CAS 条件更新，§4.2）：非终态 → pending + 节点计数自增；终态 → failed。

    统一失败语义（转存失败 / 下载失败 / /quark 预检失败共用，L2 五节点状态机）：
      - node_attempt 快照 + 1（CAS 防并发丢增量，P2-5 协议：WHERE 含
        retry_count/node_attempt 旧值）；retry_count 同步自增（双写兼容）；
      - node_attempt < _RETRY_LIMIT → 非终态：status='pending'（排队重试，
        node_started_at 刷新、node_finished_at 清空）；
      - node_attempt ≥ _RETRY_LIMIT → 终态：status='failed' + flow_error 告警；
      - 失败诊断写入 node_error（node 是权威字段，错误必须写清楚原因）。

    clear_save：失败路径清空 save_task_id / save_attempt_at（P2-10/P0-1 防盲等——
    已受理未落盘时若不清空，下一轮会跳过 save 永远等不到文件，死循环到上限）。
    条件化（T8.7）：仅 status != 'transferring' 时清空——transferring 说明并发方已
    重新 save 并落库了新 task_id（save 落库 WHERE status='transferring'，不动
    retry_count/node_attempt，故本 CAS 仍命中），无条件清空会抹掉并发新 save；
    用 SQL case() 单语句内读 status 旧值判定（SET 表达式基于旧行求值，无 TOCTOU 间隙）。
    clear_gid：下载失败回退时清 aria2_gid（重新转存会重新 add_uri）。

    返回 'retry'（非终态回退）/'terminal_failed'（终态），供调用方决定续跑策略。
    """
    new_attempt = node_attempt_snapshot + 1
    terminal = new_attempt >= _RETRY_LIMIT
    now = _now()
    err = f"{reason}（node_attempt={new_attempt}/{_RETRY_LIMIT}）" if terminal else reason
    async with async_session() as s:
        async with s.begin():
            r = await s.execute(
                update(DownloadQueue)
                .where(
                    DownloadQueue.id == dq_id,
                    DownloadQueue.retry_count == retry_snapshot,          # P2-5 CAS
                    DownloadQueue.node_attempt == node_attempt_snapshot,  # L2 节点级 CAS
                )
                .values(
                    status="failed" if terminal else "pending",
                    node_attempt=new_attempt,
                    retry_count=new_attempt,
                    node_error=err,
                    error=err,
                    node_started_at=now if not terminal else DownloadQueue.node_started_at,
                    node_finished_at=now if terminal else None,
                    updated_at=now,
                    # T8.7：条件化清空——status 旧值仍为 'transferring' 说明并发方已重新
                    # save 并落库新 task_id，保留（case 读取 SET 前旧行值）；否则清空防盲等。
                    save_task_id=(
                        case(
                            (DownloadQueue.status == "transferring",
                             DownloadQueue.save_task_id),
                            else_=None,
                        )
                        if clear_save
                        else DownloadQueue.save_task_id
                    ),
                    save_attempt_at=(
                        case(
                            (DownloadQueue.status == "transferring",
                             DownloadQueue.save_attempt_at),
                            else_=None,
                        )
                        if clear_save
                        else DownloadQueue.save_attempt_at
                    ),
                    aria2_gid=None if clear_gid else DownloadQueue.aria2_gid,
                )
            )
            if r.rowcount == 0:
                # P2-5：CAS 冲突（recovery 并发已回退/已计数）→ 不重复计数、不转移状态；
                # 仍清 save 幂等标记（P0-1：残留会让下一轮跳过 save 盲等死循环）——
                # T8.7 条件化：仅 status != 'transferring' 时清（transferring = 并发方已
                # 重新 save 落库新 task_id，兜底分支同样不得抹掉）。
                await s.execute(
                    update(DownloadQueue).where(DownloadQueue.id == dq_id).values(
                        save_task_id=(
                            case(
                                (DownloadQueue.status == "transferring",
                                 DownloadQueue.save_task_id),
                                else_=None,
                            )
                            if clear_save
                            else DownloadQueue.save_task_id
                        ),
                        save_attempt_at=(
                            case(
                                (DownloadQueue.status == "transferring",
                                 DownloadQueue.save_attempt_at),
                                else_=None,
                            )
                            if clear_save
                            else DownloadQueue.save_attempt_at
                        ),
                    )
                )
                await record_task_run(
                    s, "transfer", "error",
                    f"{episode} 失败但 retry_count/node_attempt CAS 冲突（并发回退?），本轮跳过不计数: {reason}",
                    media_id,
                    duration_seconds=_time.monotonic() - t0,
                )
                return "retry"
            await record_task_run(
                s, "transfer", "error",
                f"{episode} {reason}（node_attempt={new_attempt}/{_RETRY_LIMIT}）", media_id,
                duration_seconds=_time.monotonic() - t0,
            )
            # P3-6：仅终态（转 failed）才回 tracking——非终态回退 pending 仍属进行中
            # （排队中），media 保持 downloading。与计数同一事务。
            if terminal:
                await _sync_media_status(media_id, s)

    if terminal:
        await notifier.notify(NotifyEvent(
            event_type=EVENT_FLOW_ERROR,
            title=notify_title,
            body=f"{err}；已重试 {new_attempt} 次达上限，任务标记 failed，请人工 retry。",
            recipient=None,
            extra={"media_id": media_id, "episode": episode},
        ))
    logger.warning(
        "[transfer] %s %s 失败 %s（node_attempt=%d/%d）%s",
        media_id, episode, reason, new_attempt, _RETRY_LIMIT,
        "转 failed" if terminal else "回退 pending",
    )
    return "terminal_failed" if terminal else "retry"


async def _fail_download(dq_id, media_id, episode, file_name, quark_path,
                         retry_snapshot, node_attempt_snapshot, reason) -> None:
    """确定性失败（aria2 error/removed）→ 节点级重试 / 终态 failed（§4.2）。

    回退前清理夸克残留（alist.remove，失败仅告警不阻断）——G6 仅移除「下载完成
    即删」，失败回退时文件可能不完整/损坏，清理仍是必要语义。
    """
    t0 = _time.monotonic()  # Q8①：真实耗时
    try:
        if quark_path:
            dir_part, names = _split_quark_path(quark_path)
            if names:
                await alist.remove(names, dir_part)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 失败回退前清理夸克残留失败: %s", exc)

    await _node_failure(
        dq_id, media_id, episode, file_name, retry_snapshot, node_attempt_snapshot,
        reason, clear_save=True, clear_gid=True, t0=t0, notify_title="任务失败",
    )


async def _refresh_progress(dq_id) -> None:
    """仍在下载（active/waiting/未知状态）→ 显式刷新 updated_at（防 recover 超时误回退）。

    P2-9：paused 不再刷新（由 recover 超时回退），故调用方只在非 paused 时调用。
    """
    now = _now()
    async with async_session() as s:
        async with s.begin():
            await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.id == dq_id, DownloadQueue.status == "downloading")
                .values(updated_at=now)
            )


async def _sync_media_status(media_id: int, session=None) -> int:
    """P3-6（council）：media 不再有任何进行中 download_queue → 条件回退 status='tracking'。

    进行中 = DownloadQueue.status in _ACTIVE_STATUSES（pending 排队中/quota_wait
    等待容量均算处理中；failed/done/skipped 不算）。条件更新 WHERE
    media.status='downloading'：不覆盖用户手动 paused，也不干扰其余状态；无匹配行
    （用户已 paused / 已非 downloading）返回 0 忽略。

    参数 session：传入时复用外部事务（由调用方统一提交，减少额外 session）；
    不传则自开短事务。返回回退 update 的行数（0 或 1）。
    """
    async def _run(s):
        active = (
            await s.scalar(
                select(func.count())
                .select_from(DownloadQueue)
                .where(
                    DownloadQueue.media_id == media_id,
                    DownloadQueue.status.in_(_ACTIVE_STATUSES),
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
# 阶段 B：容量预算并发准入（§5）
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
    t0 = _time.monotonic()  # Q8①：真实耗时
    # P2-2 TTL 清理（公共化）：冷却项停留超过 2 倍窗口即不可能再被命中——同 key
    # 距上次 notify 已超 2*窗口，之后任何触发必然走「新告警」分支（重新写时间戳），
    # 旧条目对节流判定无影响；遍历删除防 dict 随 media 删除/类别变化长期无界增长。
    # 每次入口 O(n) 清理一次；conftest 按测试边界 clear() 的重置语义不受影响。
    _now_m = _time.monotonic()
    for _key, (_ts, _b) in list(_alert_cooldown.items()):
        if _now_m - _ts > 2 * _ALERT_COOLDOWN_SECONDS:
            _alert_cooldown.pop(_key, None)
    logger.warning("[transfer] %s", message)
    async with async_session() as s:
        await record_task_run(s, "transfer", "error", message, media_id,
                              duration_seconds=_time.monotonic() - t0)
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


async def _read_reserved() -> int:
    """reserved 聚合：未落盘在途集合（transferring/scrape/library）file_size 求和（§5.1）。

    P1-4（议会裁决）收紧口径：**不含 downloading**——已落盘文件由容量 check 内层
    used（alist /quark 递归）覆盖，双重计算会假性容量不足；downloading 任务转存
    时已在 transferring 阶段计入预留，落盘后由 used 接管（预留随 transferring→
    downloading 转移自动释放，不重复占额）。
    reserved 唯一可信源 = DB 聚合（无独立账本）：任务抢占（pending→transferring）
    自动计入、离开集合（→done/failed/downloading/回退 pending）自动释放，无需记账。
    """
    async with async_session() as s:
        total = await s.scalar(
            select(func.coalesce(func.sum(DownloadQueue.file_size), 0)).where(
                DownloadQueue.status.in_(_INFLIGHT_STATUSES)
            )
        )
    # PG 下 SUM(NUMERIC) 返回 decimal.Decimal；统一转 int（字节计），否则下游
    # `reserved + file_size` → capacity.check() 里 float + Decimal TypeError
    return int(total or 0)


async def _preflight_quark_mount(dq_id, media_id, episode, file_name, retry_snapshot,
                                 node_attempt_snapshot, t0) -> str | None:
    """L5（oracle 决策）：/quark 挂载前置校验（容量门槛之前）。

    quark_default_folder 与 AList Quark 驱动 root_folder_id 不一致时，cloudSaver
    save 的文件会落盘夸克其他目录，/quark 永不可见——与其白等 _LINK_WAIT_TIMEOUT
    =300s 轮询失败，不如在转存前预检直接走失败路径快速计数（node_attempt++，
    ≤3 次即 failed 供人工修正配置）。
    match is False 或 configured_folder_id 为空 → 该任务直接失败（_node_failure，
    node_error 写精确诊断）；调用异常 → 仅告警不阻断（继续正常流程）。

    返回 None=通过；'retry'/'terminal_failed'=预检失败已计数（调用方据此停本批）。
    """
    try:
        diag = await alist.diagnose_quark_mount()
    except Exception as exc:  # noqa: BLE001  预检不可用（含旧服务缺该方法）→ 告警后继续
        logger.warning("[transfer] /quark 挂载预检不可用（仅告警，不阻断转存）: %s", exc)
        return None
    if diag is not None:
        configured = diag.get("configured_folder_id") or None
        root = diag.get("root_folder_id") or None
        if diag.get("match") is False or not configured:
            if not configured:
                reason = ("quark_default_folder 为空（folderId 配置缺失），转存前预检失败；"
                          "请配置为 AList Quark 驱动的 root_folder_id")
            else:
                reason = (f"quark_default_folder 配置与 AList root_folder_id 不一致："
                          f"configured={configured} vs root={root}，转存前预检失败；"
                          f"文件将落盘夸克其他目录，/quark 永不可见")
            # 失败路径（与转存失败同一节点级语义：node_attempt++，<3 排队重试 / ≥3 failed）
            return await _node_failure(
                dq_id, media_id, episode, file_name, retry_snapshot, node_attempt_snapshot,
                reason, clear_save=True, t0=t0, notify_title="转存配置校验失败",
            )
    return None


class _DownloadStateChanged(Exception):
    """P2-4（council）：成功路径单表 update 的 rowcount 校验未通过（转存链期间 dq 已被
    并发方变动——recovery 超时回退 / 人工 retry）时抛出的内部信号。

    必须用异常触发 `async with session.begin()` 的事务回滚：直接 return 会让上下文
    正常退出并 commit，把半边状态错误提交为 downloading。
    """


async def _commit_downloading(dq_id, media_id, episode, file_name, out_name, gid,
                              save_task_id, t0, quark_path: str | None = None) -> str:
    """addUri 成功 → CAS 落 downloading（aria2_gid / quark_path / local_path 落库）。

    条件更新 WHERE status='transferring'（rowcount 门控）：转存链（save → 落盘等待，
    最长 300s）期间 dq 可能已被并发方变动（recovery 超时回退 / 人工 retry）——
    命中 0 行 → raise _DownloadStateChanged 触发事务整体回滚（拒绝落 downloading），
    aria2.add_uri 已提交下行任务（gid 已签发）→ best-effort 清理防孤儿下载，
    失败仅告警不阻断（下一轮 job 仍会取件重试）。

    返回 'admitted'（成功）/'conflict'（状态已被并发方变动，主循环换下一个 pending）。
    """
    now = _now()
    # quark_path 由转存链传入（已含落盘后改名的新名）；缺省回退原始名（旧数据/异常）
    quark_path = quark_path or f"/quark/{file_name}"
    try:
        async with async_session() as s:
            async with s.begin():
                r = await s.execute(
                    update(DownloadQueue)
                    .where(DownloadQueue.id == dq_id, DownloadQueue.status == "transferring")
                    .values(
                        status="downloading",
                        aria2_gid=gid,
                        quark_path=quark_path,
                        local_path=f"/downloads/{out_name}",
                        node_attempt=0,          # 进入 downloading 节点重新计数
                        node_started_at=now,
                        node_finished_at=now,    # transfer 节点瞬时完成标记
                        node_error=None,         # 上一节点诊断不留
                        save_task_id=save_task_id or None,
                        # save_attempt_at 保留（P0-1：受理时间随任务存活，失败回退才清）
                        updated_at=now,
                    )
                )
                if r.rowcount != 1:
                    raise _DownloadStateChanged()
                await record_task_run(
                    s, "transfer", "success",
                    f"转存并提交 aria2 下载: {episode}（gid={gid}）", media_id,
                    duration_seconds=_time.monotonic() - t0,
                )
    except _DownloadStateChanged:
        # 事务已整体回滚（dq 未落 downloading）。aria2.add_uri 已提交下行任务
        # （gid 已签发）——best-effort 清理，防孤儿 aria2 下载继续占用带宽/空间；
        # 失败仅告警不阻断（下一轮 job 仍会取件重试）。
        try:
            await aria2.client.remove(gid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[transfer] 清理孤儿 aria2 任务失败 %s: %s", gid, exc)
        # design T5（fix-transfer-flow-reliability Task 8）：转存已落盘（且可能已被
        # _get_link_wait_visible 改名）——冲突回滚后夸克文件成残留（无 downloading 行
        # 指引后续清理），以 quark_path（final_quark_path）拆分调用 alist.remove
        # best-effort 清理，失败仅告警不阻断（后续转存重试会重新 save/覆盖）。
        try:
            dir_part, names = _split_quark_path(quark_path)
            if names:
                await alist.remove(names, dir_part)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[transfer] 清理夸克残留失败 %s: %s", quark_path, exc)
        async with async_session() as s:
            await record_task_run(
                s, "transfer", "error",
                f"{episode} 状态已变，清理孤儿 aria2 任务", media_id,
                duration_seconds=_time.monotonic() - t0,
            )
            await s.commit()
        return "conflict"

    # P1（议会验证 gamma）：download_started 通知（§6.3 通知时机清单：addUri 成功
    # 进入 downloading → download_started）。失败仅告警不阻断（通知通道异常不影响
    # 主流程，与 download_complete 通知同模式）。
    try:
        await notifier.notify(NotifyEvent(
            event_type=EVENT_DOWNLOAD_STARTED,
            title=f"下载开始: {out_name}",
            body=f"媒体 {media_id} · 集 {episode} · {out_name} 已提交 aria2 下载。",
            recipient=None,
            extra={"media_id": media_id, "episode": episode},
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] download_started 通知失败（不阻断主流程）: %s", exc)

    # design T7（fix-transfer-flow-reliability Task 10）：转存提交成功（文件已落盘
    # downloading）→ 立即使 30s 进程内 used 缓存失效，下一轮准入 re-count 反映真实
    # used（downloading 仍不计 reserved，防双计，消除容量记账漏计窗口）。
    try:
        capacity.provider.invalidate_usage_cache()
    except Exception as exc:  # noqa: BLE001  缓存失效失败不影响主流程
        logger.debug("[transfer] 容量缓存失效失败: %s", exc)
    return "admitted"


async def _transfer_chain(dq_id, media_id, episode, file_name, share_code, stoken,
                          fids, fid_tokens, folder_id, save_task_id, save_attempt_at,
                          download_name, quark_path, retry_snapshot, node_attempt_snapshot,
                          t0) -> str:
    """转存链路（§4.2 步骤）：cloudSaver save → 等落盘可见 → alist 直链 → aria2 addUri。

    - save 幂等（P2-10/P0-1）：dq.save_task_id 已存在（上一轮已受理此文件）则跳过
      save 直接等落盘/取直链；save 成功即把 task_id 持久化（WHERE status='transferring'
      条件更新），后续 get_link/add_uri 失败重试不再重复 save（防重复转存占空间）；
      **仅成功路径保持幂等**——失败回退路径清空 save_task_id（P0-1 防盲等）；
    - stale 兜底（P0-1）：save_task_id 存在但受理超 _SAVE_ATTEMPT_MAX_SECONDS（或
      时间为空）→ 强制清空重新 save（任何清空路径漏清也会超时自动恢复）；
    - P2（§7）/Task 7：aria2 out = dq.download_name——取件时该列为空，转存落盘
      可见/改名 前由 _ensure_download_name 按媒体信息生成并 CAS 落库（后置生成，
      重试幂等：已生成则跳过），缺失/无可格式化名回退原始名；
      comment = lumencloud:<media_id>:<episode>（GID 来源校验标记）；
    - 任一步失败 → _fail_transfer 节点级重试（清理夸克残留 + 计数 + 回退 pending/failed）。

    返回状态（供主循环决策）：'admitted' / 'retry' / 'terminal_failed' / 'conflict'。
    """
    # quark 最终路径：转存落盘后可能被 _get_link_wait_visible(rename_to=download_name)
    # 改名（用户需求：下载前 quark 即规范化集号命名），成功后此处切到新名；
    # 失败路径与提交落库均以最终名定位（清理/删除才删得到正确文件）。
    final_quark_path = quark_path
    try:
        # P0-1（council 兜底）：save_task_id 存在但受理超时（或该列为空）→ 视为 stale，
        # 先清 save_task_id 再走 save 分支，强制重新 save（杜绝「已受理未落盘」盲等）。
        if save_task_id and (
            save_attempt_at is None
            or (_now() - save_attempt_at).total_seconds() > _SAVE_ATTEMPT_MAX_SECONDS
        ):
            now_stale = _now()
            async with async_session() as s:
                async with s.begin():
                    await s.execute(
                        update(DownloadQueue)
                        .where(DownloadQueue.id == dq_id, DownloadQueue.status == "transferring")
                        .values(save_task_id=None, save_attempt_at=None, updated_at=now_stale)
                    )
            save_task_id = None
            save_attempt_at = None
            logger.warning(
                "[transfer] save_task_id 受理已超 %ds 或时间缺失，强制重新 save 防盲等",
                _SAVE_ATTEMPT_MAX_SECONDS,
            )
        if not save_task_id:
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
                # folderId 缺失时回退 QUARK_DEFAULT_FOLDER（实证：folderId 为空 → 不落盘 /quark）
                # Phase 8：改读 config_store（system_config 优先，env fallback，保存即生效）
                "folderId": folder_id_effective,
                "shareCode": share_code,
                "receiveCode": stoken,  # G4：receiveCode 语义 = stoken（非提取码）
            })
            save_task_id = _extract_save_task_id(save_res)
            if save_task_id:
                # save 一受理即落库（条件更新 WHERE status='transferring'），保证在
                # get_link / add_uri 之前 task_id 已可被重试读取。
                # P0-1：save_attempt_at 同时落库（=受理时间），供 stale 兜底判断。
                now_save = _now()
                async with async_session() as s:
                    async with s.begin():
                        await s.execute(
                            update(DownloadQueue)
                            .where(DownloadQueue.id == dq_id, DownloadQueue.status == "transferring")
                            .values(
                                save_task_id=save_task_id,
                                save_attempt_at=now_save,
                                updated_at=now_save,
                            )
                        )
        # Task 7（queue-flow-rework）：download_name 后置生成 + 幂等。
        # 取件时 DQ 行 download_name 为空（Task 4 不填），转存落盘可见/改名 前按
        # 媒体信息生成并 CAS 落库（WHERE status='transferring' 防重写）——重试时
        # download_name 已存在则跳过，不重复格式化/改名；_get_link_wait_visible
        # 用该名改 quark 文件，后续 aria2 out / quark_path / local_path 沿用，
        # 命名端到端一致（设计 D5）。
        if not download_name:
            download_name = await _ensure_download_name(
                dq_id, media_id, episode, file_name
            )
        link, final_quark_path = await _get_link_wait_visible(
            file_name, timeout=_LINK_WAIT_TIMEOUT, rename_to=download_name
        )
        # P2（§7）：out = 转存后生成的 download_name（格式化落盘名）；为 None
        # （CAS 未命中/无可格式化名）时回退原始名。quark 原文件已在转存落盘后被
        # 改名（_get_link_wait_visible rename_to），此处 out 与 quark 新名保持一致。
        out_name = download_name or file_name
        # T8.2：每次 add_uri 显式携带 allow-overwrite=true + auto-file-renaming=false。
        # 背景：下载重试时若 aria2 默认对已存在同名文件自动重命名（auto-file-renaming），
        # 落盘名与 dq.download_name 失配 → 完成态校验 / local_path 定位错位 → 重试必败。
        # options 与启动参数重复声明无害（RPC options 与命令行参数取并集语义）。
        gid = await aria2.client.add_uri(
            link,
            out=out_name,
            comment=f"{_COMMENT_PREFIX}{media_id}:{episode}",
            options={
                "allow-overwrite": "true",
                "auto-file-renaming": "false",
            },
        )
    except Exception as exc:  # noqa: BLE001
        # 任一步失败（含转存成功但直链/aria2 提交失败）→ 节点级重试路径（L2）；
        # 清理可能已转存的夸克残留（避免残留占用中转空间；以最终名定位——若已改名）。
        return await _fail_transfer(
            dq_id, media_id, episode, file_name, final_quark_path,
            retry_snapshot, node_attempt_snapshot, exc, t0,
        )
    return await _commit_downloading(dq_id, media_id, episode, file_name, out_name, gid,
                                     save_task_id, t0, quark_path=final_quark_path)


async def _fail_transfer(dq_id, media_id, episode, file_name, quark_path,
                         retry_snapshot, node_attempt_snapshot, exc, t0) -> str:
    """转存链失败（P2-10/P0-1 语义）：清理残留 + 节点级回退（pending/node_attempt++）。

    返回 _node_failure 的状态（'retry'/'terminal_failed'）。CAS 冲突分支由
    _node_failure 内部处理（不重复计数 + 按状态条件化清 save_task_id：非
    transferring 才清，防抹并发新 save——T8.7）。
    """
    try:
        dir_part, names = _split_quark_path(quark_path or f"/quark/{file_name}")
        if names:
            await alist.remove(names, dir_part)
    except Exception as e:  # noqa: BLE001  清理失败仅告警（P3-1），不阻断重试
        logger.warning("[transfer] 转存失败后清理夸克残留失败: %s", e)
    logger.warning(
        "[transfer] 转存失败 media=%s %s: %s", media_id, episode, exc,
    )
    return await _node_failure(
        dq_id, media_id, episode, file_name, retry_snapshot, node_attempt_snapshot,
        f"转存失败: {exc}", clear_save=True, t0=t0, notify_title="转存失败",
    )


async def _try_admit_one(t0) -> str:
    """取最早 pending 任务并完整执行转存链（一次准入一个，§4.2 阶段 B）。

    返回状态（主循环据此决定继续/停止）：
      - 'admitted'            ：已准入并走完转存链（成功或失败均已落库）
      - 'no_pending'          ：无 pending 任务（循环结束）
      - 'conflict'            ：取件后被并发方抢占（换下一个 pending 继续）
      - 'quota_wait'          ：容量不足（保持 pending + quota_reject_count++，本批停止）
      - 'capacity_unavailable'：容量数据不可用（fail-closed，本批停止）
      - 'retry'/'terminal_failed'：本任务失败已回退/终态（本批停止，等价原版
                                    一次处理一个后的续跑语义）

    原子性（§5.1「读 reserved + 记预留」）：
      - reserved 是 DB 实时聚合（无独立账本），「记预留」= CAS 抢占本身
        （pending→transferring 条件更新，原子）——同一任务绝不重复准入；
      - 准入段（P0-1/T1 三段式：短事务 A 取快照 → 锁外容量 check → 短事务 B
        锁行 + 重读 reserved + 复判 + CAS）由进程内 _admission_lock 串行化
        （单 worker 部署可靠）；
      - 事务级锁兜底：事务 B 内先对 system_config 锁行做写（SQLite 单写者下即持
        排他写锁，等价 BEGIN IMMEDIATE）再 SELECT SUM——多进程下准入段也串行，
        读到的 reserved 恒为最新已提交 in-flight；Postgres 部署需预置锁行，
        用 SELECT ... FOR UPDATE 同效（dialect 分支）；
      - 容量 check 在执行**无外层事务上下文**（短事务 A 已提交、事务 B 未开始），
        网络 IO 与快照落库不再嵌套进事务；锁外 check 只是预判，事务 B 内重读
        reserved 后以「usage 缓存快照 + reserved_新 + file_size ≤ quota」复判为
        最终判定（不重复调 get_usage，避免事务内网络 IO 回潮）。
    """
    # 1) 取最早 pending（enqueued_at, id 排序 FIFO）+ 快照全字段（防重权威源 = download_queue）
    async with async_session() as s:
        dq = (
            (
                await s.execute(
                    select(DownloadQueue)
                    .where(DownloadQueue.status == "pending")
                    .order_by(DownloadQueue.enqueued_at, DownloadQueue.id)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if dq is None:
            return "no_pending"
        dq_id, media_id, episode = dq.id, dq.media_id, dq.episode
        file_name = dq.file_name
        file_size = dq.file_size or 0
        share_code, stoken = dq.share_code, dq.stoken
        fids, fid_tokens, folder_id = dq.fids, dq.fid_tokens, dq.folder_id
        save_task_id, save_attempt_at = dq.save_task_id, dq.save_attempt_at
        download_name, quark_path = dq.download_name, dq.quark_path
        retry_snapshot = dq.retry_count or 0
        node_attempt_snapshot = dq.node_attempt or 0
        # P0-1（T1）：reserved 快照与 pending 快照同一短事务读取（已提交值，
        # detached），供锁外容量 check 预判（check 不再进入任何外层事务）。
        reserved_snapshot = await _read_reserved_in_tx(s)

    # 2) L5 /quark 挂载预检（容量门槛之前；失败 → 该任务节点级失败计数，本批停止）
    preflight = await _preflight_quark_mount(
        dq_id, media_id, episode, file_name, retry_snapshot, node_attempt_snapshot, t0,
    )
    if preflight is not None:
        return preflight

    # 3) 准入原子段（P0-1/T1 三段式）：锁外容量 check（网络 IO + 快照落库脱离
    #    外层事务上下文，杜绝事务内嵌套提交回潮）+ 短事务 B（锁行 + 重读 reserved
    #    + 内存 usage 快照复判 + CAS 抢占 / 置 quota_wait），仍由 _admission_lock
    #    串行化整段（锁外 check 只是预判，check 与 CAS 间的容量竞态由事务 B 内
    #    重读 reserved + 复判兜底）。
    conflict = False
    capacity_error: Exception | None = None
    capacity_ok = True
    final_capacity_ok = True
    quota_count = 0
    async with _admission_lock:
        now = _now()
        # 3a) 锁外容量预判：check(reserved_snapshot + file_size)（reserved 快照来自
        #     短事务 A，已提交值）；异常（CapacityUnavailable）→ fail-closed 锁外告警。
        #     同时取 usage 快照供事务 B 复判——get_usage 走 30s 进程内缓存（check
        #     内部复用同一缓存，不增加网络 IO 次数）；provider 缺 get_usage
        #     （如测试 fake）或调用异常时 usage 缺失 → 复判退化为信任锁外 check。
        usage_snap = None
        try:
            usage_snap = await capacity.provider.get_usage()
        except Exception as exc:  # noqa: BLE001  复判参数不可用 → 以锁外 check 为准
            logger.debug("[transfer] 容量复判参数不可用（依赖锁外 check 预判）: %s", exc)
        try:
            capacity_ok = await capacity.provider.check(reserved_snapshot + file_size)
        except Exception as exc:  # noqa: BLE001  CapacityUnavailable → fail-closed
            capacity_error = exc
        if capacity_error is None:
            # 3b) 短事务 B（写）：锁行 → 重读 reserved → 容量复判 → CAS/置 quota_wait
            async with async_session() as s:
                async with s.begin():
                    # 事务级锁：SQLite 单写者下「先写锁行」触发排他写锁（等价 BEGIN
                    # IMMEDIATE）→ 本事务内 SELECT SUM 读到最新已提交 in-flight，准入
                    # 段跨进程串行化；PG 多 worker 用锁行 FOR UPDATE（需预置该行）。
                    try:
                        if s.bind.dialect.name == "postgresql":
                            await s.execute(
                                select(SystemConfig)
                                .where(SystemConfig.key == _ADMISSION_LOCK_KEY)
                                .with_for_update()
                            )
                        else:
                            await s.execute(
                                update(SystemConfig)
                                .where(SystemConfig.key == _ADMISSION_LOCK_KEY)
                                .values(updated_at=now)
                            )
                    except Exception as exc:  # noqa: BLE001  锁行不可用 → 退回 CAS 兜底
                        logger.debug("[transfer] 准入锁行不可用（依赖 CAS 兜底）: %s", exc)
                    # 锁行后重读 reserved（读到最新已提交 in-flight；与锁外快照的
                    # 差额由复判兜底——防多进程下 check 与 CAS 间的容量竞态）
                    reserved_new = await _read_reserved_in_tx(s)
                    if capacity_ok:
                        # 容量复判（P0-1/T1 边界）：usage 缓存快照 + reserved_新 + 本集
                        # ≤ quota（锁外 check 只是预判，此处为最终判定；**不重复调用
                        # get_usage**，避免事务内网络 IO 回潮）。
                        # **故意不含 margin**（design T1 边界 L38 指定，与 check() 的
                        # used+candidate+margin≤quota 不同）：锁外 check 已含 margin，
                        # 复判只防御「check 与事务 B 之间 reserved 并发增大」这一竞态——
                        # check 的 margin 即该增大的容差；若 reserved 增量 ≤ margin，
                        # 复判（无 margin 的宽松公式）必然通过，且配额预算仍被 check
                        # 的严格公式覆盖。勿在此引入 margin 复刻 check，避免双计容差
                        # 导致准入偏严。
                        recheck_ok = True
                        if (
                            usage_snap is not None
                            and usage_snap.used_gb is not None
                            and usage_snap.total_gb
                        ):
                            recheck_ok = (
                                usage_snap.used_gb
                                + (reserved_new + file_size) / (1024 ** 3)
                                <= usage_snap.total_gb
                            )
                        if not recheck_ok:
                            final_capacity_ok = False
                    else:
                        final_capacity_ok = False
                    if final_capacity_ok:
                        # CAS 抢占：记预留 = 抢占本身（status→transferring 即计入
                        # in-flight 聚合）；wait_since 清除（P2-1）：准入成功 = 等待
                        # 结束，重置起点；唤醒/置回循环保留原起点仅让 >24h 告警覆盖
                        # 真实持续等待
                        r = await s.execute(
                            update(DownloadQueue)
                            .where(DownloadQueue.id == dq_id, DownloadQueue.status == "pending")
                            .values(status="transferring", node_started_at=now,
                                    wait_since=None, updated_at=now)
                        )
                        if r.rowcount != 1:
                            conflict = True  # 并发方已抢占（同一任务绝不被重复准入）
                        else:
                            # P3-6：media.status → downloading（条件更新不覆盖 paused）
                            await s.execute(
                                update(Media)
                                .where(Media.id == media_id, Media.status == "tracking")
                                .values(status="downloading", updated_at=now)
                            )
                    else:
                        # 容量不足（锁外预判 False）或复判不满足（reserved 并发增大）：
                        # 置 quota_wait 幽灵态（议会验证 P1-1 落地，§4.2）。
                        # 条件更新 status='pending'→'quota_wait' + quota_reject_count++
                        # （CAS 门控 rowcount 防并发）；quota_wait 后不再被取件命中
                        # （取件只认 pending），由 _admit_batch 入口的「释放唤醒」统一唤醒。
                        # wait_since 语义（P2-1 修复）：记录「首次进入等待」的起点——
                        # COALESCE(现有值, now)：首次置 now，后续唤醒-置回循环保留原起点，
                        # 使 >24h 告警基于真实持续等待时长触发（此前每轮清空/刷新永不达标）。
                        # 准入成功（pending→transferring）时才清除 wait_since。
                        # 绝不消耗 retry/node_attempt（§4.5）。
                        r_q = await s.execute(
                            update(DownloadQueue)
                            .where(DownloadQueue.id == dq_id, DownloadQueue.status == "pending")
                            .values(
                                status="quota_wait",
                                wait_since=func.coalesce(DownloadQueue.wait_since, now),
                                quota_reject_count=DownloadQueue.quota_reject_count + 1,
                                node_error="等待容量释放（已用+预留+本集超配额），置 quota_wait 排队（不消耗 node_attempt）",
                                updated_at=now,
                            )
                        )
                        if r_q.rowcount == 1:
                            quota_count = (
                                await s.scalar(
                                    select(DownloadQueue.quota_reject_count).where(DownloadQueue.id == dq_id)
                                )
                            ) or 0
    if conflict:
        return "conflict"
    if capacity_error is not None:
        await _record_alert(
            media_id, f"容量数据不可用，保持 pending（fail-closed）: {capacity_error}",
            category="capacity", bucket="capacity",
        )
        return "capacity_unavailable"
    if not final_capacity_ok:
        async with async_session() as s2:
            await record_task_run(
                s2, "transfer", "skipped", f"容量不足等待释放: {file_name}", media_id,
                duration_seconds=_time.monotonic() - t0,
            )
            await s2.commit()
        if quota_count >= _QUOTA_REJECT_ALERT_THRESHOLD:
            await _record_alert(
                media_id,
                f"容量不足已累计 {quota_count} 次，请人工检查夸克空间或配置: {file_name}",
                category="capacity", bucket="capacity",
            )
        return "quota_wait"

    # 4) 转存链（锁外长操作：save → 等落盘 → 直链 → addUri → 落 downloading/回退）。
    #    返回值透传：'admitted' / 'retry'（非终态回退）/'terminal_failed' / 'conflict'
    return await _transfer_chain(
        dq_id, media_id, episode, file_name, share_code, stoken, fids, fid_tokens,
        folder_id, save_task_id, save_attempt_at, download_name, quark_path,
        retry_snapshot, node_attempt_snapshot, t0,
    )


async def _read_reserved_in_tx(s) -> int:
    """在调用方事务内读 reserved 聚合（事务级锁已获取时读到最新已提交 in-flight）。"""
    total = await s.scalar(
        select(func.coalesce(func.sum(DownloadQueue.file_size), 0)).where(
            DownloadQueue.status.in_(_INFLIGHT_STATUSES)
        )
    )
    # 同 _read_reserved：PG 下 SUM 为 Decimal，统一转 int 防下游 float+Decimal
    return int(total or 0)


async def _fetch_from_task_queue(num: int = 10) -> int:
    """从 TaskQueue(ready) 按 (created_at, id) FIFO 取件生成 DownloadQueue(pending)。

    queue-flow-rework Task 4：下载队列从巡检队列取件（巡检只写 task_queue，
    DownloadQueue 由准入端按序取件生成，D2「取件源双轨」①）。

    取件契约（Task 2 review Imp#1 收敛 + Task 4 review R1 裁决 + T6 原子化）：
    - **只取 status='ready'**：pending/probing/error 无完整转存凭据绝不提升
      （防无凭据行 promote）；done 为源行终态不重复取件。
    - **同 (media_id, episode) 已有 DownloadQueue 行的任务直接 SQL 层排除**
      （NOT EXISTS，任意状态含 Task 2 前存量 promote 遗留 / 人工 skip 补写防重
      终态 / 并发取件产物；防重权威源 = download_queue UNIQUE(media_id, episode)）。
      被排除行**保持 ready、不置 done、不占 LIMIT num 名额**（FIFO 不饿死）；
      既有 DQ 行消失（入库删除/运维清理）后下轮自然补取，重试路径保留。
    - **单语句条件 INSERT 原子化（T6）**：逐行执行
      `INSERT INTO download_queue ... SELECT ... FROM task_queue WHERE id=? AND
      status='ready' AND NOT EXISTS (同键已有 DQ)`——同一条语句内同时完成「源行
      ready 校验 + 同键排除 + DQ 创建」。影响行数 1 → 同事务置源行 done（条件更新
      WHERE status='ready'）；影响行数 0（撞 UNIQUE 或源行已被并发取件）→ **不置
      done、保持 ready**（由下轮或并发路径处理）。取代旧的「CAS ready→done + 保存点
      INSERT」：CAS 置 done 与 DQ 创建原子一致，杜绝「保存点回滚但 done 已在事务
      提交、源行误标终态」的窗口（design T6，Task 9）。SQLite 与 Postgres 均支持
      该 INSERT...SELECT...WHERE NOT EXISTS 形态。
    - download_name 暂不填（Task 7 转存成功后置格式化，避免与分享原始名分叉）。
    - 返回本次生成的行数。
    """
    fetched = 0
    now = _now()
    async with async_session() as s:
        async with s.begin():
            rows = (
                await s.execute(
                    select(TaskQueue)
                    .where(
                        TaskQueue.status == "ready",
                        # SQL 层排除（review R1）：同键已有任意 DQ 行的 ready 任务
                        # 不进入取件批次（也不占 LIMIT num），源行保持 ready 等待
                        # 既有 DQ 消失后补取——不写终态、不消耗源行。
                        ~exists(
                            select(DownloadQueue.id).where(
                                DownloadQueue.media_id == TaskQueue.media_id,
                                DownloadQueue.episode == TaskQueue.episode,
                            )
                        ),
                    )
                    .order_by(TaskQueue.created_at.asc(), TaskQueue.id.asc())
                    .limit(num)
                )
            ).scalars().all()
            if not rows:
                return 0
            for r in rows:
                # T8.6（Task 16）：取件凭据完整性校验——ready 行凭据本应完整
                # （enqueue 探测收集），缺失即数据缺陷；不完整 → 不建注定失败的
                # DQ：保持源行 ready（不置 done、不建 DQ），_record_alert 告警留痕，
                # 由下轮重试或上游探测路径补全凭据。校验在前，不完整行根本不进入
                # 下方单语句 INSERT；SQL 层 coalesce 兜底（from_select 的 NOT NULL
                # 目标列）仅保证落库类型合法，不代表凭据可转存。
                if not (r.file_name and (r.file_size or 0) > 0 and r.share_code):
                    await _record_alert(
                        r.media_id,
                        f"task_queue id={r.id} media={r.media_id} episode={r.episode} "
                        f"转存凭据不完整（file_name/file_size/share_code 缺失），"
                        f"保持 ready 不建注定失败的 DQ",
                        category="transfer",
                    )
                    continue  # 不置 done、不建 DQ；下轮重试（或由上游探测路径补全凭据）
                # T6 原子化：单语句条件 INSERT（INSERT...SELECT...WHERE NOT EXISTS）在
                # 同一条语句内完成「源行 status='ready' 校验 + 同键 NOT EXISTS 排除 +
                # DQ 创建」——影响行数 1 → 同事务置源行 done；影响行数 0（撞 UNIQUE=
                # 其他 DQ 写入路径抢先落库 / 源行已被并发取件）→ 不置 done、保持 ready，
                # 由下轮取件或并发路径处理。取代旧「CAS ready→done + 保存点 INSERT」：
                # 消除保存点回滚但 done 已在外层事务提交、源行误标终态的窗口。
                # 拷贝 Task 2 快照字段 → DQ 同名字段（pwd_id 即设计文档的 pwd）。
                res = await s.execute(
                    insert(DownloadQueue)
                    .from_select(
                        [
                            DownloadQueue.media_id, DownloadQueue.episode,
                            DownloadQueue.task_queue_id, DownloadQueue.file_name,
                            DownloadQueue.file_size, DownloadQueue.size_estimated,
                            DownloadQueue.share_code, DownloadQueue.pwd_id,
                            DownloadQueue.stoken, DownloadQueue.receive_code,
                            DownloadQueue.fids, DownloadQueue.fid_tokens,
                            DownloadQueue.folder_id, DownloadQueue.status,
                            DownloadQueue.enqueued_at, DownloadQueue.updated_at,
                        ],
                        select(
                            # 列引用来自 task_queue（类型与列定义一致，SQLite/Postgres
                            # 均兼容）；FILE 三字段用 coalesce 保持原「空值兜底拷贝」语义
                            # （NOT NULL 目标列）；status/时间用常量绑定。
                            TaskQueue.media_id, TaskQueue.episode, TaskQueue.id,
                            func.coalesce(TaskQueue.file_name, ""),
                            func.coalesce(TaskQueue.file_size, 0),
                            TaskQueue.size_estimated,
                            func.coalesce(TaskQueue.share_code, ""),
                            TaskQueue.pwd_id, TaskQueue.stoken, TaskQueue.receive_code,
                            TaskQueue.fids, TaskQueue.fid_tokens, TaskQueue.folder_id,
                            literal("pending", type_=Text),
                            literal(now, type_=DateTime), literal(now, type_=DateTime),
                        ).where(
                            TaskQueue.id == r.id,
                            TaskQueue.status == "ready",
                            ~exists(
                                select(DownloadQueue.id).where(
                                    DownloadQueue.media_id == r.media_id,
                                    DownloadQueue.episode == r.episode,
                                )
                            ),
                        ),
                    )
                )
                if res.rowcount != 1:
                    # 影响 0 行：撞 UNIQUE（同键 DQ 已被其他路径抢先创建）或源行已被
                    # 并发取件（不再 ready）。不置 done、保持 ready——同键 DQ 由抢先
                    # 路径负责；源行保持 ready 由下轮取件或并发路径继续处理。
                    continue
                # 插入成功 → 同事务置源行 done（条件更新 WHERE status='ready'，保持
                # CAS 幂等语义），终态防重复取件，与 _enqueue 的 UNIQUE 捕获协同。
                await s.execute(
                    update(TaskQueue)
                    .where(TaskQueue.id == r.id, TaskQueue.status == "ready")
                    .values(status="done", updated_at=now)
                )
                fetched += 1
    return fetched


async def _admit_batch() -> None:
    """阶段 B：容量预算并发准入（§5.3）。

    入口时序（议会验证 P0/P1 裁决）：
      0. 读暂停开关（system_config download_queue_paused，直读 DB——queue.py 的
         pause/resume API 不刷新进程内缓存）→ 已暂停：记录 task_run(skipped,
         「队列已暂停，本轮不取新任务（在途任务继续完成）」) 并返回，**不取新待办、
         不唤醒 quota_wait**（暂停期间防反复写）；在途任务不受影响自然完成。
      0.5 释放唤醒 quota_wait → pending（单次消费入口统一唤醒全部，§4.2；唤醒前
         统计 wait_since 超 24h 行数，>0 发一次 flow_error 通知，_record_alert
         category="capacity" 沿用 P2-2 节流）；真正能准入多少由后续容量 check 把关。
      0.75 取件（queue-flow-rework Task 4）：TaskQueue(ready) 按 (created_at, id)
         FIFO 生成 DownloadQueue(pending)，源行同事务置 done（同键已有 DQ 行在
         SQL 层排除，见 _fetch_from_task_queue）——有 ready 任务先取件再准入
         （null pending 时若先空跑返回，ready 任务将永不取件，下载停摆）。
      1. 准入唯一约束 = 网盘容量（不再设并发数上限）：每轮准入数量 = 容量可容纳数；
         容量不足 → quota_wait 按网盘空间排队（空间释放后由下轮入口唤醒重试）。
      2. GID 来源校验（§12.2 简化版）整批一次：存在陌生 aria2 活动/等待任务 → 告警
         并跳过本轮（下轮续跑，防 n8n 误启动双转存；一过性陌生任务不造成整批停摆）。
      3. 准入循环内每任务走 _try_admit_one；容量不足/容量不可用/任务失败回退后停止
         本批（等价原版一次处理一个 + 续跑语义，下一轮 job/事件续跑）。
    """
    t0 = _time.monotonic()  # Q8①：真实耗时

    # 0) 暂停开关（P0 落地）：暂停 = 不取新 + 在途继续（§8.2）。直读 system_config
    #    （queue.py 的 _set_pause 不调 config_store.refresh，进程内缓存可能过期）。
    #    读配置失败 → 保守按未暂停继续，仅记录 warning（不因配置读失败停摆整个消费）。
    try:
        async with async_session() as s:
            row = await s.get(SystemConfig, _PAUSE_CONFIG_KEY)
        paused = as_bool(row.value) if row is not None else False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[transfer] 读取下载队列暂停开关失败（按未暂停继续）: %s", exc)
        paused = False
    if paused:
        # 纯空跑不写 task_run（每分钟高频噪音，前端已有暂停横幅 + 开关状态展示；
        # 保留服务日志供运维核对 job 存活）
        logger.info("[transfer] 队列已暂停，本轮不取新任务（在途任务继续完成）")
        return

    # 0.5) 释放唤醒 quota_wait → pending（P1 落地，§4.2）。仅未暂停时唤醒（暂停期间
    #     防反复写）。>24h 持续等待告警（_record_alert category="capacity" 节流）。
    now = _now()
    stale_quota_count = 0
    async with async_session() as s:
        async with s.begin():
            cutoff = now - timedelta(hours=_QUOTA_WAIT_ALERT_HOURS)
            stale_quota_count = (
                await s.scalar(
                    select(func.count()).select_from(DownloadQueue).where(
                        DownloadQueue.status == "quota_wait",
                        DownloadQueue.wait_since.isnot(None),
                        DownloadQueue.wait_since < cutoff,
                    )
                )
            ) or 0
            # 统一唤醒：quota_wait → pending（P2-1 修复：**不清 wait_since**——它记录
            # 首次进入等待的起点，唤醒不代表退出等待；仅准入成功抢占 transferring 时
            # 才清除。这样 >24h 告警能基于真实持续等待时长触发，且置回时 COALESCE
            # 保留原起点，杜绝 wait_since 每轮清空重计）。后续容量 check 决定准入。
            await s.execute(
                update(DownloadQueue)
                .where(DownloadQueue.status == "quota_wait")
                .values(status="pending", updated_at=now)
            )
    if stale_quota_count > 0:
        await _record_alert(
            None,
            f"容量不足已持续超过 {_QUOTA_WAIT_ALERT_HOURS} 小时：{stale_quota_count} 个任务在 quota_wait 等待中，"
            f"请人工检查夸克空间或配置",
            category="capacity", bucket="capacity",
        )

    # T8.5 容量预查（fix-transfer-flow-reliability Task 15）：唤醒 quota_wait 后先查
    # 容量余量，余量 ≤ 0 直接返回——不进入后续取件/GID 校验/准入循环。旧行为会在
    # 准入循环内对每行做容量 check、拒绝后置回 quota_wait：N×UPDATE + 容量查询的
    # 写放大（容量已满时每轮白做）。预查是优化不是新硬门：容量不可用（异常）→
    # 不 return，由准入循环内的 fail-closed 语义兜底（_try_admit_one 容量 check
    # 失败 → 保持 pending + 告警，见 _try_admit_one capacity_unavailable 分支）。
    try:
        usage = await capacity.provider.get_usage()
        quota_gb = await capacity.provider._load_quota_gb()
        margin_gb = await capacity.provider._load_margin_gb(quota_gb)
        if usage.used_gb is None:
            # 同 capacity.check 的 fail-closed：无 used_gb 视为容量不可用 → 交给准入循环兜底
            raise capacity.CapacityUnavailable("get_usage 返回 used_gb=None（预查视为容量不可用）")
        remaining_gb = max(0.0, quota_gb - usage.used_gb - margin_gb)
        if remaining_gb <= 0:
            logger.info("[transfer] 容量余量不足（%.2fG），唤醒后直接返回不进入准入循环", remaining_gb)
            return
    except Exception as exc:  # noqa: BLE001  容量不可用 → 交给准入循环 fail-closed 处理
        logger.debug("[transfer] 唤醒后容量预查失败（由准入循环 fail-closed 兜底）: %s", exc)

    # 1) 取件 → pending（queue-flow-rework Task 4，阶段 1 前置）：TaskQueue(ready)
    #    按 (created_at, id) FIFO 生成 DownloadQueue(pending)，源行同事务置 done
    #    （防重复取件）。先取件再查 pending：ready 任务先转 pending 再走准入——
    #    否则无 pending 时直接空跑，ready 任务永不取件（巡检已入队但下载停摆）。
    #    取件只认 status='ready'（pending/probing/error 无凭据不提升，裁决见 Task 2
    #    review Imp#1 收敛），跳过同键已有 DownloadQueue 行（防重权威源）。
    await _fetch_from_task_queue()

    # 1b) 无 pending 直接空跑（取件后的 pending 已计入；唤醒后的 quota_wait 已计入；
    #     不触发 GID 校验/预检）
    async with async_session() as s:
        has_pending = (
            await s.scalar(
                select(func.count()).select_from(DownloadQueue).where(DownloadQueue.status == "pending")
            )
        ) or 0
        if not has_pending:
            # 纯空跑不写 task_run（每分钟高频噪音；保留服务日志供运维核对 job 存活）
            logger.info("[transfer] 无 pending 任务待转存，本轮空跑")
            return

    # 2) GID 来源校验兜底（§12.2）：存在陌生 aria2 活动/等待任务 → 告警并跳过本轮
    #    （不处理、不 ++quota_reject_count；防 n8n 被误启动时的双转存）。陌生任务
    #    消失后下轮自动续跑——一过性外来任务不再造成整批永久停摆（queue-flow-rework
    #    Task 5 降级：由 fail-closed 改为「告警 + 本轮跳过 + 下轮续跑」）。
    #    P2-6（council）：合并校验 active + waiting 队列——waiting 中的陌生任务同样
    #    代表排队中的双转存，仅校验 active 会漏检；任一调用异常同样告警 + 跳过本轮。
    #    判定口径（2026-09 修订，oracle 评审）：不依赖 aria2 comment——实测 aria2
    #    1.36.0 静默丢弃 addUri 的 comment option（getOption/tellStatus 均读不到），
    #    comment 恒空会导致自家任务也被判陌生、转存永久停摆。改为 **DB gid 白名单**：
    #    aria2 活动/等待任务的 gid 必须在本系统 download_queue 已签发 gid 集合内
    #    （aria2_gid 非空全部行，不限 status）；不在集合 → 判陌生拦截。
    #    权威源 = DB（_commit_downloading 落库），版本无关，不依赖 aria2 行为。
    #    口径放宽（fix-transfer-flow-reliability Task 3）：recovery 回退 downloading→
    #    pending 时 aria2.remove 失败的场景下 gid 残留于 pending 行——若白名单只收
    #    status='downloading'，回退中/在库任务会被误判陌生并每轮整批跳过（自锁）；
    #    改为「aria2_gid 非空全部行」后不再误判（陌生判定仅对不在 DB 任何行的 gid）。
    try:
        actives = await aria2.client.tell_active() or []
        tell_waiting = getattr(aria2.client, "tell_waiting", None)
        if tell_waiting is not None:
            actives = actives + (await tell_waiting() or [])
    except Exception as exc:  # noqa: BLE001  Aria2Unavailable → 无法确认来源，告警 + 跳过本轮
        await _record_alert(
            None, f"aria2 状态不可用，本轮跳过转存（GID 校验失败，下轮续跑）: {exc}", category="gid",
        )
        return
    async with async_session() as s:
        known_gids = {
            g for (g,) in (
                await s.execute(
                    select(DownloadQueue.aria2_gid).where(
                        DownloadQueue.aria2_gid.isnot(None),
                    )
                )
            ).all()
        }
    for t in actives:
        gid = t.get("gid") or ""  # aria2 契约每项必有 gid；空/缺省按陌生计数（fail-closed 语义保持）
        if gid in known_gids:
            # 在库 gid（白名单命中）→ 自然清零计数，不拦截
            _unknown_gid_strikes.pop(gid, None)
            continue
        # 陌生 gid（不在 DB 任何行）：每轮 strikes += 1 + 告警 + 跳过本轮
        # （fail-closed 拦截保持，防 n8n 误启动双转存）；连续 _GID_STRIKE_LIMIT 轮
        # 未消失 → best-effort aria2.remove 清理孤儿任务（避免该 gid 永不消失时
        # 整批永久跳过自锁），随后仍跳过本轮，下轮 actives 不再含该 gid 自动恢复。
        strikes = _unknown_gid_strikes.get(gid, 0) + 1
        _unknown_gid_strikes[gid] = strikes
        if strikes >= _GID_STRIKE_LIMIT:
            _unknown_gid_strikes.pop(gid, None)   # 清计数防重复删除
            try:
                await aria2.client.remove(gid)
            except Exception as exc:  # noqa: BLE001  best-effort
                logger.warning("[transfer] 清理孤儿 aria2 任务失败 %s: %s", gid, exc)
            await _record_alert(
                None,
                f"检测到非本系统 aria2 任务 gid={gid}（连续 {_GID_STRIKE_LIMIT} 轮未在 DB 白名单），"
                f"已 best-effort 清理并告警，请人工确认 n8n 未误启动",
                category="gid",
            )
        else:
            await _record_alert(
                None,
                f"检测到非本系统 aria2 任务 gid={gid}（第 {strikes}/{_GID_STRIKE_LIMIT} 轮，暂跳过转存），"
                f"请人工确认 n8n 未误启动",
                category="gid",
            )
        return

    # 3) 准入循环：无可准入任务/资源受限时停止（准入唯一约束 = 网盘容量——容量不足
    #    置 quota_wait 按空间排队，由下一轮 job/事件续跑唤醒；无并发数上限）
    while True:
        result = await _try_admit_one(t0)
        if result == "no_pending":
            break
        # conflict（状态被并发方变动，含转存链期间的 recovery 回退）/ 资源受限 /
        # 任务失败回退 → 本批停止（与 P2-4 原版「本轮结束、下一轮 job/事件续跑」
        # 语义对齐；避免对同一回退任务自旋重试到无限循环）。
        if result in ("conflict", "quota_wait", "capacity_unavailable", "retry", "terminal_failed"):
            break


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def process_transfer_queue() -> None:
    """下载队列消费主流程（APScheduler job 与 scan 事件触发共用，§4.4）。

    阶段 A（downloading 完成轮询）+ 阶段 B（容量预算并发准入转存）依序执行
    （阶段 A 释放容量后阶段 B 的容量检查更准）。

    并发语义（§5.3）：不再持全局锁单任务串行——阶段 A 轮询与阶段 B 准入可被
    多路触发（scan 事件 / 手动 retry / 定时 job）并行执行；准入正确性由
    _try_admit_one 的「进程锁 + 事务级锁 + 行级 CAS」保证（不超容量、不重复准入）。
    """
    t0 = _time.monotonic()  # Q8①：真实耗时
    try:
        await _poll_downloading_tasks()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[transfer] 阶段A（完成轮询）异常")
        async with async_session() as s:
            await record_task_run(s, "transfer", "error", f"阶段A轮询异常: {exc}",
                                  duration_seconds=_time.monotonic() - t0)
            await s.commit()
    try:
        await _admit_batch()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[transfer] 阶段B（容量准入转存）异常")
        async with async_session() as s:
            await record_task_run(s, "transfer", "error", f"阶段B转存异常: {exc}",
                                  duration_seconds=_time.monotonic() - t0)
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


async def trigger_transfer_consume() -> None:
    """事件驱动下载队列消费触发（queue-flow-rework Task 6：事件触发下载队列消费）。

    调度一次「下载队列消费尝试」：内部执行 _admit_batch（step 0.5 释放唤醒全部
    quota_wait→pending、step 0.75/1 调 _fetch_from_task_queue 从 TaskQueue(ready) FIFO
    取件生成 DQ(pending)、step 3 有界准入循环——即设计文档 D3/D6 的「取件 + 有界
    准入」消费入口）。事件来源：scan 入队成功（经 trigger_transfer 委托）、入库完成 /
    容量释放（library_check._finalize_done）、手动 retry/promote。

    并发安全（§5.3）：进程内 _consume_trigger_lock 防重入——多路事件同时触发时同一
    时刻只允许一轮消费在跑；后到者直接跳过（正在跑的一轮已尽力取件并唤醒全部
    quota_wait，遗漏由每分钟 process_transfer_queue_job 兜底）。与每分钟 job / 手动
    重试的重叠并行仍由 _try_admit_one 的「进程锁 + 事务级锁 + 行级 CAS」保证正确
    （不超容量、不重复准入）。

    供调用方 fire-and-forget（_spawn / _background 强引用集合模式）；异常不外泄
    （内部记录，不阻塞调用方）。
    """
    if _consume_trigger_lock.locked():
        logger.info("[transfer] 已有消费轮进行中（事件触发防重入），本轮跳过")
        return
    async with _consume_trigger_lock:
        try:
            await _admit_batch()
        except Exception:  # noqa: BLE001
            logger.exception("[transfer] trigger_transfer_consume 事件消费异常")


async def trigger_transfer() -> None:
    """scan 入队成功后的事件触发（queue-flow-rework Task 6 起统一走互斥消费入口）。

    兼容壳：queue.py 的 _trigger_consume 与 scan._trigger_transfer 仍引用本函数，
    实际消费转调 trigger_transfer_consume（带 _consume_trigger_lock 防重入的
    _admit_batch 消费轮）；阶段 A（downloading 完成轮询）保留给每分钟
    process_transfer_queue_job 兜底。异常在 trigger_transfer_consume 内捕获，
    不阻塞调用方。
    """
    await trigger_transfer_consume()


async def trigger_download_complete(gid: str) -> bool:
    """aria2 下载完成回调推进（P6 端点延迟导入调用，签名冻结：async (gid) -> bool）。

    §6.2 回调链路：按 DownloadQueue.aria2_gid 反查 downloading 任务（comment 仅作
    GID 来源校验辅助，此处不校验）→ 条件更新 downloading→scrape（幂等：二次回调 /
    轮询已并发推进时 rowcount=0 → 返回 False，不重复推进/通知/触发刮削）→
    _after_complete_promote（通知 + 刮削执行器）→ 返回 True。

    内部 try/except 全包：任何异常（DB 故障/notifier 异常等）记录日志并返回 False，
    回调端点不会因内部异常抛 500（事件丢失由轮询兜底，§6.2）。
    """
    try:
        if not gid:
            return False
        now = _now()
        async with async_session() as s:
            async with s.begin():
                dq = (
                    await s.execute(
                        select(DownloadQueue).where(
                            DownloadQueue.aria2_gid == gid,
                            DownloadQueue.status == "downloading",
                        )
                    )
                ).scalars().first()
                if dq is None:
                    return False  # gid 查不到 / 已非 downloading（幂等）
                dq_id, media_id, episode, file_name = dq.id, dq.media_id, dq.episode, dq.file_name
                # Task 2：aria2 真实大小回填（回调路径与轮询对齐）。tell_status
                # 失败静默不回填（保持估算值，事件丢失由轮询兜底）；totalLength
                # 缺失/非法同样返回 None。
                real_size: int | None = None
                try:
                    st = await aria2.client.tell_status(gid)
                    real_size = int((st or {}).get("totalLength") or 0) or None
                except Exception:  # noqa: BLE001
                    pass  # aria2 查询失败 → 不回填，保持估算值
                vals = {
                    "status": "scrape",
                    "node_attempt": 0,
                    "node_started_at": now,
                    "node_finished_at": now,
                    "node_error": None,
                    "updated_at": now,
                }
                if real_size is not None:
                    vals.update(file_size=real_size, size_estimated=False)
                r = await s.execute(
                    update(DownloadQueue)
                    .where(DownloadQueue.id == dq_id, DownloadQueue.status == "downloading")
                    .values(**vals)
                )
                if r.rowcount != 1:
                    return False  # 已被轮询/回调并发推进（幂等）
                await record_task_run(
                    s, "transfer", "success",
                    f"下载完成（aria2 回调）: {episode} ({file_name})", media_id,
                    duration_seconds=0.0,
                )
                await _sync_media_status(media_id, s)
        # 通知 + 触发刮削（同轮询推进语义，G6 不删夸克）
        await _after_complete_promote(media_id, episode, file_name)
        logger.info("[transfer] trigger_download_complete 推进成功（gid=%s）", gid)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("[transfer] trigger_download_complete 异常（gid=%s）: %s", gid, exc)
        return False


# ---------------------------------------------------------------------------
# council 审查修复记录（P2-2 / P2-4 / P2-5 / P2-6 / P2-9 / P2-10 / P3-2 / P3-6）
# ---------------------------------------------------------------------------
# P2-2  ：flow_error 通知节流。_record_alert 按 (media_id, 告警类别) 在 10 分钟
#         （_ALERT_COOLDOWN_SECONDS）内去重：task_run(error) 每次记录，notify 仅在
#         「首次」或「消息变化（新根因）」时发出；每分钟兜底 job 重复触发同类告警
#         不再刷屏。类别：GID 校验失败 "gid" / 容量失败 "capacity" / 其他取消息前 40 字符。
# P2-4/P2-10：cloudsaver.save 幂等 + save_task_id 记录。DownloadQueue 的 save_task_id
#         列，save 一受理即落库；重试时若非空则跳过 save，直接 _get_link_wait_visible
#         等落盘/取直链，防重复转存。
# P2-5  ：retry_count 增量改 CAS 条件更新（WHERE 含 retry_count=读到的旧值），
#         替代「读-改-写」；recovery 并发回退不丢增量，CAS 未命中本轮跳过不计数。
# P2-6  ：GID 来源校验合并 active + waiting 队列（aria2.tell_waiting）；
#         waiting 中陌生任务同样阻断转存并告警。
# P2-9  ：paused 不再刷新 updated_at，由 recover_stale_tasks 按
#         episode_state_timeout_hours 超时回退 pending + 清理残留。
# P3-2  ：quota 拒绝累计告警阈值 _QUOTA_REJECT_ALERT_THRESHOLD=5。容量不足更新
#         后累计次数 ≥5 → flow_error 告警（category/bucket 均 "capacity"，与
#         "容量数据不可用"共享 P2-2 节流，10 分钟内同类只 notify 一次）。
# P3-6  ：media.status=downloading 写入者。准入抢占成功 → tracking→downloading
#         （WHERE status='tracking' 不覆盖 paused）；任务离开 in-flight（done/failed/
#         回退）后经 _sync_media_status 检查该 media 无任何进行中 download_queue
#         （pending/transferring/downloading/scrape/library/quota_wait）→ 回 tracking。
# ---------------------------------------------------------------------------
# L2/L4/L5/G6（oracle 决策，五节点任务模型）实施记录：
# - L2  ：DownloadQueue.status 成为执行状态机权威（pending→transferring→downloading
#         →scrape→library→done；failed/skipped 终态）。node_attempt 节点级重试计数
#         （<3 排队重试 / ≥3 failed，与 retry_count 平行自增）；node_started_at/
#         node_finished_at/node_error 节点级时间戳与精确失败诊断。
# - L4  ：_get_link_wait_visible 每轮先 list_dir("/quark")（refresh=True）拿真实目录，
#         精确/模糊（归一 + 去扩展名差异）匹配真实名再 get_link——根治原实现直接
#         get_link 吃缓存索引 + 文件名被夸克规范化改名导致「转存全失败」。
# - L5  ：准入前调用 alist.diagnose_quark_mount() 前置校验：match=False / configured
#         为空 → 该任务直接失败（node_error 精确诊断，不浪费 300s 轮询）；
#         调用异常仅告警不阻断。
# - G6  ：下载完成不再删夸克文件（_complete_download / trigger_download_complete 的
#         alist.remove 已移除）——入库确认（library 节点完成）后才删，由后续 lane 执行。
# - P5  ：旧三表（episode_state/transfer_queue/download_task）→ DownloadQueue 单表；
#         容量预算并发（§5，DB 聚合 reserved，准入唯一约束=容量）；trigger_download_complete
#         （§6.2 aria2 回调推进，幂等条件更新）。
# - 议会验证（P0/P1）：
#   - P0  暂停开关落地：_admit_batch 入口直读 system_config download_queue_paused
#         （queue.py 不刷新进程内缓存），true → skipped 记录 + 不取新/不唤醒。
#   - P1-1 quota_wait 幽灵态落地：容量不足 → status='quota_wait' + wait_since +
#         quota_reject_count++（CAS 门控）；消费入口统一唤醒回 pending；
#         >24h 持续等待 flow_error 告警（category="capacity" 节流）。
#   - P1-4 reserved 口径收紧：_INFLIGHT_STATUSES 不含 downloading（已落盘由 used
#         覆盖，防双重计算）；_ACTIVE_STATUSES（media 处理中判定）保持含 downloading。
#         准入无并发数上限（容量为唯一约束，空间不足 quota_wait 排队）。
#   - P1  gamma：_commit_downloading 成功发出 download_started 通知（§6.3）。
# ---------------------------------------------------------------------------

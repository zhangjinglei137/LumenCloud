"""
夸克中转空间容量判断（模型 B：硬上限，docs/新系统设计.md §4.4 / §6.2 / §6.3）。

阶段 1 实证结论（docs/阶段1验证报告.md Q1、Q2）：
    Q1: cloudSaver **无容量接口**（勿实现容量查询）
        → 容量数据源 = alist /quark 目录统计（list_dir 递归求和）
    Q2: 夸克为**硬上限**（164KB 落盘 vs 20.8G 被拒）→ **模型 B**
        used_gb + candidate_bytes + 安全余量 ≤ quota_gb 才允许转存

行为约定：
    get_usage(): 真实读取 alist /quark 目录占用（递归所有子目录），实时可用；
                 失败（alist 未配置 / 网络故障 / 全部目录统计失败）→ 抛
                 CapacityUnavailable（fail-closed，绝不用估算值放行）。
                成功后将快照写入 quark_capacity_log 归档（60s 节流，写库失败
                仅告警不影响主流程）。
    check(candidate_bytes): 模型 B 硬上限判定；实时容量不可用则抛
                 CapacityUnavailable，由调用方保持 pending + quota_reject_count++。
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import QuarkCapacityLog, SystemConfig
from app.services import alist, config_store
from app.services.notifier import EVENT_FLOW_ERROR, NotifyEvent, notifier

logger = logging.getLogger(__name__)

# alist 挂载的夸克中转空间根目录（Q1 结论：数据源 = alist /quark）
QUARK_ROOT = "/quark"

# 快照写库节流（秒）：60 秒内不重复写 quark_capacity_log，避免高频调用刷表
SNAPSHOT_THROTTLE_SECONDS = 60.0
_last_snapshot_written_at: float = 0.0

# get_usage 进程内缓存 TTL（秒）：短 TTL 缓存避免每个 pending 任务/每轮 job
# 都全量递归 alist /quark 目录树（P2-7）。fail-closed 语义不变：异常永不缓存。
USAGE_CACHE_TTL_SECONDS = 30.0

# system_config 中的安全余量键（缺省 = quota × 5%，docs §6.3）
SAFETY_MARGIN_CONFIG_KEY = "capacity_safety_margin_gb"

# system_config 中的配额键（「配置双源统一」：system_config 优先，env 仅 fallback）
QUOTA_CONFIG_KEY = "quark_quota_gb"

# ---- 阶段 4 生产化 / E：容量使用率阈值告警（交付 1）----
# system_config 中的使用率告警阈值键（值 = 0~1 浮点，如 0.90 表示 90%）
CAPACITY_ALERT_THRESHOLD_KEY = "capacity_alert_threshold"
# 阈值默认值：使用率 ≥ 90% 触发告警（settings.py PATCH 白名单可写该键覆盖）
CAPACITY_ALERT_THRESHOLD_DEFAULT = 0.90
# 去抖：需连续 N 条快照（quark_capacity_log 最近 2 条）都超阈值才告警，
# 避免瞬时抖动刷屏；去抖状态落在 DB 快照中，进程重启不丢
CAPACITY_ALERT_CONSECUTIVE = 2
# 告警冷却（秒）：30 分钟内不重复告警（模块级时间戳，思路同 transfer._alert_cooldown）
CAPACITY_ALERT_COOLDOWN_SECONDS = 1800.0

# 最近一次容量告警通知时间戳（monotonic；模块级共享状态，冷却用）
_last_capacity_alert_at: float = 0.0


async def _load_alert_threshold() -> float:
    """读取使用率告警阈值（system_config capacity_alert_threshold，默认 0.90）。

    模块级函数（M2，Oracle Gate3）：check_capacity_alert 与 CapacityProvider 同处
    本模块，不依赖 provider 实例（provider 为单例，且该读取与容量判断无关）。
    读取/解析失败均回退默认值（阈值是运维告警参数，不影响 fail-closed 主路径）。
    """
    default = CAPACITY_ALERT_THRESHOLD_DEFAULT
    try:
        async with async_session() as session:
            row = await session.get(SystemConfig, CAPACITY_ALERT_THRESHOLD_KEY)
    except Exception as exc:
        logger.warning("读取 %s 失败，用默认阈值 %.2f: %s",
                       CAPACITY_ALERT_THRESHOLD_KEY, default, exc)
        return default
    if row is None or not row.value:
        return default
    try:
        return float(row.value)
    except (TypeError, ValueError):
        logger.warning("%s 非数值 %r，用默认阈值 %.2f",
                       CAPACITY_ALERT_THRESHOLD_KEY, row.value, default)
        return default


def _now_naive_utc() -> datetime:
    """统一时间源（naive UTC，与 tasks 模块 _now 保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class CapacityInfo:
    """容量快照（写入 quark_capacity_log 归档用，实时判断见 check）。"""

    total_gb: float
    used_gb: Optional[float]
    source: str  # cloudsaver / alist / estimated
    checked_at: Optional[datetime] = None


class CapacityUnavailable(Exception):
    """容量数据不可用（实时容量读取失败），调用方须 fail-closed。"""


class CapacityProvider:
    """容量判断提供者（封装 get_usage + check，模型 A/B 可切换避免返工）。

    get_usage 数据源 = alist /quark 目录统计（递归所有子目录）：
      - list_dir 已把目录项 size 置 0，dir 项需继续递归 list_dir；
      - 单个目录 list_dir 失败 → 跳过该目录继续统计其余；
      - 全部目录统计失败（含根目录）→ 抛 CapacityUnavailable（fail-closed）。
    """

    def __init__(self) -> None:
        # env 默认配额（pydantic-settings，.env QUARK_QUOTA_GB）；system_config 优先，
        # 运行时经 _load_quota_gb() 读取覆盖（「配置双源统一」约定）。
        # Phase 8 配置入库：此处保留 env 作为 fallback 默认（config_store 尚未加载时
        # 语义一致），生效配额仍以 _load_quota_gb 的 system_config 读取为准（不重复读）。
        self._fallback_quota_gb: float = settings.QUARK_QUOTA_GB
        # 保留 _quota_gb 供既有引用/测试读取（值 = env fallback）
        self._quota_gb: float = self._fallback_quota_gb
        # P2-7：进程内短 TTL 用量缓存（命中直接返回，不再递归 alist / 写快照）
        self._usage_cache: Optional[CapacityInfo] = None
        self._usage_cached_at: float = 0.0
        logger.debug(
            "CapacityProvider 初始化：数据源=alist %s，quota=%.2f G（模型 B 硬上限，"
            "system_config quark_quota_gb 优先）",
            QUARK_ROOT,
            self._quota_gb,
        )

    async def get_usage(self) -> CapacityInfo:
        """获取容量快照（实时：alist /quark 目录统计，P2-7 进程内 30s 缓存）。

        失败（alist 未配置 / 网络故障 / 全部目录统计失败）→ 抛 CapacityUnavailable，
        调用方须 fail-closed（绝不用估算值放行转存）。异常不缓存。
        """
        # P2-7：缓存命中且未过期 → 直接返回（不再递归 alist、不再写快照）
        if self._usage_cache is not None and (
            time.monotonic() - self._usage_cached_at < USAGE_CACHE_TTL_SECONDS
        ):
            return self._usage_cache

        # Phase 8 配置入库：alist 凭据 DB 优先、env fallback（每次调用读最新值）
        if (
            not config_store.get("alist_base_url", settings.ALIST_BASE_URL)
            or not config_store.get("alist_token", settings.ALIST_TOKEN)
        ):
            raise CapacityUnavailable("AList 未配置（ALIST_BASE_URL/ALIST_TOKEN），容量不可用")

        try:
            used_bytes, any_ok = await self._total_used_bytes(QUARK_ROOT)
        except Exception as exc:  # alist 网络/响应异常等 → 归一为 CapacityUnavailable
            logger.warning("容量统计 %s 失败: %s", QUARK_ROOT, exc)
            raise CapacityUnavailable(f"alist 容量统计失败: {exc}") from exc

        if not any_ok:
            raise CapacityUnavailable("alist 目录统计全部失败，容量不可用（fail-closed）")

        quota_gb = await self._load_quota_gb()
        used_gb = used_bytes / (1024 ** 3)
        snap = CapacityInfo(
            total_gb=quota_gb,
            used_gb=used_gb,
            source="alist",
            checked_at=_now_naive_utc(),
        )
        await self._persist_snapshot(snap)
        # 成功路径（含快照归档）后刷新缓存；异常路径永不写入缓存
        self._usage_cache = snap
        self._usage_cached_at = time.monotonic()
        return snap

    async def _total_used_bytes(self, root: str) -> tuple[float, bool]:
        """递归统计 root 下所有文件 size（字节）。

        - 单目录 list_dir 失败 → log 跳过，继续统计其余（部分结果可用）；
        - 返回 (used_bytes, any_ok)，any_ok=False 表示一个目录都没成功（含根目录失败）。
        """
        used = 0.0
        stack = [root]
        ok_count = 0
        while stack:
            path = stack.pop()
            try:
                entries = await alist.list_dir(path)
            except Exception as exc:  # AlistUnavailable 或其子类
                logger.warning("容量统计: 列出 %s 失败，跳过该目录: %s", path, exc)
                continue
            ok_count += 1
            for entry in entries or []:
                if entry.get("is_dir"):
                    name = entry.get("name")
                    if name:
                        stack.append(f"{path.rstrip('/')}/{name}")
                else:
                    used += float(entry.get("size") or 0)
        return used, ok_count > 0

    async def _persist_snapshot(self, snap: CapacityInfo) -> None:
        """写 quark_capacity_log 归档快照（60s 节流；写库失败仅告警不抛）。

        快照归档不影响主流程（容量判断以实时 get_usage 为准），因此写库异常
        只 log warning。测试可 monkeypatch 此方法避免依赖真实数据库。
        """
        global _last_snapshot_written_at
        if time.monotonic() - _last_snapshot_written_at < SNAPSHOT_THROTTLE_SECONDS:
            return
        try:
            async with async_session() as session:
                session.add(
                    QuarkCapacityLog(
                        total_gb=snap.total_gb,
                        used_gb=snap.used_gb,
                        source=snap.source,
                        checked_at=snap.checked_at,
                    )
                )
                await session.commit()
            _last_snapshot_written_at = time.monotonic()
        except Exception as exc:
            logger.warning("容量快照写入 quark_capacity_log 失败（不影响主流程）: %s", exc)

    async def _load_quota_gb(self) -> float:
        """读取配额（system_config quark_quota_gb 优先，env 仅 fallback 默认）。

        「配置双源统一」约定：settings.py PATCH 白名单可写 quark_quota_gb，
        运行时此处读 system_config；缺失/非法/异常均回退 env 默认，不抛。
        """
        default = self._fallback_quota_gb
        try:
            async with async_session() as session:
                row = await session.get(SystemConfig, QUOTA_CONFIG_KEY)
        except Exception as exc:
            logger.warning("读取 %s 失败，用 env 默认配额 %.2fG: %s",
                           QUOTA_CONFIG_KEY, default, exc)
            return default
        if row is None or not row.value:
            return default
        try:
            return float(row.value)
        except (TypeError, ValueError):
            logger.warning("%s 非数值 %r，用 env 默认配额 %.2fG",
                           QUOTA_CONFIG_KEY, row.value, default)
            return default

    async def _load_margin_gb(self, quota_gb: Optional[float] = None) -> float:
        """读取安全余量（system_config capacity_safety_margin_gb，缺省 = quota × 5%）。

        读取/解析失败均回退默认值（余量不影响 fail-closed 主路径）。
        quota_gb 可选：缺省用 self._quota_gb（env fallback），check 传入刚读到的
        system_config 实际配额，保证「默认 = quota × 5%」与生效配额一致。
        """
        default = (quota_gb if quota_gb is not None else self._quota_gb) * 0.05
        try:
            async with async_session() as session:
                row = await session.get(SystemConfig, SAFETY_MARGIN_CONFIG_KEY)
        except Exception as exc:
            logger.warning("读取 %s 失败，用默认余量 %.2fG: %s",
                           SAFETY_MARGIN_CONFIG_KEY, default, exc)
            return default
        if row is None or not row.value:
            return default
        try:
            return float(row.value)
        except (TypeError, ValueError):
            logger.warning("%s 非数值 %r，用默认余量 %.2fG",
                           SAFETY_MARGIN_CONFIG_KEY, row.value, default)
            return default

    # 阶段 4 生产化 / E：使用率告警阈值读取（交付 1）
    async def _load_alert_threshold(self) -> float:
        """读取使用率告警阈值（system_config capacity_alert_threshold，默认 0.90）。

        读取/解析失败均回退默认值（阈值是运维告警参数，不影响 fail-closed 主路径）。
        """
        return await _load_alert_threshold()

    async def check(self, candidate_bytes: int) -> bool:
        """模型 B（硬上限，docs §6.3）判定候选转存是否放行。

        公式: used_gb + candidate_bytes/1024**3 + 安全余量 ≤ quota_gb → True
        实时容量不可用 → 抛 CapacityUnavailable（调用方保持 pending + quota_reject_count++，
        绝不用估算值放行）。
        """
        usage = await self.get_usage()
        if usage.used_gb is None:
            # 理论不可达（get_usage 成功必有 used_gb），fail-closed
            logger.warning("容量检查: get_usage 返回 used_gb=None，fail-closed 不放行")
            return False

        quota_gb = await self._load_quota_gb()
        margin_gb = await self._load_margin_gb(quota_gb)
        candidate_gb = candidate_bytes / (1024 ** 3)
        needed_gb = usage.used_gb + candidate_gb + margin_gb
        allow = needed_gb <= quota_gb
        if not allow:
            logger.warning(
                "容量不足（模型 B）: used=%.2fG + candidate=%.2fG + margin=%.2fG = %.2fG > quota=%.2fG",
                usage.used_gb, candidate_gb, margin_gb, needed_gb, quota_gb,
            )
        return allow


# 阶段 4 生产化 / E：容量使用率阈值告警巡检（交付 1）
async def check_capacity_alert() -> bool:
    """容量使用率阈值告警巡检（由 scheduler job `capacity_alert` 每小时调用）。

    读 quark_capacity_log 最近 2 条快照（checked_at DESC）：
      - 两条均 used_gb/total_gb ≥ 阈值（system_config capacity_alert_threshold，
        默认 0.90）→ **连续 2 次超阈值**（去抖，防瞬时抖动刷屏）；
      - 满足去抖且距上次告警 ≥ 30min（模块级 _last_capacity_alert_at）→ 发一条
        flow_error 通知（复用 notifier 事件契约，title「夸克容量使用率过高」）。

    方案权衡（注释存档，供追溯）：
      - **不在 transfer 高频 check()/get_usage() 路径内做判定**（check 每分钟多次，
        高频路径只做容量判断，不掺 DB 读 + 通知判定）；
      - 独立巡检入口 + 每小时 job 驱动：job 主动 get_usage() 已保证快照定期落库
        （Q6 容量趋势连续），每小时评估一次对运维告警足够。
      - 去抖状态落在 quark_capacity_log（非内存）：任一条快照低于阈值即不再满足
        「连续 2 次」→ 回落自动重置，进程重启不丢去抖进度。冷却 30min 用模块级
        时间戳（同 transfer._alert_cooldown 思路）。

    返回：本次是否发送了容量告警通知（True=已通知）。
    快照不足 / 数据缺失 / 读取异常 → False（fail-safe：宁缺毋滥，不误报）。
    """
    threshold = await provider._load_alert_threshold()
    try:
        async with async_session() as session:
            rows = (
                (
                    await session.execute(
                        select(QuarkCapacityLog)
                        .where(QuarkCapacityLog.checked_at.isnot(None))
                        .order_by(
                            QuarkCapacityLog.checked_at.desc(),
                            QuarkCapacityLog.id.desc(),
                        )
                        .limit(CAPACITY_ALERT_CONSECUTIVE)
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:
        logger.warning("容量告警评估：读取快照失败，本轮跳过（不误报）: %s", exc)
        return False

    rates = [
        row.used_gb / row.total_gb
        for row in rows
        if row.total_gb and row.used_gb is not None
    ]
    if len(rates) < CAPACITY_ALERT_CONSECUTIVE:
        # 快照不足（表空 / 记录太少 / 含缺失数据）→ 不判定（幂等：下轮补齐后自然判定）
        logger.debug("容量告警评估：快照不足 %d/%d，跳过", len(rates), CAPACITY_ALERT_CONSECUTIVE)
        return False
    if not all(rate >= threshold for rate in rates):
        # 回落：最近快照已低于阈值（未连续超阈值）→ 重置去抖，等待重新累计
        logger.debug("容量告警评估：未连续 %d 次超阈值，不告警", CAPACITY_ALERT_CONSECUTIVE)
        return False

    global _last_capacity_alert_at
    now = time.monotonic()
    if now - _last_capacity_alert_at < CAPACITY_ALERT_COOLDOWN_SECONDS:
        logger.info(
            "容量使用率告警冷却中（%.0fs 内不重复通知）", CAPACITY_ALERT_COOLDOWN_SECONDS,
        )
        return False

    latest = rows[0]  # 最新快照（checked_at DESC 首位）
    rate_pct = rates[0] * 100
    await notifier.notify(NotifyEvent(
        event_type=EVENT_FLOW_ERROR,
        title="夸克容量使用率过高",
        body=(
            f"夸克中转空间使用率 {rate_pct:.1f}%（used {latest.used_gb:.2f}G / "
            f"total {latest.total_gb:.2f}G），连续 {CAPACITY_ALERT_CONSECUTIVE} 次快照"
            f"≥ 阈值 {threshold:.0%}，请及时清理或扩容。"
        ),
        recipient=None,
        extra={"source": latest.source, "checked_at": str(latest.checked_at)},
    ))
    _last_capacity_alert_at = time.monotonic()
    logger.warning(
        "夸克容量使用率过高告警: %.1f%%（used %.2fG / total %.2fG，阈值 %.0f%%）",
        rate_pct, latest.used_gb, latest.total_gb, threshold * 100,
    )
    return True


# 模块级单例
provider = CapacityProvider()
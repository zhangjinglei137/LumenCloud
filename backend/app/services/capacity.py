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

from app.config import settings
from app.database import async_session
from app.models import QuarkCapacityLog, SystemConfig
from app.services import alist

logger = logging.getLogger(__name__)

# alist 挂载的夸克中转空间根目录（Q1 结论：数据源 = alist /quark）
QUARK_ROOT = "/quark"

# 快照写库节流（秒）：60 秒内不重复写 quark_capacity_log，避免高频调用刷表
SNAPSHOT_THROTTLE_SECONDS = 60.0
_last_snapshot_written_at: float = 0.0

# system_config 中的安全余量键（缺省 = quota × 5%，docs §6.3）
SAFETY_MARGIN_CONFIG_KEY = "capacity_safety_margin_gb"


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
        self._quota_gb: float = settings.QUARK_QUOTA_GB
        logger.debug(
            "CapacityProvider 初始化：数据源=alist %s，quota=%.2f G（模型 B 硬上限）",
            QUARK_ROOT,
            self._quota_gb,
        )

    async def get_usage(self) -> CapacityInfo:
        """获取容量快照（实时：alist /quark 目录统计）。

        失败（alist 未配置 / 网络故障 / 全部目录统计失败）→ 抛 CapacityUnavailable，
        调用方须 fail-closed（绝不用估算值放行转存）。
        """
        if not settings.ALIST_BASE_URL or not settings.ALIST_TOKEN:
            raise CapacityUnavailable("AList 未配置（ALIST_BASE_URL/ALIST_TOKEN），容量不可用")

        try:
            used_bytes, any_ok = await self._total_used_bytes(QUARK_ROOT)
        except Exception as exc:  # alist 网络/响应异常等 → 归一为 CapacityUnavailable
            logger.warning("容量统计 %s 失败: %s", QUARK_ROOT, exc)
            raise CapacityUnavailable(f"alist 容量统计失败: {exc}") from exc

        if not any_ok:
            raise CapacityUnavailable("alist 目录统计全部失败，容量不可用（fail-closed）")

        used_gb = used_bytes / (1024 ** 3)
        snap = CapacityInfo(
            total_gb=self._quota_gb,
            used_gb=used_gb,
            source="alist",
            checked_at=_now_naive_utc(),
        )
        await self._persist_snapshot(snap)
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

    async def _load_margin_gb(self) -> float:
        """读取安全余量（system_config capacity_safety_margin_gb，缺省 = quota × 5%）。

        读取/解析失败均回退默认值（余量不影响 fail-closed 主路径）。
        """
        default = self._quota_gb * 0.05
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

        margin_gb = await self._load_margin_gb()
        candidate_gb = candidate_bytes / (1024 ** 3)
        needed_gb = usage.used_gb + candidate_gb + margin_gb
        allow = needed_gb <= self._quota_gb
        if not allow:
            logger.warning(
                "容量不足（模型 B）: used=%.2fG + candidate=%.2fG + margin=%.2fG = %.2fG > quota=%.2fG",
                usage.used_gb, candidate_gb, margin_gb, needed_gb, self._quota_gb,
            )
        return allow


# 模块级单例
provider = CapacityProvider()
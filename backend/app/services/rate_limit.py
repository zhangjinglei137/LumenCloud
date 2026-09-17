"""通用进程内失败限流器（Task B2 / Design D3）。

抽象 auth.py 既有登录限流为可复用组件：窗口滑动内某键失败计数达到上限即
拒绝，窗口过期自动恢复，成功后 reset 清零。

现状假设：单 worker 部署（uvicorn --workers 1）下模块级 dict 读写原子性
足够，不引入锁/外部存储（与 auth.py 既有注释一致；多 worker 需 Redis 等
共享存储，属后续演进，不在本任务范围）。
"""
import logging
from collections import deque
from time import monotonic

logger = logging.getLogger(__name__)

# 键数上限（复用既有 _LOGIN_FAIL_MAX_KEYS 式上限）：恶意海量不同键（如伪造
# 用户名/来源 IP）防内存膨胀——超限清空整表并告警（简单方案，可接受，注释
# 同 auth.py 既有说明）。
DEFAULT_MAX_KEYS = 10000


class RateLimiter:
    """窗口滑动失败计数限流器。

    语义（与既有登录限流「先判后记」保持一致）：
    - hit(key)：记录一次失败（仅保留窗口内时间戳，惰性清理过期）；
    - check(key)：True=未超限可继续，False=窗口内失败数 >= max_failures 应拒绝；
    - reset(key)：成功后清除该键计数。

    防内存膨胀双层：
    - 键数超 max_keys → 清空重建并告警（_LOGIN_FAIL_MAX_KEYS 式上限）；
    - 单键 deque 以 maxlen=max_failures 封顶——超阈值后继续 hit 不增长
      （窗口内计数天然封顶在阈值，check 恒拒绝，语义不变）。
    """

    def __init__(
        self,
        max_failures: int,
        window_seconds: float,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures 必须 >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须 > 0")
        self.max_failures = int(max_failures)
        self.window_seconds = float(window_seconds)
        self._max_keys = max_keys
        self._failures: dict[str, deque] = {}

    def _prune(self, key: str, dq: deque, now: float) -> None:
        """移除窗口外的时间戳（惰性清理，check/hit 时顺带）。"""
        cutoff = now - self.window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()

    def hit(self, key: str) -> None:
        """记录一次失败事件。"""
        now = monotonic()
        if key not in self._failures and len(self._failures) >= self._max_keys:
            logger.warning("失败限流表超 %d 键，清空重建", self._max_keys)
            self._failures.clear()
        dq = self._failures.setdefault(key, deque(maxlen=self.max_failures))
        self._prune(key, dq, now)
        dq.append(now)

    def check(self, key: str) -> bool:
        """True=未超限（可继续）；False=窗口内失败数已达上限（应拒绝 429）。"""
        dq = self._failures.get(key)
        if dq is None:
            return True
        self._prune(key, dq, monotonic())
        if not dq:
            # 窗口全部过期 → 移除键防残留
            self._failures.pop(key, None)
            return True
        return len(dq) < self.max_failures

    def reset(self, key: str) -> None:
        """成功后清除该键（成功即清零的温和策略）。"""
        self._failures.pop(key, None)
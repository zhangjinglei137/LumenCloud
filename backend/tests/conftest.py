"""确保 backend 目录在 sys.path，pytest 可导入 app.*。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_transfer_alert_cooldown():
    """每个测试前重置 transfer 模块的 flow_error 通知节流表（P2-2/P3-2）。

    module 级节流表 `app.tasks.transfer._alert_cooldown` 是进程级共享状态；
    单测以独立 in-memory DB 模拟「互不相干的业务场景」，但 media_id 均从 1
    自增，多个测试会在 10 分钟真实时间窗口内命中同一 (media_id, category)
    节流 key——不重置会互相吞掉断言依赖的 flow_error 通知。
    重置仅发生在测试边界，不影响产品中每分钟 job 的节流语义。
    """
    from app.tasks import transfer as transfer_mod

    transfer_mod._alert_cooldown.clear()
    yield
    transfer_mod._alert_cooldown.clear()
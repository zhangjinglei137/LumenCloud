"""「下载完成回调」鉴权 + 状态门控 + 幂等 端到端回归封闭（Task C5）。

定位：补齐「真实回调链路」的端到端覆盖——真实 HTTP（TestClient 挂载
notify.router，走真实签名校验）→ 真实 `trigger_download_complete`（**不 mock**）→
真实 in-memory SQLite 的状态门控与幂等。验证回调受理后「是否推进」完全由
trigger_download_complete 内部条件更新决定（downloading→scrape，rowcount=0 即
幂等返回），端点对推进结果统一应答 200（回调已受理，不伪装推进，notify.py）。

覆盖（对应 brief Task C5 第 5/6/7 项）：
- 合法签名 + downloading gid → 200，DB 推进 downloading→scrape，触发刮削一次
- 合法签名 + 非 downloading gid（库中无 downloading 命中）→ 200 受理，DB 不推进
- 重复回调（同 gid 已推进）→ 两次 200，仅推进一次（幂等，不重复触发刮削）

边界：HTTP 鉴权拒绝面（无签名 / 错误签名 / 超时戳 / secret 未配置 / gid 为空 /
trigger 未就绪 → 503）已由 test_notify.py 逐项覆盖，函数层推进/幂等/未知 gid
已由 test_transfer.py（P6 段）覆盖——本文件不重复上述单层用例，只做端到端串联，
避免测试膨胀。

fake 组装对齐 test_transfer.py 的 env 风格：transfer 模块的 aria2/notifier/_spawn
注入 fake，async_session 指向测试库（patch_db）。
"""
import asyncio
import hashlib
import hmac
import json
import time
import types
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.notify as notify_mod
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media

_SECRET = "e2e-webhook-secret-0123456789abcdef"
_ENDPOINT = "/internal/aria2/notify"
_DL_GID = "gid-e2e-dl-1"


def run(coro):
    """同步执行 async 协程（fixture 初始化与断言辅助）。"""
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# fake 服务（对齐 test_transfer.py env 风格，只取回调推进路径所需的依赖）
# ---------------------------------------------------------------------------

class FakeAria2Client:
    """aria2.client：trigger 内部 tell_status 查 totalLength 回填（失败静默）。"""

    def __init__(self):
        self.statuses = {}

    async def tell_status(self, gid):
        return {"status": self.statuses.get(gid, "active")}


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """独立 in-memory SQLite（StaticPool 共享连接）→ 返回 sessionmaker。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    run(_create())
    yield maker
    run(engine.dispose())


@pytest.fixture()
def env(monkeypatch):
    """替换 transfer 模块回调推进路径的依赖：aria2 / notifier / _spawn。"""
    fakes = {
        "aria2": FakeAria2Client(),
        "notifier": FakeNotifier(),
    }
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    # 刮削执行器触发用 _spawn——置「跟踪不执行」（验证触发行为，不真正跑刮削）
    spawn_calls: list = []
    monkeypatch.setattr(transfer_mod, "_spawn", lambda factory: spawn_calls.append(factory))
    fakes["spawn"] = spawn_calls
    return fakes


def patch_db(monkeypatch, db):
    """把 transfer 模块使用的 async_session 换成测试库（trigger 真实落库）。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_downloading(db, *, gid=_DL_GID, file_name="ep.mkv"):
    """写入 media + download_queue(downloading)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="downloading")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode="S01E01", file_name=file_name, file_size=1024,
            share_code="sc123", stoken="stoken-x", fids='["f1"]',
            status="downloading", aria2_gid=gid, quark_path=f"/quark/{file_name}",
            retry_count=0, node_attempt=0, node_started_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def seed_pending(db, *, file_name="ep-pending.mkv"):
    """写入 media + download_queue(pending)（无 aria2_gid，模拟排队中任务）。"""
    async with db() as s:
        media = Media(title="排队剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        dq = DownloadQueue(
            media_id=mid, episode="S01E02", file_name=file_name, file_size=2048,
            share_code="sc123", stoken="stoken-x", receive_code="提取码占位",
            fids='["f1"]', fid_tokens='["ft1"]', folder_id="folder-1",
            download_name=None, status="pending", retry_count=0, quota_reject_count=0,
            save_task_id=None, save_attempt_at=None, quark_path=None, aria2_gid=None,
            enqueued_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return mid, dq.id


async def get_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


# ---------------------------------------------------------------------------
# HTTP 回调辅助（签名构造与 notify.py 校验逻辑一致）
# ---------------------------------------------------------------------------

def sign(body_bytes: bytes, secret: str = _SECRET) -> str:
    """对 body 原文计算 HMAC-SHA256 签名（hex，与后端校验逻辑一致）。"""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def make_body(gid: str, ts: int | None = None) -> bytes:
    """构造回调 body（ts 缺省为当前毫秒时间戳）。"""
    if ts is None:
        ts = int(time.time() * 1000)
    return json.dumps({"gid": gid, "ts": ts}).encode("utf-8")


def make_client(monkeypatch) -> TestClient:
    """构建仅挂载 notify.router 的最小 app；config_store 用 fake 注入 secret。"""
    fake_store = types.SimpleNamespace(get=lambda key, default=None: _SECRET)
    monkeypatch.setattr(notify_mod, "config_store", fake_store)
    app = FastAPI()
    app.include_router(notify_mod.router)
    return TestClient(app)


def post_callback(client, gid: str) -> Any:
    """发送合法签名 + 新鲜时间戳的下载完成回调，返回 TestClient 响应。"""
    body = make_body(gid=gid)
    return client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )


# ---------------------------------------------------------------------------
# 端到端：合法签名 + downloading gid → 正常推进
# ---------------------------------------------------------------------------

def test_e2e_valid_sig_downloading_gid_promotes(db, env, monkeypatch):
    """合法签名 + downloading gid → 200 {"ok": true}，DB 推进 downloading→scrape，
    触发刮削执行器一次（真实 trigger_download_complete，不 mock）。"""
    patch_db(monkeypatch, db)
    client = make_client(monkeypatch)
    _, dq_id = run(seed_downloading(db, gid=_DL_GID))

    r = post_callback(client, _DL_GID)

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    dq = run(get_dq(db, dq_id))
    assert dq.status == "scrape"
    assert dq.node_attempt == 0
    assert dq.node_started_at is not None
    assert dq.node_finished_at is not None
    # 刮削执行器事件触发一次（同轮询推进语义）
    assert env["spawn"] == [transfer_mod.scrape_runner]


# ---------------------------------------------------------------------------
# 端到端：合法签名 + 非 downloading gid → 受理但不推进
# ---------------------------------------------------------------------------

def test_e2e_valid_sig_non_downloading_gid_accepted_but_not_promoted(db, env, monkeypatch):
    """合法签名 + 库中无 downloading 命中（仅 pending 任务，gid 反查不到）→
    回调受理 200（端点不伪装推进），DB 状态不变、刮削不触发。"""
    patch_db(monkeypatch, db)
    client = make_client(monkeypatch)
    _, pending_dq_id = run(seed_pending(db))

    r = post_callback(client, "gid-unknown-no-downloading")

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    # pending 任务未被推进（trigger 反查 downloading 未命中 → False）
    dq = run(get_dq(db, pending_dq_id))
    assert dq.status == "pending"
    assert env["spawn"] == []


# ---------------------------------------------------------------------------
# 端到端：重复回调 → 幂等（仅推进一次）
# ---------------------------------------------------------------------------

def test_e2e_repeat_callback_idempotent(db, env, monkeypatch):
    """同 gid 重复回调 → 两次均 200 受理；但仅第一次真正推进（downloading→scrape），
    第二次（dq 已 scrape = 非 downloading）幂等不重复推进、不重复触发刮削。"""
    patch_db(monkeypatch, db)
    client = make_client(monkeypatch)
    _, dq_id = run(seed_downloading(db, gid=_DL_GID))

    r1 = post_callback(client, _DL_GID)
    r2 = post_callback(client, _DL_GID)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    dq = run(get_dq(db, dq_id))
    assert dq.status == "scrape"
    # 刮削仅触发一次（第二次回调 rowcount=0 → False，不重复触发）
    assert env["spawn"] == [transfer_mod.scrape_runner]

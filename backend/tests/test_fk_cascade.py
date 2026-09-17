"""Task A1：外键 ondelete 级联策略测试（数据/schema 迁移锚点）。

验证 DB 层外键级联语义（模拟应用层**不手动删子表**的路径——直接对 ORM 父对象
执行 session.delete + commit，由 SQLite PRAGMA foreign_keys=ON + FK ondelete
规则驱动子行清理/置空）：

- CASCADE：删除 Media 父行后，episode_state / task_queue / download_queue /
  download_task 子行随父行级联删除（无 ondelete 时删除会被
  "FOREIGN KEY constraint failed" 拦截 → 该行为缺失即本测试 RED 失败）。
- SET NULL：删除 User 父行后，watch_requests.requested_by / reviewed_by、
  invite_codes.used_by 引用列被置 NULL（行保留）。
  （invite_codes.created_by 不在本任务 ondelete 范围，seed 时置 NULL 以绕开
  其无 ondelete 约束对「删除 User」的合法拦截，专注验证 used_by 的 SET NULL。）

数据库隔离：模块导入前把 LUMENCLOUD_DATA_DIR 指向临时目录（app.database 模块级
engine 创建时读取），使用独立临时 SQLite，不碰生产数据。lifespan 由 TestClient
上下文自动触发（init_db → alembic upgrade head，即迁移 0017 落地验证通道）。
"""
import atexit
import os
import shutil
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_fkcascade_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# 进程退出时清理临时数据目录（防止测试运行残留磁盘文件）
atexit.register(shutil.rmtree, _TMP_DATA, ignore_errors=True)
# 隔离外部服务：避免发起真实外部网络调用
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["CLOUDSAVER_USERNAME"] = ""
os.environ["CLOUDSAVER_PASSWORD"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""
os.environ["ALIST_BASE_URL"] = ""
os.environ["ALIST_TOKEN"] = ""
os.environ["ARIA2_RPC_URL"] = ""
os.environ["ARIA2_TOKEN"] = ""
os.environ["NASTOOLS_BASE_URL"] = ""
os.environ["PUSHPLUS_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


async def _seed_cascade_media():
    """媒体 + 四类子行（episode_state / task_queue / download_queue / download_task）。

    download_queue 同时引用 media（media_id）与 task_queue（task_queue_id，
    验证 FK 链上第二条 CASCADE）。download_task 仅引用 media（transfer_id 留空，
    避免引入 transfer_queue 依赖）。
    """
    from app.database import async_session
    from app.models import DownloadQueue, DownloadTask, EpisodeState, Media, TaskQueue

    async with async_session() as session:
        media = Media(title="FK级联测试", status="tracking", in_emby=False)
        session.add(media)
        await session.flush()
        session.add_all(
            [
                EpisodeState(media_id=media.id, episode="S01E01", state="done"),
                TaskQueue(media_id=media.id, episode="S01E01", status="done"),
                DownloadQueue(
                    media_id=media.id,
                    episode="S01E01",
                    task_queue_id=None,  # 先无 task_queue 引用（task_queue_id 可空）
                    file_name="f01.mkv",
                    file_size=100,
                    share_code="ScAa11111111",
                    status="done",
                ),
                DownloadTask(
                    media_id=media.id, episode="S01E01", status="complete"
                ),
            ]
        )
        await session.commit()
        return {"media_id": media.id, "task_queue_id": None}


async def _delete_media_only_and_count(media_id: int) -> dict:
    """仅删除 Media 父行（新 session 中无任何子对象驻留），返回四张子表残留计数。"""
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import DownloadQueue, DownloadTask, EpisodeState, Media, TaskQueue

    async with async_session() as session:
        m = await session.get(Media, media_id)
        # 关键：只删父行，不手动删任何子表
        await session.delete(m)
        await session.commit()

    async with async_session() as session:
        counts = {}
        for model in (EpisodeState, TaskQueue, DownloadQueue, DownloadTask):
            counts[model.__tablename__] = await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.media_id == media_id)
            )
        return counts


async def _seed_task_queue_cascade_only():
    """隔离第二跳 CASCADE 的 seed：download_queue 同时引用两个父行，只删 task_queue。

    - media_keep：**不被删除**的 media（承担 download_queue.media_id 引用，
      确保第一跳 media_id→media CASCADE 不会被触发，隔离验证第二跳）；
    - task_queue：被删除的父行（task_queue_id 引用它的 download_queue 应随
      第二跳 CASCADE 一起清除）。
    """
    from app.database import async_session
    from app.models import DownloadQueue, Media, TaskQueue

    async with async_session() as session:
        media_keep = Media(title="FK第二跳保留", status="tracking", in_emby=False)
        session.add(media_keep)
        await session.flush()
        tq = TaskQueue(media_id=media_keep.id, episode="S01E01", status="done")
        session.add(tq)
        await session.flush()
        dq = DownloadQueue(
            media_id=media_keep.id,
            episode="S01E01",
            task_queue_id=tq.id,
            file_name="f01.mkv",
            file_size=100,
            share_code="ScBb22222222",
            status="done",
        )
        session.add(dq)
        await session.commit()
        return {
            "media_keep_id": media_keep.id,
            "task_queue_id": tq.id,
            "download_queue_id": dq.id,
        }


async def _delete_task_queue_only_and_check(
    task_queue_id: int, media_keep_id: int, download_queue_id: int
) -> dict:
    """仅删除 task_queue 父行（不删 media、不手动删子表），返回第二跳残留计数与对照状态。"""
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import DownloadQueue, Media, TaskQueue

    async with async_session() as session:
        tq = await session.get(TaskQueue, task_queue_id)
        await session.delete(tq)
        await session.commit()

    async with async_session() as session:
        dq_refs = await session.scalar(
            select(func.count())
            .select_from(DownloadQueue)
            .where(DownloadQueue.task_queue_id == task_queue_id)
        )
        dq_by_id = await session.get(DownloadQueue, download_queue_id)
        media_keep = await session.get(Media, media_keep_id)
        return {
            "dq_task_queue_refs": dq_refs,     # 第二跳 CASCADE 应清空 → 0
            "dq_alive": dq_by_id is not None,  # 第二跳 CASCADE 生效 → False
            "media_keep_alive": media_keep is not None,  # 第一跳未触发 → True
        }


async def _seed_user_refs():
    """用户 + 引用它的 watch_requests（requested_by/reviewed_by）与 invite_codes（used_by）。

    注意：invite_codes.created_by 指向另一个不受删除影响的用户（created_by 的
    ondelete 不在本任务范围，保持无 ondelete 会合法拦截「删除其引用者」——故
    单独建 user_keep 承担 created_by 引用，以隔离验证 used_by 的 SET NULL）。
    """
    from app.database import async_session
    from app.models import InviteCode, User, WatchRequest

    async with async_session() as session:
        user = User(username="cascade_user", password_hash="x", role="guest")
        user_keep = User(username="cascade_keep", password_hash="x", role="guest")
        session.add_all([user, user_keep])
        await session.flush()
        watch = WatchRequest(
            requested_by=user.id,
            reviewed_by=user.id,
            title="想看测试",
            status="pending",
        )
        invite = InviteCode(
            code="FK_SET_NULL01", created_by=user_keep.id, used_by=user.id
        )
        session.add_all([watch, invite])
        await session.commit()
        return {"user_id": user.id, "watch_id": watch.id, "invite_code": invite.code}


async def _delete_user_only_and_check(user_id: int, watch_id: int, invite_code: str) -> dict:
    """仅删除 User 父行，返回引用列是否被置 NULL + 引用行是否保留。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import InviteCode, User, WatchRequest

    async with async_session() as session:
        u = await session.get(User, user_id)
        await session.delete(u)
        await session.commit()

    async with async_session() as session:
        watch = await session.get(WatchRequest, watch_id)
        invite = await session.scalar(
            select(InviteCode).where(InviteCode.code == invite_code)
        )
        return {
            "watch_requested_by": watch.requested_by,
            "watch_reviewed_by": watch.reviewed_by,
            "invite_used_by": invite.used_by,
            "watch_alive": watch is not None,
            "invite_alive": invite is not None,
        }


def test_delete_media_cascades_child_rows():
    """仅删除 Media 父行 → episode_state/task_queue/download_queue/download_task 级联清空。"""
    with TestClient(app) as client:
        seed = client.portal.call(_seed_cascade_media)
        counts = client.portal.call(_delete_media_only_and_count, seed["media_id"])
        assert counts == {
            "episode_state": 0,
            "task_queue": 0,
            "download_queue": 0,
            "download_task": 0,
        }, counts


def test_delete_task_queue_cascades_download_queue_ref():
    """FK 第二跳隔离：仅删除 task_queue → 引用其 task_queue_id 的 download_queue 级联删除。

    download_queue.media_id 指向一个**不被删除**的 media_keep，故第一跳
    （media_id→media CASCADE）不会被触发；断言 download_queue 行消失只能由
    task_queue_id→task_queue 的第二跳 CASCADE 实现。对照：media_keep 仍存在。
    """
    with TestClient(app) as client:
        seed = client.portal.call(_seed_task_queue_cascade_only)
        result = client.portal.call(
            _delete_task_queue_only_and_check,
            seed["task_queue_id"],
            seed["media_keep_id"],
            seed["download_queue_id"],
        )
        # 第二跳 CASCADE 生效：引用已删除 task_queue 的 download_queue 行被清空/不存在
        assert result["dq_task_queue_refs"] == 0, result
        assert result["dq_alive"] is False
        # 隔离对照：第一跳未被触发，media_keep 仍存在
        assert result["media_keep_alive"] is True


def test_delete_user_sets_null_on_request_and_invite_refs():
    """仅删除 User 父行 → watch_requests.requested_by/reviewed_by、invite_codes.used_by 置 NULL（行保留）。"""
    with TestClient(app) as client:
        seed = client.portal.call(_seed_user_refs)
        result = client.portal.call(
            _delete_user_only_and_check,
            seed["user_id"],
            seed["watch_id"],
            seed["invite_code"],
        )
        assert result["watch_alive"] is True
        assert result["invite_alive"] is True
        assert result["watch_requested_by"] is None
        assert result["watch_reviewed_by"] is None
        assert result["invite_used_by"] is None
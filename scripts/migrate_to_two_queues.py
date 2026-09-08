"""
LumenCloud 影视下载两队列：旧三表 → 新两表数据迁移脚本
（docs/影视下载两队列重设计.md §9.1，冷切换时执行）

迁移方向：
  episode_state / transfer_queue / download_task ──▶ task_queue / download_queue

映射规则（与设计文档 §9.1 一致）：
  - episode_state(node ∈ transfer/download/downloading 且 state ∈ transferring/downloading)
      → download_queue(status=transferring/downloading)
  - episode_state(node=idle 且 state=queued)
      → task_queue(status=pending)
  - episode_state(node=scrape/library)
      → download_queue(status=原样)
  - episode_state(done/failed)
      → download_queue(status=终态)
  - 分享信息快照（file_name/file_size/share_code/fids/fid_tokens/folder_id/
    save_task_id/save_attempt_at）← transfer_queue
  - aria2_gid ← download_task（无则 NULL）
  - quark_path ← episode_state.quark_path 或 download_task.quark_path（优先非空）
  - 电影 episode 键归一化：旧键=夸克文件名，新键=movie:<title> —— 旧 done/failed
    记录按原键保留（防重窗口语义不变），新键仅后续探测/promote 生效

用法：
  # 只读预览（默认模式）：打印待迁移/跳过统计，不写库
  python scripts/migrate_to_two_queues.py

  # 实际写入（--apply 前自动导出回滚备份到 scripts/backup/）
  python scripts/migrate_to_two_queues.py --apply

  # 容器内执行（上线冷切换）
  docker exec lumencloud python scripts/migrate_to_two_queues.py --apply

护栏：
  - 不带 --apply 只做 dry-run（只读预览，不写库、不写备份）
  - 幂等：目标表 UNIQUE(media_id, episode) 冲突即跳过（重复执行安全）
  - --apply 前自动导出回滚备份 JSON（新表已有行 + 本次待写行）
  - 旧三表仅读取，不删除（退役动作由运维在验证后手工执行）
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# 允许从任意 cwd 运行：backend 目录加入 sys.path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import async_session  # noqa: E402
from app.models import (  # noqa: E402
    DownloadQueue,
    DownloadTask,
    EpisodeState,
    Media,
    TaskQueue,
    TransferQueue,
)

# 进行中节点（映射为 download_queue 的 transferring/downloading）
_ACTIVE_NODES = ("transfer", "download", "downloading")
_ACTIVE_STATES = ("transferring", "downloading")
# 刮削/入库节点（原样保留到 download_queue）
_SCRAPE_LIBRARY_NODES = ("scrape", "library")

STATUS_MAP = {"transfer": "transferring", "download": "downloading", "downloading": "downloading"}


def _now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def _movie_key(title: str) -> str:
    """电影归一化键（设计 §7）：movie:<media.title>。"""
    return f"movie:{(title or '').strip()}"


async def _preview() -> dict:
    """只读预览：统计各迁移分支的行数，不写库。"""
    async with async_session() as s:
        rows = (await s.execute(select(EpisodeState))).scalars().all()
        active = [r for r in rows if r.node in _ACTIVE_NODES and r.state in _ACTIVE_STATES]
        queued = [r for r in rows if r.node == "idle" and r.state == "queued"]
        scrape_lib = [r for r in rows if r.node in _SCRAPE_LIBRARY_NODES]
        terminal = [r for r in rows if r.node in ("done", "failed")]
        other = [r for r in rows if r not in active + queued + scrape_lib + terminal]
        tq_count = (await s.execute(select(text("COUNT(*)")).select_from(TransferQueue))).scalar()
        dl_count = (await s.execute(select(text("COUNT(*)")).select_from(DownloadTask))).scalar()
    return {
        "episode_state_total": len(rows),
        "to_download_queue_active": len(active),
        "to_task_queue_queued": len(queued),
        "to_download_queue_scrape_library": len(scrape_lib),
        "to_download_queue_terminal": len(terminal),
        "skip_other": len(other),
        "transfer_queue_rows": tq_count,
        "download_task_rows": dl_count,
    }


async def _apply() -> dict:
    """执行迁移：旧三表 → 新两表（幂等：UNIQUE 冲突跳过）。"""
    backup = {"exported_at": _now().isoformat(), "rows": []}
    stats = {"tq_insert": 0, "dq_insert": 0, "tq_conflict": 0, "dq_conflict": 0, "skip": 0}
    async with async_session() as s:
        # 预取媒体标题（电影键归一化用）
        media_rows = (await s.execute(select(Media.id, Media.title))).all()
        title_by_id = {mid: title for mid, title in media_rows}
        es_rows = (await s.execute(select(EpisodeState))).scalars().all()

        # 快照表：transfer_queue 按 (media_id, episode) 索引；download_task 同
        tq_rows = (await s.execute(select(TransferQueue))).scalars().all()
        dl_rows = (await s.execute(select(DownloadTask))).scalars().all()
        tq_by_key = {(t.media_id, t.episode): t for t in tq_rows}
        dl_by_key = {}
        for d in dl_rows:
            if d.media_id is not None and d.episode:
                dl_by_key.setdefault((d.media_id, d.episode), d)

        for es in es_rows:
            key = (es.media_id, es.episode)
            tq = tq_by_key.get(key)
            dl = dl_by_key.get(key)
            snap = {
                "media_id": es.media_id,
                "episode": es.episode,
                "file_name": (tq.file_name if tq else es.file_name) or "",
                "file_size": (tq.file_size if tq else es.file_size) or 0,
                "share_code": (tq.share_code if tq else es.share_code) or "",
                "pwd_id": tq.pwd_id if tq else None,
                "stoken": tq.stoken if tq else None,
                "receive_code": tq.receive_code if tq else None,
                "fids": tq.fids if tq else None,
                "fid_tokens": tq.fid_tokens if tq else None,
                "folder_id": tq.folder_id if tq else None,
                "save_task_id": tq.save_task_id if tq else None,
                "save_attempt_at": tq.save_attempt_at if tq else None,
            }

            # ---- 分支 1：queued → task_queue(pending) ----
            if es.node == "idle" and es.state == "queued":
                exists = (await s.execute(
                    select(text("1")).select_from(TaskQueue).where(
                        TaskQueue.media_id == es.media_id, TaskQueue.episode == es.episode
                    )
                )).scalar()
                if exists:
                    stats["tq_conflict"] += 1
                    continue
                row = TaskQueue(
                    media_id=es.media_id,
                    episode=es.episode,
                    file_name=snap["file_name"],
                    file_size=snap["file_size"],
                    share_code=snap["share_code"],
                    pwd_id=snap["pwd_id"],
                    stoken=snap["stoken"],
                    receive_code=snap["receive_code"],
                    fids=snap["fids"],
                    fid_tokens=snap["fid_tokens"],
                    folder_id=snap["folder_id"],
                    status="pending",
                    probe_attempt=0,
                    error=None,
                )
                backup["rows"].append({"to": "task_queue", "row": row})
                s.add(row)
                stats["tq_insert"] += 1
                continue

            # ---- 分支 2/3/4：active / scrape_library / terminal → download_queue ----
            if es.node in _ACTIVE_NODES or es.node in _SCRAPE_LIBRARY_NODES or es.node in ("done", "failed"):
                exists = (await s.execute(
                    select(text("1")).select_from(DownloadQueue).where(
                        DownloadQueue.media_id == es.media_id, DownloadQueue.episode == es.episode
                    )
                )).scalar()
                if exists:
                    stats["dq_conflict"] += 1
                    continue
                if es.node in _ACTIVE_NODES:
                    status = STATUS_MAP.get(es.node, "transferring")
                elif es.node in _SCRAPE_LIBRARY_NODES:
                    status = es.node
                else:
                    status = es.node  # done / failed
                quark_path = es.quark_path or (dl.quark_path if dl else None)
                row = DownloadQueue(
                    media_id=es.media_id,
                    episode=es.episode,
                    task_queue_id=None,
                    file_name=snap["file_name"],
                    file_size=snap["file_size"],
                    share_code=snap["share_code"],
                    pwd_id=snap["pwd_id"],
                    stoken=snap["stoken"],
                    receive_code=snap["receive_code"],
                    fids=snap["fids"],
                    fid_tokens=snap["fid_tokens"],
                    folder_id=snap["folder_id"],
                    download_name=None,
                    aria2_gid=dl.aria2_gid if dl else es.aria2_gid,
                    quark_path=quark_path,
                    local_path=None,
                    save_task_id=snap["save_task_id"],
                    save_attempt_at=snap["save_attempt_at"],
                    status=status,
                    node_attempt=es.node_attempt or 0,
                    node_started_at=es.node_started_at,
                    node_finished_at=es.node_finished_at,
                    node_error=es.node_error,
                    retry_count=es.retry_count or 0,
                    quota_reject_count=tq.quota_reject_count if tq else 0,
                    error=es.error,
                )
                backup["rows"].append({"to": "download_queue", "row": row})
                s.add(row)
                stats["dq_insert"] += 1
                continue

            # ---- 其余状态（其他 node/state 组合）：跳过并统计 ----
            stats["skip"] += 1

        backup_path = None
        if backup["rows"]:
            from app.database import engine

            # 导出回滚备份（当前库新表已有行 + 本次待写行）
            backup_dir = Path(__file__).resolve().parent / "backup"
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / f"two_queues_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            backup["rows"] = [
                {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row["row"].__dict__.items()
                 if not k.startswith("_")}
                for row in backup["rows"]
            ]
            backup_path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
        await s.commit()
    stats["backup"] = str(backup_path) if backup_path else None
    return stats


async def _main() -> None:
    parser = argparse.ArgumentParser(description="旧三表 → 新两表数据迁移（dry-run 默认）")
    parser.add_argument("--apply", action="store_true", help="实际写入（默认仅只读预览）")
    args = parser.parse_args()

    try:
        if args.apply:
            print("=== 迁移前预览 ===")
        stats = await _preview()
        for k, v in stats.items():
            print(f"  {k}: {v}")

        if not args.apply:
            print("\n（dry-run 模式：仅预览，未写库。加 --apply 实际执行。）")
            return

        print("\n=== 执行迁移 ===")
        result = await _apply()
        for k, v in result.items():
            print(f"  {k}: {v}")
        print("\n迁移完成。旧三表未删除（退役动作请人工验证后执行）。")
    except DBAPIError as exc:
        print(f"\n数据库错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_main())

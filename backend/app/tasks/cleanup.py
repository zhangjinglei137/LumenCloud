from sqlalchemy import select
from app.database import async_session
from app.models import DownloadTask, CleanupRecord
from app.services.alist import alist_service


async def cleanup_quark_files(ctx):
    async with async_session() as db:
        result = await db.execute(
            select(DownloadTask).where(
                DownloadTask.status == "completed",
                DownloadTask.quark_file_id.isnot(None),
            )
        )
        tasks = result.scalars().all()

        for task in tasks:
            try:
                await alist_service.delete_file("/quark", [task.quark_file_id])

                record = CleanupRecord(
                    download_task_id=task.id,
                    quark_file_id=task.quark_file_id,
                    file_name=task.file_name,
                )
                db.add(record)

                task.status = "cleaned"
            except Exception:
                continue

        await db.commit()

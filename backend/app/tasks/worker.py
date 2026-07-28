from arq.connections import RedisSettings
from app.config import settings


async def startup(ctx):
    pass


async def shutdown(ctx):
    pass


class WorkerSettings:
    functions = [
        "app.tasks.scan.scan_all_media",
        "app.tasks.scan.scan_single_media",
        "app.tasks.download.run_download_pipeline",
        "app.tasks.cleanup.cleanup_quark_files",
    ]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    on_startup = startup
    on_shutdown = shutdown
    cron_jobs = [
        {"name": "Daily full scan", "coroutine": "app.tasks.scan.scan_all_media", "cron": "0 3 * * *"},
        {"name": "Weekly Quark cleanup", "coroutine": "app.tasks.cleanup.cleanup_quark_files", "cron": "0 4 * * 0"},
    ]

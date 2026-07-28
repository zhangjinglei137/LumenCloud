import re
import asyncio
from sqlalchemy import select
from app.database import async_session
from app.models import Media, DownloadTask, MediaStatus
from app.services.cloudsaver import cloudsaver_service
from app.services.aria2 import aria2_service
from app.services.alist import alist_service


async def run_download_pipeline(ctx, media_id: str, missing_codes: list[str]):
    async with async_session() as db:
        result = await db.execute(select(Media).where(Media.id == media_id))
        media = result.scalar_one_or_none()
        if media is None:
            return

        media.status = MediaStatus.DOWNLOADING
        await db.commit()

    # 1. Search CloudSaver
    search_result = await cloudsaver_service.search(media.title)
    data_list = search_result.get("data", [])

    # 2. Extract Quark resources
    quark_resources = []
    for data_item in data_list:
        for list_item in data_item.get("list", []):
            title = list_item.get("title", "")
            cloud_links = list_item.get("cloudLinks", [])
            for link in cloud_links:
                if link.get("cloudType") == "quark" and link.get("link"):
                    quark_resources.append({
                        "title": title,
                        "link": link["link"],
                    })

    if not quark_resources:
        async with async_session() as db:
            media.status = MediaStatus.TRACKING
            await db.commit()
        return

    async with async_session() as db:
        for ep_code in missing_codes:
            matched = None
            for res in quark_resources:
                if _match_episode(res["title"], ep_code):
                    matched = res
                    break

            if not matched:
                continue

            share_match = re.search(r'https://pan\.quark\.cn/s/([^&/]+)', matched["link"])
            if not share_match:
                continue
            share_code = share_match.group(1)

            save_result = await cloudsaver_service.quark_save({
                "shareCode": share_code,
                "folderId": "",
            })

            if not save_result.get("success"):
                continue

            try:
                await asyncio.sleep(3)
                file_list = await alist_service.list_files("/quark", refresh=True)
                files = file_list.get("data", {}).get("content", [])

                for f in files:
                    fname = f.get("name", "")
                    if _match_episode(fname, ep_code):
                        download_link = await alist_service.get_download_link(f"/quark/{fname}")
                        if download_link:
                            gid = await aria2_service.add_uri(
                                [download_link],
                                dir_path="/downloads",
                                filename=fname,
                            )
                            task = DownloadTask(
                                media_id=media.id,
                                aria2_gid=gid,
                                quark_file_id=f.get("name", ""),
                                quark_share_code=share_code,
                                episode_range=ep_code,
                                file_name=fname,
                                status="downloading",
                            )
                            db.add(task)
                        break
            except Exception:
                continue

        await db.commit()


def _match_episode(filename: str, ep_code: str) -> bool:
    name = filename.lower()
    code_lower = ep_code.lower()
    if code_lower in name:
        return True
    match = re.match(r'S(\d+)E(\d+)', ep_code, re.IGNORECASE)
    if match:
        season, episode = match.groups()
        cn_pattern = f"\u7b2c{int(episode)}\u96c6"
        if cn_pattern in name:
            return True
    return False

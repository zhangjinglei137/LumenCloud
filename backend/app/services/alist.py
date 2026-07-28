import httpx
from app.config import settings


class AListService:
    def __init__(self):
        self.base_url = settings.ALIST_BASE_URL
        self.token = settings.ALIST_TOKEN

    async def list_files(self, path: str = "/quark", refresh: bool = False) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/fs/list", json={
                "path": path,
                "refresh": refresh,
            }, headers={"Authorization": self.token})
            resp.raise_for_status()
            return resp.json()

    async def delete_file(self, path: str, file_names: list[str]) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/fs/remove", json={
                "dir": path,
                "names": file_names,
            }, headers={"Authorization": self.token})
            resp.raise_for_status()
            return resp.json()

    async def get_file_info(self, path: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/fs/get", json={
                "path": path,
            }, headers={"Authorization": self.token})
            resp.raise_for_status()
            return resp.json()

    async def get_download_link(self, path: str) -> str:
        info = await self.get_file_info(path)
        return info.get("data", {}).get("raw_url", "")


alist_service = AListService()

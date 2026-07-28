import httpx
from app.services.config_service import config_service


class AListService:
    async def _get_base_url(self) -> str:
        return await config_service.get("ALIST_BASE_URL")

    async def _get_token(self) -> str:
        return await config_service.get("ALIST_TOKEN")

    async def list_files(self, path: str = "/quark", refresh: bool = False) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{await self._get_base_url()}/api/fs/list", json={
                "path": path, "refresh": refresh,
            }, headers={"Authorization": await self._get_token()})
            resp.raise_for_status()
            return resp.json()

    async def delete_file(self, path: str, file_names: list[str]) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{await self._get_base_url()}/api/fs/remove", json={
                "dir": path, "names": file_names,
            }, headers={"Authorization": await self._get_token()})
            resp.raise_for_status()
            return resp.json()

    async def get_file_info(self, path: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{await self._get_base_url()}/api/fs/get", json={
                "path": path,
            }, headers={"Authorization": await self._get_token()})
            resp.raise_for_status()
            return resp.json()

    async def get_download_link(self, path: str) -> str:
        return (await self.get_file_info(path)).get("data", {}).get("raw_url", "")


alist_service = AListService()

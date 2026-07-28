import httpx
from app.services.config_service import config_service


class NasToolsService:
    async def _get_base_url(self) -> str:
        return await config_service.get("NASTOOLS_BASE_URL")

    async def _get_username(self) -> str:
        return await config_service.get("NASTOOLS_USERNAME")

    async def _get_password(self) -> str:
        return await config_service.get("NASTOOLS_PASSWORD")

    async def _login(self) -> str:
        base_url = await self._get_base_url()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/",
                data={"next": "", "username": await self._get_username(),
                      "password": await self._get_password(), "remember": "on"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            for c in resp.headers.get("set-cookie", "").split(","):
                if "session=" in c:
                    return c.split(";")[0].strip()
            return ""

    async def restart(self) -> bool:
        cookie = await self._login()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{await self._get_base_url()}/do",
                json={"cmd": "restart", "data": {}}, headers={"cookie": cookie})
            return resp.status_code == 200

    async def directory_sync(self) -> bool:
        import asyncio
        await asyncio.sleep(30)
        cookie = await self._login()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{await self._get_base_url()}/do",
                json={"cmd": "run_directory_sync", "data": {"sid": []}},
                headers={"cookie": cookie})
            return resp.status_code == 200


nastools_service = NasToolsService()

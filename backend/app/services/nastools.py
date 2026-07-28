import httpx
from app.config import settings


class NasToolsService:
    def __init__(self):
        self.base_url = settings.NASTOOLS_BASE_URL
        self.username = settings.NASTOOLS_USERNAME
        self.password = settings.NASTOOLS_PASSWORD

    async def _login(self) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/",
                data={
                    "next": "",
                    "username": self.username,
                    "password": self.password,
                    "remember": "on",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            cookies = resp.headers.get("set-cookie", "")
            session_cookie = ""
            for c in cookies.split(","):
                if "session=" in c:
                    session_cookie = c.split(";")[0].strip()
                    break
            return session_cookie

    async def restart(self) -> bool:
        cookie = await self._login()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/do",
                json={"cmd": "restart", "data": {}},
                headers={"cookie": cookie},
            )
            return resp.status_code == 200

    async def directory_sync(self) -> bool:
        import asyncio
        await asyncio.sleep(30)
        cookie = await self._login()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/do",
                json={"cmd": "run_directory_sync", "data": {"sid": []}},
                headers={"cookie": cookie},
            )
            return resp.status_code == 200


nastools_service = NasToolsService()

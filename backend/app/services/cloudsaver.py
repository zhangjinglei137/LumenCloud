import httpx
from app.config import settings


class CloudSaverService:
    def __init__(self):
        self.base_url = settings.CLOUDSAVER_BASE_URL
        self.username = settings.CLOUDSAVER_USERNAME
        self.password = settings.CLOUDSAVER_PASSWORD
        self._token: str | None = None

    async def _login(self) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/user/login", json={
                "username": self.username,
                "password": self.password,
            })
            data = resp.json()
            self._token = data["data"]["token"]
            return self._token

    async def _get_token(self) -> str:
        if not self._token:
            await self._login()
        return self._token

    async def search(self, keyword: str) -> dict:
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/api/search", params={
                "keyword": keyword,
            }, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()

    async def quark_save(self, payload: dict) -> dict:
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.base_url}/api/quark/save", json=payload, headers={
                "Authorization": f"Bearer {token}",
            })
            resp.raise_for_status()
            return resp.json()


cloudsaver_service = CloudSaverService()

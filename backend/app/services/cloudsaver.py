import httpx
from app.services.config_service import config_service


class CloudSaverService:
    def __init__(self):
        self._token: str | None = None

    async def _get_base_url(self) -> str:
        return await config_service.get("CLOUDSAVER_BASE_URL")

    async def _get_username(self) -> str:
        return await config_service.get("CLOUDSAVER_USERNAME")

    async def _get_password(self) -> str:
        return await config_service.get("CLOUDSAVER_PASSWORD")

    async def _login(self) -> str:
        base_url = await self._get_base_url()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{base_url}/api/user/login", json={
                "username": await self._get_username(),
                "password": await self._get_password(),
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
            resp = await client.get(f"{await self._get_base_url()}/api/search", params={
                "keyword": keyword,
            }, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            return resp.json()

    async def quark_save(self, payload: dict) -> dict:
        token = await self._get_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{await self._get_base_url()}/api/quark/save", json=payload, headers={
                "Authorization": f"Bearer {token}",
            })
            resp.raise_for_status()
            return resp.json()


cloudsaver_service = CloudSaverService()

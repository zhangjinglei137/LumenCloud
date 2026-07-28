import httpx
from app.config import settings


class EmbyService:
    def __init__(self):
        self.base_url = settings.EMBY_BASE_URL
        self.api_key = settings.EMBY_API_KEY

    async def authenticate_user(self, username: str, password: str) -> dict | None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={
                    "X-Emby-Authorization":
                    'Emby UserId="", Client="LumenCloud", Device="Web", DeviceId="lumen", Version="1.0"',
                },
            )
            if resp.status_code == 200:
                return resp.json()
            return None

    async def get_items_by_provider(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/Items", params={
                "api_key": self.api_key,
                "Recursive": "true",
                "HasTmdbId": "true",
                "Fields": "ProviderIds",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_missing_episodes(self, parent_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/emby/Shows/Missing", params={
                "ParentId": parent_id,
                "api_key": self.api_key,
                "IncludeUnaired": "true",
                "IncludeSpecials": "false",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_play_count(self, tmdb_id: int) -> int:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/Items", params={
                "api_key": self.api_key,
                "Recursive": "true",
                "HasTmdbId": "true",
                "Fields": "UserData",
                "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            data = resp.json()
            total = 0
            for item in data.get("Items", []):
                total += item.get("UserData", {}).get("PlayCount", 0)
            return total


emby_service = EmbyService()

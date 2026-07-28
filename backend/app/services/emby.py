import httpx
from app.services.config_service import config_service


class EmbyService:
    async def _get_base_url(self) -> str:
        return await config_service.get("EMBY_BASE_URL")

    async def _get_api_key(self) -> str:
        return await config_service.get("EMBY_API_KEY")

    async def authenticate_user(self, username: str, password: str) -> dict | None:
        base_url = await self._get_base_url()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
                headers={"X-Emby-Authorization": 'Emby UserId="", Client="LumenCloud", Device="Web", DeviceId="lumen", Version="1.0"'},
            )
            return resp.json() if resp.status_code == 200 else None

    async def get_items_by_provider(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{await self._get_base_url()}/Items", params={
                "api_key": await self._get_api_key(),
                "Recursive": "true", "HasTmdbId": "true",
                "Fields": "ProviderIds", "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_missing_episodes(self, parent_id: str) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{await self._get_base_url()}/emby/Shows/Missing", params={
                "ParentId": parent_id, "api_key": await self._get_api_key(),
                "IncludeUnaired": "true", "IncludeSpecials": "false",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_play_count(self, tmdb_id: int) -> int:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{await self._get_base_url()}/Items", params={
                "api_key": await self._get_api_key(),
                "Recursive": "true", "HasTmdbId": "true",
                "Fields": "UserData", "AnyProviderIdEquals": f"tmdb.{tmdb_id}",
            })
            resp.raise_for_status()
            return sum(item.get("UserData", {}).get("PlayCount", 0) for item in resp.json().get("Items", []))

    async def get_items_by_type(self, item_type: str) -> dict:
        """获取特定类型的 Emby 库内容。item_type: Movie / Series"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{await self._get_base_url()}/Items", params={
                "api_key": await self._get_api_key(),
                "Recursive": "true",
                "IncludeItemTypes": item_type,
                "Fields": "ProviderIds,UserData,ImageTags,ProductionYear,CommunityRating",
                "SortBy": "SortName",
                "Limit": "200",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_user_views(self) -> dict:
        """获取 Emby 用户视图（媒体库分类目录）"""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{await self._get_base_url()}/Users", params={
                "api_key": await self._get_api_key(),
            })
            resp.raise_for_status()
            users = resp.json()
            if not users:
                return {"Items": []}
            # 用第一个用户的视图
            user_id = users[0]["Id"]
            resp2 = await client.get(f"{await self._get_base_url()}/Users/{user_id}/Views", params={
                "api_key": await self._get_api_key(),
            })
            resp2.raise_for_status()
            return resp2.json()


emby_service = EmbyService()

import httpx
from app.services.config_service import config_service


class TMDBService:
    async def _get_key(self) -> str:
        return await config_service.get("TMDB_API_KEY")

    async def search_multi(self, keyword: str, page: int = 1) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.tmdb.org/3/search/multi", params={
                "api_key": await self._get_key(),
                "query": keyword, "language": "zh-CN", "page": page,
            })
            resp.raise_for_status()
            return resp.json()

    async def get_movie_detail(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.tmdb.org/3/movie/{tmdb_id}", params={
                "api_key": await self._get_key(), "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_tv_detail(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.tmdb.org/3/tv/{tmdb_id}", params={
                "api_key": await self._get_key(), "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_tv_season(self, tmdb_id: int, season_number: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.tmdb.org/3/tv/{tmdb_id}/season/{season_number}", params={
                "api_key": await self._get_key(), "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()


tmdb_service = TMDBService()

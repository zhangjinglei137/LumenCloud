import httpx
from app.config import settings


class TMDBService:
    def __init__(self):
        self.base_url = "https://api.tmdb.org/3"
        self.api_key = settings.TMDB_API_KEY

    async def search_multi(self, keyword: str, page: int = 1) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/search/multi", params={
                "api_key": self.api_key,
                "query": keyword,
                "language": "zh-CN",
                "page": page,
            })
            resp.raise_for_status()
            return resp.json()

    async def get_movie_detail(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/movie/{tmdb_id}", params={
                "api_key": self.api_key,
                "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_tv_detail(self, tmdb_id: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/tv/{tmdb_id}", params={
                "api_key": self.api_key,
                "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()

    async def get_tv_season(self, tmdb_id: int, season_number: int) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.base_url}/tv/{tmdb_id}/season/{season_number}", params={
                "api_key": self.api_key,
                "language": "zh-CN",
            })
            resp.raise_for_status()
            return resp.json()


tmdb_service = TMDBService()

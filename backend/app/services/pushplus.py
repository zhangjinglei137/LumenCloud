import httpx
from app.services.config_service import config_service


class PushPlusService:
    def __init__(self):
        self.url = "http://www.pushplus.plus/send"

    async def _get_token(self) -> str:
        return await config_service.get("PUSHPLUS_TOKEN")

    async def send(self, title: str, content: str, template: str = "html") -> bool:
        token = await self._get_token()
        if not token:
            return False
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.url, params={
                "token": token, "title": title, "content": content, "template": template,
            })
            return resp.status_code == 200


pushplus_service = PushPlusService()

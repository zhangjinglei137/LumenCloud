import httpx
from app.config import settings


class PushPlusService:
    def __init__(self):
        self.token = settings.PUSHPLUS_TOKEN
        self.url = "http://www.pushplus.plus/send"

    async def send(self, title: str, content: str, template: str = "html") -> bool:
        if not self.token:
            return False
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.url, params={
                "token": self.token,
                "title": title,
                "content": content,
                "template": template,
            })
            return resp.status_code == 200


pushplus_service = PushPlusService()

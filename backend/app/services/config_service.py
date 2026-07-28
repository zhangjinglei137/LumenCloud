from sqlalchemy import select
from app.database import async_session
from app.models.interaction import SystemConfig

DEFAULT_CONFIGS = {
    "TMDB_API_KEY":          ("", "TMDB API密钥"),
    "EMBY_BASE_URL":         ("http://192.168.3.31:8096", "Emby服务地址"),
    "EMBY_API_KEY":          ("", "Emby API密钥"),
    "CLOUDSAVER_BASE_URL":   ("http://192.168.3.31:8008", "CloudSaver服务地址"),
    "CLOUDSAVER_USERNAME":   ("admin", "CloudSaver用户名"),
    "CLOUDSAVER_PASSWORD":   ("", "CloudSaver密码"),
    "ARIA2_RPC_URL":         ("http://192.168.3.31:6800/jsonrpc", "Aria2 RPC地址"),
    "ARIA2_SECRET":          ("", "Aria2密钥"),
    "ARIA2_DOWNLOAD_DIR":    ("/downloads", "Aria2下载目录"),
    "NASTOOLS_BASE_URL":     ("http://192.168.3.31:3000", "NasTools地址"),
    "NASTOOLS_USERNAME":     ("admin", "NasTools用户名"),
    "NASTOOLS_PASSWORD":     ("", "NasTools密码"),
    "ALIST_BASE_URL":        ("http://192.168.3.31:5244", "AList地址"),
    "ALIST_TOKEN":           ("", "AList Token"),
    "PUSHPLUS_TOKEN":        ("", "PushPlus推送Token"),
}

class ConfigService:
    _cache: dict = {}
    _loaded: bool = False

    async def _load(self):
        if self._loaded:
            return
        async with async_session() as db:
            rows = (await db.execute(select(SystemConfig))).scalars().all()
            self._cache = {r.key: r.value for r in rows}
            self._loaded = True

    async def get(self, key: str) -> str:
        await self._load()
        if key in self._cache and self._cache[key]:
            return self._cache[key]
        return DEFAULT_CONFIGS.get(key, ("",))[0]

    async def set(self, key: str, value: str):
        async with async_session() as db:
            row = (await db.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one_or_none()
            if row:
                row.value = value
            else:
                db.add(SystemConfig(key=key, value=value))
            await db.commit()
        self._cache[key] = value

    async def get_all(self) -> dict:
        await self._load()
        return {k: {"value": self._cache.get(k, v[0]), "description": v[1]} for k, v in DEFAULT_CONFIGS.items()}

    async def seed(self):
        async with async_session() as db:
            for key, (val, desc) in DEFAULT_CONFIGS.items():
                if not (await db.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one_or_none():
                    db.add(SystemConfig(key=key, value=val, description=desc))
            await db.commit()
        self._loaded = False

    def clear(self):
        self._loaded = False
        self._cache = {}

config_service = ConfigService()

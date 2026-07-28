import uuid
import httpx
from app.services.config_service import config_service


class Aria2Service:
    async def _get_rpc_url(self) -> str:
        return await config_service.get("ARIA2_RPC_URL")

    async def _get_secret(self) -> str:
        return await config_service.get("ARIA2_SECRET")

    async def _rpc_call(self, method: str, params: list | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": str(uuid.uuid4()),
            "params": [f"token:{await self._get_secret()}"] + (params or []),
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(await self._get_rpc_url(), json=payload)
            resp.raise_for_status()
            return resp.json()

    async def add_uri(self, uris: list[str], dir_path: str | None = None,
                      filename: str | None = None) -> str:
        options = {}
        if dir_path: options["dir"] = dir_path
        if filename: options["out"] = filename
        result = await self._rpc_call("aria2.addUri", [uris, options])
        return result.get("result", "")

    async def tell_status(self, gid: str) -> dict:
        return (await self._rpc_call("aria2.tellStatus", [gid])).get("result", {})

    async def get_global_stat(self) -> dict:
        return (await self._rpc_call("aria2.getGlobalStat")).get("result", {})


aria2_service = Aria2Service()

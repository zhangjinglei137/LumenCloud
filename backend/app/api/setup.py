import secrets
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from app.database import async_session
from app.models import User
from app.services.config_service import config_service

router = APIRouter(prefix="/api/setup", tags=["setup"])

TEMP_ADMIN_ID = "__setup_admin__"

# ponytail: in-memory password store, fine for single-process setup flow
_temp_password: str = ""


class SetupStatus(BaseModel):
    needs_setup: bool
    temp_username: str = ""
    temp_password: str = ""


class SetupConfig(BaseModel):
    """管理员设置的配置项"""
    new_username: str = ""
    new_password: str = ""
    tmdb_api_key: str = ""
    emby_base_url: str = "http://192.168.3.31:8096"
    emby_api_key: str = ""
    cloudsaver_base_url: str = "http://192.168.3.31:8008"
    cloudsaver_username: str = "admin"
    cloudsaver_password: str = ""
    aria2_rpc_url: str = "http://192.168.3.31:6800/jsonrpc"
    aria2_secret: str = ""
    alist_base_url: str = "http://192.168.3.31:5244"
    alist_token: str = ""
    nastools_base_url: str = "http://192.168.3.31:3000"
    nastools_username: str = "admin"
    nastools_password: str = ""
    pushplus_token: str = ""


def _log_temp_credentials(password: str):
    log = logging.getLogger("uvicorn")
    log.warning("=" * 60)
    log.warning("  🎬 首次启动！系统就绪后请登录配置服务")
    log.warning("  管理员账号: admin")
    log.warning(f"  管理员密码: {password}")
    log.warning("=" * 60)


async def get_temp_admin():
    """获取或创建临时管理员，返回 (needs_setup, username, password)"""
    global _temp_password
    async with async_session() as db:
        admin = (await db.execute(select(User).where(User.id == TEMP_ADMIN_ID))).scalar_one_or_none()

        if admin:
            # 存在临时管理员 — 如果 emby_user_id 已更改则 setup 已完成
            if admin.emby_user_id != "__setup__":
                return False, "", ""
            # 进程重启后密码丢失则重新生成
            if not _temp_password:
                _temp_password = secrets.token_urlsafe(8)
                _log_temp_credentials(_temp_password)
            return True, admin.username, _temp_password

        # 检查是否有其他真实用户（非临时管理员）
        user_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
        if user_count > 0:
            return False, "", ""

        # 创建临时管理员
        _temp_password = secrets.token_urlsafe(8)
        admin = User(
            id=TEMP_ADMIN_ID,
            emby_user_id="__setup__",
            username="admin",
            is_admin=True,
        )
        db.add(admin)
        await db.commit()
        _log_temp_credentials(_temp_password)
        return True, "admin", _temp_password


@router.get("/status", response_model=SetupStatus)
async def setup_status():
    needs, username, password = await get_temp_admin()
    return SetupStatus(needs_setup=needs, temp_username=username, temp_password=password)


@router.post("/complete")
async def setup_complete(config: SetupConfig):
    """完成首次设置：更新管理员信息 + 保存所有服务配置"""
    needs, _, _ = await get_temp_admin()
    if not needs:
        raise HTTPException(status_code=400, detail="Setup already completed")

    async with async_session() as db:
        admin = (await db.execute(select(User).where(User.id == TEMP_ADMIN_ID))).scalar_one_or_none()
        if not admin:
            raise HTTPException(status_code=400, detail="No temp admin found")

        # 更新管理员信息 — emby_user_id 改变标记 setup 完成
        admin.emby_user_id = f"emby_{config.new_username or 'admin'}"
        admin.username = config.new_username or "admin"
        await db.commit()

    # 保存所有服务配置到数据库
    configs = {
        "TMDB_API_KEY": config.tmdb_api_key,
        "EMBY_BASE_URL": config.emby_base_url,
        "EMBY_API_KEY": config.emby_api_key,
        "CLOUDSAVER_BASE_URL": config.cloudsaver_base_url,
        "CLOUDSAVER_USERNAME": config.cloudsaver_username,
        "CLOUDSAVER_PASSWORD": config.cloudsaver_password,
        "ARIA2_RPC_URL": config.aria2_rpc_url,
        "ARIA2_SECRET": config.aria2_secret,
        "ALIST_BASE_URL": config.alist_base_url,
        "ALIST_TOKEN": config.alist_token,
        "NASTOOLS_BASE_URL": config.nastools_base_url,
        "NASTOOLS_USERNAME": config.nastools_username,
        "NASTOOLS_PASSWORD": config.nastools_password,
        "PUSHPLUS_TOKEN": config.pushplus_token,
    }
    for key, value in configs.items():
        if value:  # 只保存非空值
            await config_service.set(key, value)
    config_service.clear()

    return {"message": "Setup completed. Please login with your Emby account or the admin account."}

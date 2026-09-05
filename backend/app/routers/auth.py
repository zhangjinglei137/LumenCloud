"""认证 API（docs/新系统设计.md §5.2 + §9.1 邀请码消耗原子性）。

- POST /api/auth/register : 邀请码注册（校验码 + 标记 used + 建用户 同一事务）
- POST /api/auth/login    : 登录，签发 JWT
- GET  /api/auth/me       : 当前用户
- ensure_admin()          : 管理员初始化（幂等，main.py lifespan 调用）
"""
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from jose import jwt
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import InviteCode, User
from app.routers.deps import get_current_user, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# 密码哈希（bcrypt）
# ---------------------------------------------------------------------------
# 注：requirements.txt 的 passlib[bcrypt]==1.7.4 在 bcrypt 5.0.0 下不可用：
# passlib 内部用 >72 字节长密码探测 wrap bug，而 bcrypt 5.0 对此直接抛
# ValueError（passlib 1.7.4 未适配 bcrypt≥4.1）。故直接用 bcrypt 库实现，
# 行为与 CryptContext(schemes=['bcrypt']) 等价，接口不变。
_BCRYPT_MAX_BYTES = 72


def _pw_bytes(value: str) -> bytes:
    return value.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_pw_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_pw_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _now() -> datetime:
    """统一时间源（naive UTC），与 models server_default 一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_access_token(user: User) -> str:
    """签发 JWT（sub=user.id，携带 role，exp=JWT_EXPIRE_HOURS）。"""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {"sub": str(user.id), "role": user.role, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)  # 密码长度 ≥6（§5.2）
    invite_code: str = Field(min_length=1, max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.post("/register")
async def register(
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """邀请码注册（§9.1 原子性）：条件更新消耗码 + 建用户 在同一事务。

    条件更新 `UPDATE invite_codes SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL`
    捕获并发复用；行数=0 → 422 邀请码无效/已用。
    """
    username = payload.username.strip()
    invite_code = payload.invite_code.strip()

    # 用户名唯一预检（并发冲突由 UNIQUE 约束 + IntegrityError 兜底）
    existing = await session.scalar(select(User.id).where(User.username == username))
    if existing is not None:
        raise HTTPException(status_code=422, detail="用户名已存在")

    # 同一事务内完成「建用户 + 条件更新消耗邀请码」并一次 commit：
    # 条件更新 `UPDATE invite_codes SET used_by=? WHERE code=? AND used_by IS NULL`
    # 行数=0（无效/已用）→ 422 且整个事务回滚（用户也不创建），原子性见 §9.1
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        role="guest",
        invite_code=invite_code,
    )
    session.add(user)
    try:
        await session.flush()  # 拿 user.id
        result = await session.execute(
            update(InviteCode)
            .where(InviteCode.code == invite_code, InviteCode.used_by.is_(None))
            .values(used_by=user.id, used_at=_now())
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=422, detail="邀请码无效或已被使用")
        await session.commit()
    except IntegrityError:
        # 并发注册撞 UNIQUE(username) 兜底
        raise HTTPException(status_code=422, detail="用户名已存在")

    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/login")
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """登录：bcrypt 校验密码 → 签发 JWT。"""
    username = payload.username.strip()
    user = await session.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "username": user.username, "role": user.role},
    }


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    """当前登录用户信息。"""
    return {"id": user.id, "username": user.username, "role": user.role}


# ---------------------------------------------------------------------------
# 管理员初始化（幂等）
# ---------------------------------------------------------------------------

def _assert_secure_secrets() -> None:
    """启动护栏：弱默认密钥/口令拒绝启动（fail-closed，alpha P1-2 + beta P0-1）。

    默认 HS256 密钥公开，任何人可伪造 sub/role=admin 的 JWT 绕过全部鉴权；
    ensure_admin 会以默认口令落 admin 账号。故启动即校验：
    - JWT_SECRET        ≥16 字符且非默认 "change_me"
    - INIT_ADMIN_PASSWORD ≥8 字符且非默认 "change_me"
    由 main.py lifespan 在业务执行前调用（fail-fast）。
    """
    if (
        not settings.JWT_SECRET
        or settings.JWT_SECRET == "change_me"
        or len(settings.JWT_SECRET) < 16
    ):
        raise RuntimeError(
            "JWT_SECRET 未配置或过弱（需 ≥16 字符且非默认值），拒绝启动——请在 .env 配置强随机密钥"
        )
    if (
        not settings.INIT_ADMIN_PASSWORD
        or settings.INIT_ADMIN_PASSWORD == "change_me"
        or len(settings.INIT_ADMIN_PASSWORD) < 8
    ):
        raise RuntimeError(
            "INIT_ADMIN_PASSWORD 未配置或过弱（需 ≥8 字符且非默认值），拒绝启动——请在 .env 配置强口令"
        )


async def ensure_admin() -> None:
    """首次启动无 admin 用户时，用 INIT_ADMIN_USERNAME/INIT_ADMIN_PASSWORD 创建（§5.2）。

    幂等：已存在任一 admin → 直接返回；重复执行安全（并发时访问 IntegrityError）。
    """
    try:
        async with async_session() as session:
            exists = (
                await session.execute(select(User.id).where(User.role == "admin").limit(1))
            ).first()
            if exists:
                return
            username = (settings.INIT_ADMIN_USERNAME or "").strip() or "admin"
            password = settings.INIT_ADMIN_PASSWORD or "change_me"
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    role="admin",
                )
            )
            await session.commit()
            logger.info("已初始化管理员用户：%s", username)
    except IntegrityError:
        # 并发启动兜底（用户名撞 UNIQUE），幂等可接受
        logger.warning("[ensure_admin] 管理员创建冲突（并发），视为已存在")
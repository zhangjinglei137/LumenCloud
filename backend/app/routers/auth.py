"""认证 API（docs/新系统设计.md §5.2 + §9.1 邀请码消耗原子性 + Phase 8）。

- POST /api/auth/register       : 邀请码注册（校验码 + 标记 used + 建用户 同一事务）
- POST /api/auth/login          : 登录，签发 JWT
- GET  /api/auth/me             : 当前用户
- POST /api/auth/change-password: 登录用户修改自己密码（Phase 8，初始密码随机化配套）
- ensure_admin()                : 管理员初始化（幂等，Phase 8 起随机初始密码，
                                  main.py lifespan 调用）
"""
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import jwt
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import _JWT_SECRET, load_or_create_jwt_secret, settings
from app.database import async_session
from app.models import InviteCode, User
from app.routers.deps import get_current_user, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# 登录失败限流（安全加固，单 worker 进程内内存）
# ---------------------------------------------------------------------------
# 与 config_store 同风格注释：单 worker 部署（uvicorn --workers 1）下模块级 dict
# 读写原子性足够，不引入锁/外部存储。键 = f"{client.host}:{username}"，值 =
# 最近失败时间戳 deque。先判后记：窗口内失败数 >= LOGIN_FAIL_LIMIT 直接 429 且
# 不再记录（deque 天然封顶在 LIMIT，防单键高频灌入内存膨胀）；未超限才记录失败
# 事件并 401；校验成功清除该键（成功登录重置计数器）。
# 边界：_LOGIN_FAIL_MAX_KEYS 上限防恶意海量用户名撑爆内存——超限时清空整表并
# 告警（简单方案；攻击者需伪造大量不同用户名才触发，清空即失效，可接受）。
_LOGIN_FAIL_MAX_KEYS = 10000
_login_failures: dict[str, deque] = {}


def _record_login_failure(key: str) -> None:
    """记录一次登录失败事件（仅保留窗口内时间戳，超限清空全表防内存膨胀）。"""
    now = time.monotonic()
    if key not in _login_failures and len(_login_failures) >= _LOGIN_FAIL_MAX_KEYS:
        logger.warning("登录失败限流表超 %d 键，清空重建", _LOGIN_FAIL_MAX_KEYS)
        _login_failures.clear()
    dq = _login_failures.setdefault(key, deque())
    dq.append(now)
    cutoff = now - settings.LOGIN_FAIL_WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()


def _login_failure_count(key: str) -> int:
    """窗口内该键累计失败次数。"""
    return len(_login_failures.get(key, ()))


def _reset_login_failures(key: str) -> None:
    """登录成功清除该键（成功登录重置失败计数器）。"""
    _login_failures.pop(key, None)


def _login_rate_key(request: Request, username: str) -> str:
    """限流键：client IP + 用户名（client 缺失时兜底 "unknown"）。"""
    host = request.client.host if request.client is not None else "unknown"
    return f"{host}:{username}"

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
    """签发 JWT（sub=user.id，携带 role，exp=JWT_EXPIRE_HOURS）。

    Phase 8：签名密钥来自文件化 _JWT_SECRET（config 导入时已解析），
    与 deps 验签使用的同一密钥。
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {"sub": str(user.id), "role": user.role, "exp": expire}
    return jwt.encode(payload, _JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


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


# Phase 8：登录用户修改自己密码（初始密码随机化后的配套能力）
class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)  # 同 RegisterRequest（§5.2）


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
    request: Request,
    response: Response,
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """登录：bcrypt 校验密码 → 签发 JWT；失败限流 + httpOnly cookie 双通道。

    - 限流（Task 2）：先判后记——未超限先记录失败事件再 401；窗口内失败数
      超限直接 429（不再记录，deque 封顶防内存膨胀）；成功登录清除限流键。
    - cookie（Task 3）：成功响应附带 Set-Cookie（access_token，httpOnly），供
      浏览器后续无 Authorization header 的请求鉴权（deps.get_current_user 兜底）。
    """
    username = payload.username.strip()
    key = _login_rate_key(request, username)

    user = await session.scalar(select(User).where(User.username == username))
    if user is None or not verify_password(payload.password, user.password_hash):
        # 先判后记：未超限才记录失败事件（deque 封顶在 LOGIN_FAIL_LIMIT，
        # 防单键窗口内无界增长）；已超限直接 429，不再 append。
        if _login_failure_count(key) >= settings.LOGIN_FAIL_LIMIT:
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
        _record_login_failure(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 校验成功 → 清除该键（成功登录重置失败计数器）
    _reset_login_failures(key)

    token = create_access_token(user)
    # cookie 有效期与 JWT 一致：config 无 JWT_EXPIRE_MINUTES 字段，用现有
    # JWT_EXPIRE_HOURS（默认 168h = 7 天）换算为秒。
    max_age = settings.JWT_EXPIRE_HOURS * 3600
    response.set_cookie(
        key="access_token",
        value=token,
        path="/",
        httponly=True,
        samesite="lax",
        max_age=max_age,
        secure=settings.COOKIE_SECURE,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "username": user.username, "role": user.role},
    }


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    """当前登录用户信息。"""
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """登录用户修改自己的密码（Phase 8）。

    校验旧密码（bcrypt）→ 条件更新 `UPDATE users SET password_hash=? WHERE
    id=? AND password_hash=?` 防并发覆盖（行数=0 说明当前哈希已过期 → 409）。
    取舍：不改动 JWT——改密不吊销已签发 token（单用户场景换免重新登录体验；
    如需吊销可后续引入 token 版本号）。
    """
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="旧密码错误")

    new_hash = hash_password(payload.new_password)
    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.password_hash == user.password_hash)
        .values(password_hash=new_hash)
    )
    if result.rowcount == 0:
        # 并发窗口内他人已改密，本次提交基于过期哈希 → 拒绝
        raise HTTPException(status_code=409, detail="密码已变更，请刷新后重试")
    await session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 管理员初始化（幂等，Phase 8）
# ---------------------------------------------------------------------------

# Phase 8：初始密码字符集——剔除易混淆字符（0/O、1/l/I、o、8/B 附近等）
_ADMIN_PASSWORD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def _generate_random_password(length: int = 16) -> str:
    """生成 16 位随机初始密码（secrets 加密随机，避免易混淆字符）。"""
    return "".join(secrets.choice(_ADMIN_PASSWORD_CHARS) for _ in range(length))


def _assert_secure_secrets() -> None:
    """启动护栏（fail-closed，fail-fast，Phase 8）。

    旧版要求 env 提供 JWT_SECRET / INIT_ADMIN_PASSWORD 否则拒绝启动；Phase 8 起
    JWT 密钥自动文件化、admin 初始密码随机化，二者都不再要求 env。此处只校验
    实际可用的密钥强度：
    - .jwt_secret 文件正常 → load_or_create_jwt_secret 返回 64 位 hex，通过；
    - 密钥文件写入失败且 settings.JWT_SECRET 仍为默认 "change_me"（<16 字符）
      → 回退值过弱 → 拒绝启动（fail-closed）。
    由 main.py lifespan 在业务执行前调用（fail-fast）。
    """
    secret = load_or_create_jwt_secret(settings.LUMENCLOUD_DATA_DIR)
    if len(secret) < 16:
        raise RuntimeError(
            "JWT 密钥不可用：无法写入 <data_dir>/.jwt_secret 且 JWT_SECRET 环境变量未提供"
            "强随机值，拒绝启动——请检查数据目录写入权限"
        )


async def ensure_admin() -> Optional[str]:
    """首次启动无 admin 用户时创建管理员（Phase 8：随机 16 位初始密码）。

    幂等：已存在任一 admin → 返回 None；重复执行安全（并发撞 UNIQUE 由
    IntegrityError 兜底，返回 None）。首次创建时随机生成初始密码并 bcrypt
    入库，通过 logger.info 打印一次（含用户名/初始密码/登录提示）——这是用户
    唯一能拿到初始密码的渠道；同时返回该密码供调用方/测试确定性使用。
    """
    try:
        async with async_session() as session:
            exists = (
                await session.execute(select(User.id).where(User.role == "admin").limit(1))
            ).first()
            if exists:
                # 线上反馈（Q9）：用户「清空数据库」重启未看到新初始密码——大概率
                # 只清了业务表、users 表仍残留 admin 行，此处命中跳过分支且此前
                # 无任何日志，线上无从判断。显式打印「跳过」而非静默返回，便于判断
                # 是「未触发初始化」而非「初始化失败」。
                logger.info("[ensure_admin] 已存在管理员用户，跳过初始化")
                return None
            username = (settings.INIT_ADMIN_USERNAME or "").strip() or "admin"
            password = _generate_random_password()
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    role="admin",
                )
            )
            await session.commit()
            logger.info(
                "\n"
                "============================================================\n"
                "已初始化管理员账号（首次启动）\n"
                "  用户名: %s\n"
                "  初始密码: %s\n"
                "请立即登录 https://<host>:8000 并修改密码（/api/auth/change-password 或页面）\n"
                "============================================================",
                username,
                password,
            )
            return password
    except IntegrityError:
        # 并发启动兜底（用户名撞 UNIQUE），幂等可接受
        logger.warning("[ensure_admin] 管理员创建冲突（并发），视为已存在")
        return None
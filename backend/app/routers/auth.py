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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import _JWT_SECRET, load_or_create_jwt_secret, settings
from app.database import async_session
from app.models import InviteCode, User
from app.routers.deps import get_current_user, get_session
from app.services.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# 失败限流（安全加固，单 worker 进程内内存，Task B2 抽公共 RateLimiter）
# ---------------------------------------------------------------------------
# 单 worker 部署（uvicorn --workers 1）下模块级 dict 读写原子性足够，不引入
# 锁/外部存储（与 config_store 同风格注释；多 worker 需共享存储，属后续演进）。
#
# - 登录键 = f"{client.host}:{username}"：先判后记——未超限记录失败并 401，
#   窗口内失败数达 LOGIN_FAIL_LIMIT 直接 429 且不再记录（单键 deque 以
#   max_failures 封顶防内存膨胀）；成功登录 reset 清零。语义与迁移前一致。
# - 注册键 = client.host（审查 C1：防单 IP 高频枚举邀请码，邀请码
#   token_urlsafe(8) 64bit 熵单点爆破不可行，主要防高频探测与资源消耗）：
#   请求入口 check，邀请码无效/已用 hit，校验通过 reset。
# - 429 提示统一「尝试过于频繁，请稍后再试」，不泄露邀请码有效性差异。
_login_limiter = RateLimiter(
    max_failures=settings.LOGIN_FAIL_LIMIT,
    window_seconds=settings.LOGIN_FAIL_WINDOW_SECONDS,
)

# 注册邀请码失败限流：阈值/窗口独立于登录（注册攻击面 = 单 IP 枚举任意邀请码，
# 键用纯 IP；阈值 10 比登录 5 宽松——邀请码熵高，限流主为防资源消耗与误伤）。
_REGISTER_FAIL_LIMIT = 10
_REGISTER_FAIL_WINDOW_SECONDS = 300
_register_limiter = RateLimiter(
    max_failures=_REGISTER_FAIL_LIMIT,
    window_seconds=_REGISTER_FAIL_WINDOW_SECONDS,
)


def _client_host(request: Request) -> str:
    """客户端标识：client IP（client 缺失时兜底 "unknown"）。"""
    return request.client.host if request.client is not None else "unknown"


def _login_rate_key(request: Request, username: str) -> str:
    """登录限流键：client IP + 用户名。"""
    return f"{_client_host(request)}:{username}"

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


# Task B3：登录时序侧信道抹平（Design D3）——用户不存在时对固定 dummy hash
# 执行一次等价 bcrypt 校验（结果忽略），使「用户不存在」与「密码错误」的响应
# 时间一致，防止用户名枚举（审查 C2）。dummy hash 用与真实密码完全相同的
# hash_password 在模块加载时生成一次：bcrypt 默认 rounds（与 hash_password 的
# gensalt 一致），未来调整成本参数时此处自动跟随，不会产生成本漂移
# （低成本假 hash 会让时序差异仍可测，故不硬编码字符串）。
_DUMMY_PASSWORD_HASH = hash_password("lumencloud-timing-dummy")


def _now() -> datetime:
    """统一时间源（naive UTC），与 models server_default 一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_access_token(user: User) -> str:
    """签发 JWT（sub=user.id，role，ver=token_version，exp=JWT_EXPIRE_HOURS）。

    Phase 8：签名密钥来自文件化 _JWT_SECRET（config 导入时已解析），
    与 deps 验签使用的同一密钥。
    Task B1：payload 携带 `ver`（users.token_version），deps.get_current_user
    校验版本一致——改密时版本递增即吊销改密前签发的所有旧 token。
    """
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "ver": user.token_version,
        "exp": expire,
    }
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
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """邀请码注册（§9.1 原子性）：条件更新消耗码 + 建用户 在同一事务。

    条件更新 `UPDATE invite_codes SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL`
    捕获并发复用；行数=0 → 422 邀请码无效/已用。
    Task B2（邀请码爆破限流）：请求入口先查注册限流（键=client IP），窗口内
    无效邀请码失败达上限 → 429；邀请码校验失败 hit、校验通过 reset（成功即
    清零的温和策略，防误伤正常用户连续注册）。
    """
    username = payload.username.strip()
    invite_code = payload.invite_code.strip()

    # Task B2 限流预检（先判后记，与登录一致）：窗口内失败已达上限 → 429。
    # 429 提示与登录统一，不泄露邀请码有效性差异。
    key = _client_host(request)
    if not _register_limiter.check(key):
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")

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
            # 邀请码无效/已用 → 计一次失败（Task B2）
            _register_limiter.hit(key)
            raise HTTPException(status_code=422, detail="邀请码无效或已被使用")
        await session.commit()
    except IntegrityError:
        # 并发注册撞 UNIQUE(username) 兜底
        raise HTTPException(status_code=422, detail="用户名已存在")

    # 校验成功 → 清除该键（成功即清零，防误伤正常用户连续注册的温和策略）
    _register_limiter.reset(key)
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
    if user is None:
        # Task B3：用户不存在 → 对固定 dummy hash 执行一次等价校验（结果忽略），
        # 抹平与「密码错误」分支的 bcrypt 校验耗时差异（防用户名枚举，审查 C2）。
        # 仅存在于该分支，不影响成功路径性能；dummy 与真实密码同成本（见模块
        # 常量 _DUMMY_PASSWORD_HASH 注释）。
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        password_ok = False
    else:
        password_ok = verify_password(payload.password, user.password_hash)

    if not password_ok:
        # 先判后记（Task B2 迁移通用 RateLimiter 后语义不变）：未超限才记录失败
        # 事件（单键 deque 以 max_failures 封顶，防单键窗口内无界增长）；已超限
        # 直接 429，不再记录。
        if not _login_limiter.check(key):
            raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")
        _login_limiter.hit(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 校验成功 → 清除该键（成功登录重置失败计数器）
    _login_limiter.reset(key)

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


# ---------------------------------------------------------------------------
# 登出（Task B4 / Design D4：清除 httpOnly cookie 会话 + 吊销已签发令牌）
# ---------------------------------------------------------------------------

def _optional_token_payload(request: Request) -> Optional[dict]:
    """解析请求携带的 JWT（Authorization header 优先、httpOnly cookie 兜底）。

    与 deps.get_current_user 的双通道顺序一致，但**不抛错**：无 token / 非法
    token 一律返回 None——供登出幂等使用（未登录登出仅删 cookie，不吊销）。
    """
    token: Optional[str] = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        return jwt.decode(token, _JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """登出（幂等）：清除服务端 httpOnly cookie 会话 + 吊销该用户全部令牌。

    - cookie：delete_cookie 与登录 set_cookie 的 key（access_token）/ path（/）/
      httponly / samesite（lax）/ secure 完全一致——参数不一致浏览器不会删除；
      Max-Age=0 立即使 cookie 过期（Task B4，收敛「logout 后残留 cookie 仍可
      鉴权」缺口，审查 C5）。
    - 令牌吊销：JWT 无状态，仅删 cookie 无法让已签发 token 失效（残留/泄露的
      token 仍可鉴权）。复用 Task B1 的 token_version 机制：递增 users.
      token_version → 该用户所有已签发 JWT（payload.ver 落后于新版本）在
      get_current_user 校验时立即 401（spec「登出后令牌不再有效」场景：
      残留 cookie 请求受保护端点返回 401）。语义等价「登出即吊销该用户全部
      会话」，与改密吊销同构。
    - 幂等：无 token / token 非法 / 用户不存在 → 跳过吊销，恒 200（未登录登出
      不报错）。递增为 DB 原子自增（token_version + 1），并发登出不丢更新。
    """
    payload = _optional_token_payload(request)
    user_id: Optional[int] = None
    if payload is not None and payload.get("sub") is not None:
        try:
            user_id = int(payload["sub"])
        except (TypeError, ValueError):
            user_id = None
    if user_id is not None:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(token_version=User.token_version + 1)
        )
        await session.commit()

    response.delete_cookie(
        key="access_token",
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
    )
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """登录用户修改自己的密码（Phase 8）。

    校验旧密码（bcrypt）→ 条件更新 `UPDATE users SET password_hash=?,
    token_version=token_version+1 WHERE id=? AND password_hash=?` 防并发覆盖
    （行数=0 说明当前哈希已过期 → 409）。
    Task B1：改密事务内同时递增 token_version——与密码更新同一条 UPDATE 原子
    提交；此后改密前签发的旧 JWT（payload.ver 小于新版本）在鉴权时被吊销。
    """
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="旧密码错误")

    new_hash = hash_password(payload.new_password)
    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.password_hash == user.password_hash)
        .values(password_hash=new_hash, token_version=User.token_version + 1)
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

# Task B7（审查 C7）：首启初始凭据落盘文件名（<data_dir>/.initial_admin_credential，
# 与 .jwt_secret 同风格隐藏文件），一次性生成后已存在不覆盖（幂等）。
_INITIAL_ADMIN_CREDENTIAL_FILE = ".initial_admin_credential"


def _generate_random_password(length: int = 16) -> str:
    """生成 16 位随机初始密码（secrets 加密随机，避免易混淆字符）。"""
    return "".join(secrets.choice(_ADMIN_PASSWORD_CHARS) for _ in range(length))


def _persist_initial_admin_credential(
    data_dir: str, username: str, password: str
) -> Optional[Path]:
    """把首启初始凭据写入 <data_dir>/.initial_admin_credential（chmod 600）。

    - 文件已存在 → 不覆盖（幂等，保留首次内容），返回该路径；
    - 成功写入 → 返回路径；
    - I/O 失败 → warning 日志 + 返回 None（密码仍经 ensure_admin 返回值交给
      lifespan/调用方，文件是额外渠道，不阻断首次启动）。
    密码只落盘、绝不进日志（审查 C7）。
    """
    path = Path(data_dir) / _INITIAL_ADMIN_CREDENTIAL_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as f:  # "x" 独占创建：已存在抛 FileExistsError
            f.write(f"username: {username}\npassword: {password}\n")
        path.chmod(0o600)
        return path
    except FileExistsError:
        return path  # 幂等：已存在不覆盖（首次内容为准）
    except OSError as exc:
        logger.warning("初始管理员凭据文件 %s 写入失败（%s）", path, exc)
        return None


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
    入库，落盘到 <data_dir>/.initial_admin_credential（chmod 600，一次性凭据，
    Task B7 审查 C7：不再明文刷日志）——日志仅提示文件路径；同时返回该密码
    供调用方/lifespan 确定性使用。
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
            # Task B7（审查 C7）：初始密码不再明文刷日志——落盘一次性凭据文件
            # （已存在不覆盖，幂等），日志仅提示文件路径（密码同时随返回值交
            # 由 lifespan/调用方处理）。
            credential_path = _persist_initial_admin_credential(
                settings.LUMENCLOUD_DATA_DIR, username, password
            )
            if credential_path is not None:
                logger.info(
                    "\n"
                    "============================================================\n"
                    "已初始化管理员账号（首次启动）\n"
                    "  用户名: %s\n"
                    "  初始管理员凭据已写入: %s\n"
                    "请立即登录 https://<host>:8000 并修改密码"
                    "（/api/auth/change-password 或页面；修改密码后建议删除该凭据文件）\n"
                    "============================================================",
                    username,
                    credential_path,
                )
            return password
    except IntegrityError:
        # 并发启动兜底（用户名撞 UNIQUE），幂等可接受
        logger.warning("[ensure_admin] 管理员创建冲突（并发），视为已存在")
        return None
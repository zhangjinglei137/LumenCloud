"""FastAPI 依赖：数据库会话 + JWT 鉴权（阶段 3，docs/新系统设计.md §5/§9.1）。

- get_session       : 提供数据库会话
- get_current_user  : 解析 `Authorization: Bearer <token>` JWT（sub=user_id）
                      → 查询 users 表 → 返回 User ORM；token 缺失/非法/用户不存在 → 401
- get_current_admin : 依赖 get_current_user 后校验 role=='admin'，否则 403
                      （§9.1 写操作鉴权：retry/approve/invites/settings/media 增删改 强制 admin）
"""
import logging
import sys
from typing import AsyncGenerator, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import _JWT_SECRET, settings
from app.database import async_session
from app.models import User

logger = logging.getLogger(__name__)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """提供数据库会话依赖（会话生命周期由 async_sessionmaker 管理）。

    **只防护「获取 session」阶段**：构造/连接失败 → 503「数据库不可用」（而非裸
    500），与 /api/health 的 degraded 语义一致。yield 之后的请求处理阶段异常
    （含 FastAPI 经依赖 __aexit__ 注入的 RequestValidationError / 端点自身抛出的
    HTTPException）一律原样透传——若把 try 扩大包裹到整段，body 校验失败（422）
    会被误转成 503，掩蔽真实语义。
    """
    try:
        _session = async_session()  # async_sessionmaker 惰性构造，不建立连接
        await _session.__aenter__()  # 真正建连/绑事务的时机，失败在这捕获
    except Exception as exc:  # noqa: BLE001  DB 会话层故障 → 503
        logger.error("数据库会话获取失败（返回 503）: %s", exc)
        raise HTTPException(status_code=503, detail="数据库不可用") from exc
    try:
        yield _session
    finally:
        # 等价于 async with 的退出语义：正常路径 close，异常路径携带 exc_info
        # 回滚并继续传播（不吞异常、不改变语义）
        await _session.__aexit__(*sys.exc_info())


# HTTPBearer：auto_error=False → 缺少/格式错误的 Authorization 头返回 None，
# 由 get_current_user 统一抛 401（HTTPBearer 默认 auto_error=True 抛 403，不符合契约要求的 401）。
bearer_scheme = HTTPBearer(auto_error=False)


async def _decode_user_id(token: str) -> Optional[int]:
    """解析并校验 JWT，返回 sub（user_id）；任何非法输入返回 None。

    Phase 8：使用文件化密钥 _JWT_SECRET 验签（与 auth.create_access_token 一致）。
    """
    try:
        payload = jwt.decode(
            token,
            _JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        return None
    sub = payload.get("sub")
    if sub is None:
        return None
    try:
        return int(sub)
    except (TypeError, ValueError):
        return None


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """JWT 鉴权依赖：token 缺失/非法/用户不存在 → 401。

    Task 3 双通道（渐进式）：Authorization header 优先；header 缺失时回退
    httpOnly cookie `access_token`（login 时 Set-Cookie）。header 优先于
    cookie——同时存在时以 header 为准，保持既有 401 语义。
    """
    token = None
    if credentials is not None:
        token = credentials.credentials
    else:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="未提供身份令牌")

    user_id = await _decode_user_id(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    return user


async def get_current_admin(user: User = Depends(get_current_user)) -> User:
    """管理员依赖：非 admin → 403（§9.1 写操作鉴权）。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
"""管理员接口（admin，docs/新系统设计.md §5.2）。

邀请码管理：
- GET    /api/admin/invites            邀请码列表 + 使用状态
- POST   /api/admin/invites            生成 N 个随机邀请码（默认 5）
- DELETE /api/admin/invites/{code}     删除未使用的邀请码（已用 → 409）

Q11 用户管理：
- GET    /api/admin/users              用户列表
- PATCH  /api/admin/users/{user_id}    修改角色（admin/guest；禁自改、至少留 1 admin）
- DELETE /api/admin/users/{user_id}    删除用户（禁自删、至少留 1 admin、有引用禁删）
"""
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InviteCode, Notification, User, WatchRequest
from app.routers.deps import get_current_admin, get_session

router = APIRouter(prefix="/admin", tags=["admin"])


class InviteCreate(BaseModel):
    count: int = Field(default=5, ge=1, le=50)


@router.get("/invites")
async def list_invites(
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """邀请码列表（code/used_by/used_at/created_at）。

    Q6：LEFT JOIN users 取使用人 username（used_by_username；无关联 → None）。
    """
    rows = (
        (
            await session.execute(
                select(InviteCode, User.username)
                .join(User, InviteCode.used_by == User.id, isouter=True)
                .order_by(InviteCode.created_at.desc(), InviteCode.code)
            )
        )
        .all()
    )
    return [
        {
            "code": r[0].code,
            "used_by": r[0].used_by,
            "used_by_username": r[1],
            "used_at": r[0].used_at,
            "created_at": r[0].created_at,
        }
        for r in rows
    ]


@router.post("/invites")
async def create_invites(
    payload: InviteCreate,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """生成 N 个随机邀请码（secrets.token_urlsafe(8)），created_by=当前 admin。"""
    codes = [secrets.token_urlsafe(8) for _ in range(payload.count)]
    for code in codes:
        session.add(InviteCode(code=code, created_by=admin.id))
    await session.commit()
    return {"codes": codes}


@router.delete("/invites/{code}")
async def delete_invite(
    code: str,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """删除未使用的邀请码；已被使用 → 409。"""
    row = await session.get(InviteCode, code)
    if row is None:
        raise HTTPException(status_code=404, detail="邀请码不存在")
    if row.used_by is not None:
        raise HTTPException(status_code=409, detail="邀请码已被使用，不能删除")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@router.get("/users")
async def list_users(
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """用户列表（Q11）：id/username/role/invite_code/created_at，created_at desc + id desc。"""
    rows = (
        (
            await session.execute(
                select(User).order_by(User.created_at.desc(), User.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": r.id,
            "username": r.username,
            "role": r.role,
            "invite_code": r.invite_code,
            "created_at": r.created_at,
        }
        for r in rows
    ]


class RoleUpdate(BaseModel):
    """角色修改请求体；Literal 限制 admin/guest，非法取值由 pydantic → 422。"""

    role: Literal["admin", "guest"]


async def _admin_count(session: AsyncSession) -> int:
    """当前 admin 总数（角色保护用，PATCH/DELETE 共用模块级 helper）。"""
    return (
        await session.scalar(select(func.count(User.id)).where(User.role == "admin"))
    ) or 0


@router.patch("/users/{user_id}")
async def update_user_role(
    user_id: int,
    payload: RoleUpdate,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """修改用户角色（Q11）。

    保护规则：
    - 目标不存在 → 404
    - 目标是自己 → 409（避免管理员自降级后 JWT role 即时失效的复杂状态）
    - 目标是 admin 且当前 admin 总数 == 1 → 409（兜底：调用者本身是 admin，
      总数 == 1 时目标必为自己，已被上面的自改拦截，属冗余保险）
    - 目标已是该 role → 幂等直接返回 {"ok": True}
    """
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="不能修改自己的角色")
    if user.role == "admin" and (await _admin_count(session)) == 1:
        raise HTTPException(status_code=409, detail="至少保留一个管理员")
    if user.role == payload.role:  # 幂等：目标已是该角色，重复请求直接成功
        return {"ok": True}
    user.role = payload.role
    await session.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """删除用户（Q11）。

    保护规则：
    - 目标不存在 → 404
    - 自删 → 409
    - 目标是 admin 且当前 admin 总数 <= 1 → 409（兜底：调用者本身是 admin，
      目标非自己时总数 >= 2，实际由自删分支覆盖）
    - 有任一关联引用（watch_requests/notifications/invite_codes）→ 409，保守不级联
    """
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="不能删除当前登录账号")
    if user.role == "admin" and (await _admin_count(session)) <= 1:
        raise HTTPException(status_code=409, detail="至少保留一个管理员")

    # 关联引用检查（任一命中即拒绝；保守不级联删除）
    refs = [
        select(WatchRequest.id).where(WatchRequest.requested_by == user_id),
        select(Notification.id).where(Notification.recipient == user_id),
        select(InviteCode.code).where(
            or_(InviteCode.used_by == user_id, InviteCode.created_by == user_id)
        ),
    ]
    for ref in refs:
        if (await session.scalar(ref.limit(1))) is not None:
            raise HTTPException(
                status_code=409,
                detail="该用户存在关联记录（审批/通知/邀请码），暂不可删除",
            )

    await session.delete(user)
    await session.commit()
    return {"ok": True}
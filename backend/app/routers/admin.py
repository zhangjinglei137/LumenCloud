"""管理员邀请码管理 API（admin，docs/新系统设计.md §5.2）。

- GET    /api/admin/invites            邀请码列表 + 使用状态
- POST   /api/admin/invites            生成 N 个随机邀请码（默认 5）
- DELETE /api/admin/invites/{code}     删除未使用的邀请码（已用 → 409）
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InviteCode, User
from app.routers.deps import get_current_admin, get_session

router = APIRouter(prefix="/admin", tags=["admin"])


class InviteCreate(BaseModel):
    count: int = Field(default=5, ge=1, le=50)


@router.get("/invites")
async def list_invites(
    admin: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """邀请码列表（code/used_by/used_at/created_at）。"""
    rows = (
        (
            await session.execute(
                select(InviteCode).order_by(
                    InviteCode.created_at.desc(), InviteCode.code
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "code": r.code,
            "used_by": r.used_by,
            "used_at": r.used_at,
            "created_at": r.created_at,
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
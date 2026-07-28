from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import User
from app.schemas.auth import LoginRequest, TokenResponse, UserInfo
from app.api.deps import create_jwt, get_current_user
from app.services.emby import emby_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    emby_result = await emby_service.authenticate_user(req.username, req.password)
    if emby_result is None:
        raise HTTPException(status_code=401, detail="Emby authentication failed")

    emby_user = emby_result.get("User", {})
    emby_user_id = emby_user.get("Id")
    emby_username = emby_user.get("Name", req.username)

    result = await db.execute(select(User).where(User.emby_user_id == emby_user_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(emby_user_id=emby_user_id, username=emby_username, is_admin=False)
        db.add(user)
        await db.flush()

    jwt_token = create_jwt(user.id)

    return TokenResponse(
        access_token=jwt_token,
        user=UserInfo(
            id=user.id,
            username=user.username,
            emby_user_id=user.emby_user_id,
            is_admin=user.is_admin,
        ),
    )


@router.get("/me", response_model=UserInfo)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        emby_user_id=current_user.emby_user_id,
        is_admin=current_user.is_admin,
    )

"""审批流 API（docs/新系统设计.md §5.3 审批流 + §9.1 写操作鉴权）。

- GET  /api/approvals            admin 看全部；guest 看自己的（requested_by=当前用户）
- POST /api/approvals            guest+admin 提交「想看」→ pending + 通知管理员
- POST /api/approvals/{id}/approve  admin 批准 → 写 media 表(status=tracking) + 通知访客 + 可选触发巡检
- POST /api/approvals/{id}/reject   admin 拒绝 → rejected + reject_reason
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Media, User, WatchRequest
from app.routers.deps import get_current_admin, get_current_user, get_session
from app.services import emby
from app.services.emby import EmbyUnavailable
from app.services.notifier import (
    EVENT_APPROVAL_PENDING,
    NotifyEvent,
    notifier,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_unique_violation(exc: IntegrityError) -> bool:
    """区分 UNIQUE 约束冲突与其他 IntegrityError（FK/NOT NULL 等）。

    只对 UNIQUE 冲突映射 409，其余重新抛出——避免捕获过宽掩盖真实 DB 错误。
    - PostgreSQL（psycopg2 / asyncpg）：sqlstate / pgcode 23505 = unique_violation
    - SQLite：错误信息含 "UNIQUE constraint failed"
    """
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if code == "23505":
        return True
    return "UNIQUE constraint failed" in str(orig)


class WatchRequestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    tmdb_id: int | None = None
    media_type: str | None = None  # movie / tv
    poster_path: str | None = None


class RejectRequest(BaseModel):
    reject_reason: str = Field(default="", max_length=500)


def _wr_dto(r: WatchRequest, requested_by_username: str | None = None) -> dict:
    """审批 DTO：保留 requested_by(id) 字段，附加 request_by_username（Q10，
    申请人已删除/无关联时 LEFT JOIN 得 None，前端回退 id）。"""
    return {
        "id": r.id,
        "title": r.title,
        "tmdb_id": r.tmdb_id,
        "media_type": r.media_type,
        "poster_path": r.poster_path,
        "status": r.status,
        "reject_reason": r.reject_reason,
        "reviewed_at": r.reviewed_at,
        "created_at": r.created_at,
        "requested_by": r.requested_by,
        "requested_by_username": requested_by_username,
    }


@router.get("")
async def list_approvals(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """审批列表：admin 看全部；guest 只看自己提交的。

    Q10：LEFT JOIN users 取申请人 username（outer join，不留申请人对不上不报错）。
    """
    stmt = (
        select(WatchRequest, User.username)
        .join(User, WatchRequest.requested_by == User.id, isouter=True)
        .order_by(WatchRequest.created_at.desc(), WatchRequest.id.desc())
    )
    if user.role != "admin":
        stmt = stmt.where(WatchRequest.requested_by == user.id)
    rows = (await session.execute(stmt)).all()
    return [_wr_dto(r[0], r[1]) for r in rows]


@router.post("")
async def create_approval(
    payload: WatchRequestCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """提交「想看」请求（guest+admin 均可）→ 通知管理员（§5.3 提交即通知）。"""
    if payload.media_type not in (None, "movie", "tv"):
        raise HTTPException(status_code=422, detail="media_type 仅支持 movie/tv")

    # Q2①（P1）：已存在于影视库的 tmdb_id 拒绝重复提交（应用层去重）
    if payload.tmdb_id is not None and await session.scalar(
        select(Media.id).where(Media.tmdb_id == payload.tmdb_id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="该影视已在影视库，无需重复提交")

    # 需求 4（P1-1）：Emby 防重（本地查重之后、写库之前）——该影视已在 Emby
    # 媒体库则拒绝重复提交，与「已在影视库」文案区分。Emby 故障（EmbyUnavailable，
    # 含「未配置」）fail-open：仅告警放行，不阻断用户提交。
    if payload.tmdb_id is not None:
        try:
            emby_id = await emby.find_emby_id(payload.tmdb_id, payload.title)
        except EmbyUnavailable as exc:
            logger.warning(
                "[approvals] Emby 防重检查不可用（fail-open 放行）tmdb=%s: %s",
                payload.tmdb_id, exc,
            )
        else:
            if emby_id is not None:
                raise HTTPException(
                    status_code=409, detail="该影视已在 Emby 媒体库，无需重复订阅"
                )

    wr = WatchRequest(
        requested_by=user.id,
        title=payload.title.strip(),
        tmdb_id=payload.tmdb_id,
        media_type=payload.media_type,
        poster_path=payload.poster_path,
        status="pending",
    )
    session.add(wr)
    await session.commit()
    await session.refresh(wr)

    # §5.3：访客提交即触发 approval_pending 通知管理员（recipient=None=全体）
    try:
        await notifier.notify(
            NotifyEvent(
                event_type=EVENT_APPROVAL_PENDING,
                title=f"新的想看请求: {wr.title}",
                body=f"wr#{wr.id} {wr.title}",
            )
        )
    except Exception as exc:  # noqa: BLE001  通知失败不阻断提交
        logger.exception("审批待办通知失败: %s", exc)

    return {"id": wr.id}


@router.post("/{approval_id}/approve")
async def approve_approval(
    approval_id: int,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 批准：条件更新 pending→approved → 写 media 表 → 通知访客 → 可选触发巡检。"""
    wr = await session.get(WatchRequest, approval_id)
    if wr is None:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    if wr.status != "pending":
        raise HTTPException(status_code=409, detail="该请求已被处理")

    # Q2①（P1）：审批尚未被消费前查重——已存在于影视库的 tmdb_id 拒绝批准，
    # 不产生半提交，管理员可另行 reject
    if wr.tmdb_id is not None and await session.scalar(
        select(Media.id).where(Media.tmdb_id == wr.tmdb_id).limit(1)
    ):
        raise HTTPException(status_code=409, detail="该影视已在影视库，无需重复提交")

    # C11（审查 C15）：tmdb_id 缺失（非 TMDB 条目）时以 title 大小写不敏感
    # 精确匹配兜底查重——同名条目不重复入库。仅兜底 tmdb_id=None 路径；
    # tmdb_id 非空路径保持上面原逻辑。用 func.lower 相等比较（SQLite/
    # PostgreSQL 均编译为 LOWER()），避免 ilike 的 %/_ 通配符误匹配。
    if wr.tmdb_id is None and await session.scalar(
        select(Media.id)
        .where(func.lower(Media.title) == wr.title.strip().lower())
        .limit(1)
    ):
        raise HTTPException(status_code=409, detail="该影视已在影视库，无需重复提交")

    # 需求 4（P1-1）：Emby 防重（本地查重之后、条件更新消费之前）——该影视已在
    # Emby 媒体库则拒绝批准，wr 保持 pending，管理员可另行 reject。Emby 故障
    # （EmbyUnavailable，含「未配置」）fail-open：仅告警放行，不阻断审批。
    if wr.tmdb_id is not None:
        try:
            emby_id = await emby.find_emby_id(wr.tmdb_id, wr.title)
        except EmbyUnavailable as exc:
            logger.warning(
                "[approvals] Emby 防重检查不可用（fail-open 放行）tmdb=%s: %s",
                wr.tmdb_id, exc,
            )
        else:
            if emby_id is not None:
                raise HTTPException(
                    status_code=409, detail="该影视已在 Emby 媒体库，无需重复订阅"
                )

    # 条件更新防并发双重审批（§3.1 条件更新约定）；异常未 commit 时整体回滚
    result = await session.execute(
        update(WatchRequest)
        .where(WatchRequest.id == approval_id, WatchRequest.status == "pending")
        .values(status="approved", reviewed_by=admin.id, reviewed_at=_now())
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=409, detail="该请求已被处理")

    media = Media(
        title=wr.title,
        tmdb_id=wr.tmdb_id,
        media_type=wr.media_type,
        # Q2 配套：访客「想看」携带的 poster_path 在批准入库时透传（此前丢失，
        # 批准后影视无海报；与 MediaCreate 的 poster_path 契约一致）
        poster_path=wr.poster_path,
        status="tracking",
        in_emby=False,
    )
    session.add(media)
    try:
        await session.flush()  # 撞 Media.tmdb_id UNIQUE 在这里抛出
        media_id = media.id
        await session.commit()
    except IntegrityError as exc:
        # 并发审批兜底（审查 C8）：两个 admin 并发批准引用同一 tmdb_id 的不同
        # wr 时，查重都在对方 commit 前通过 → 后提交者 flush 撞 UNIQUE。
        # 事务回滚使 wr 的 pending→approved 更新一并撤销（保持 pending，可再次
        # 尝试），返回 409「该影视已在影视库」替代裸 500。只对 UNIQUE 冲突 409，
        # 其余 IntegrityError 重新抛出（避免掩盖 FK/NOT NULL 等真实 DB 错误）。
        if not _is_unique_violation(exc):
            raise
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="该影视已在影视库，无需重复提交"
        ) from exc

    # ---- 事务外副作用 ----
    # 可选：触发该 media 巡检（fire-and-forget，E-1 不再同步等待；故障不影响审批结果）
    try:
        from app.tasks.scan import trigger_scan_background

        trigger_scan_background(media_id, manual=True)  # 审批通过=用户意图：绕过静默期
    except Exception as exc:  # noqa: BLE001
        logger.warning("批准后自动巡检触发失败 media=%s: %s", media_id, exc)

    return {"ok": True, "media_id": media_id}


@router.post("/{approval_id}/reject")
async def reject_approval(
    approval_id: int,
    payload: RejectRequest,
    admin: User = Depends(get_current_admin),  # §9.1 写操作鉴权
    session: AsyncSession = Depends(get_session),
) -> dict:
    """admin 拒绝：pending→rejected + reject_reason + reviewed_by/reviewed_at。"""
    result = await session.execute(
        update(WatchRequest)
        .where(WatchRequest.id == approval_id, WatchRequest.status == "pending")
        .values(
            status="rejected",
            reject_reason=payload.reject_reason.strip() or None,
            reviewed_by=admin.id,
            reviewed_at=_now(),
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="请求不存在或已被处理")
    await session.commit()
    return {"ok": True}
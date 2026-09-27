from __future__ import annotations
import uuid
from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import verify_village_scope
from app.models.blacklist import Blacklist
from app.models.whitelist import Whitelist
from app.models.user import User
from app.schemas.common import PaginatedResponse
from app.schemas.whitelist import WhitelistCreate, WhitelistRead, WhitelistUpdate
from app.services import audit_service
from app.core.error_messages import WhitelistErrors


from app.core.scope_utils import resolve_village_id, build_scope_filters
from app.core.db_utils import escape_like

async def _get_entry_or_404(db: AsyncSession, entry_id: uuid.UUID) -> Whitelist:
    result = await db.execute(select(Whitelist).where(Whitelist.id == entry_id))
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WhitelistErrors.NOT_FOUND)
    return entry


async def _check_not_blacklisted(
    db: AsyncSession,
    village_id: uuid.UUID,
    license_plate: str,
    province: str,
) -> None:
    result = await db.execute(
        select(Blacklist.id)
        .where(
            Blacklist.village_id == village_id,
            Blacklist.license_plate == license_plate,
            Blacklist.province == province,
        )
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=WhitelistErrors.PLATE_IS_BLACKLISTED,
        )


def _describe_entry(name: str, house_no: str | None, license_plate: str, province: str) -> str:
    return f"{name} (house {house_no or '-'}) - {license_plate} ({province})"


async def create_whitelist_entry(
    db: AsyncSession,
    request: Request,
    current_user: User,
    payload: WhitelistCreate,
) -> WhitelistRead:
    village_id = await resolve_village_id(db, current_user, payload.village_id)
    await _check_not_blacklisted(db, village_id, payload.license_plate, payload.province)

    existing = await db.execute(
        select(Whitelist.id).where(
            Whitelist.village_id == village_id,
            Whitelist.license_plate == payload.license_plate,
            Whitelist.province == payload.province,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ป้ายทะเบียนนี้มีอยู่ในบัญชีขาวของหมู่บ้านนี้แล้ว",
        )

    entry = Whitelist(
        village_id=village_id,
        name=payload.name,
        house_no=payload.house_no,
        phone=payload.phone,
        license_plate=payload.license_plate,
        province=payload.province,
        color=payload.color,
        note=payload.note,
        added_by=current_user.id,
    )
    db.add(entry)

    await audit_service.log_action(
        db,
        request,
        action="whitelist_create",
        detail=f"added whitelist entry: {_describe_entry(payload.name, payload.house_no, payload.license_plate, payload.province)}",
        user_id=current_user.id,
        village_id=village_id,
    )

    await db.commit()
    await db.refresh(entry)
    return WhitelistRead.model_validate(entry)


async def list_whitelist_entries(
    db: AsyncSession,
    current_user: User,
    village_id: uuid.UUID | None,
    name: str | None,
    house_no: str | None,
    license_plate: str | None,
    province: str | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[WhitelistRead]:
    scope_filters = build_scope_filters(current_user, village_id, Whitelist)
    stmt = select(Whitelist).where(*scope_filters)

    if name is not None:
        stmt = stmt.where(Whitelist.name.ilike(f"%{escape_like(name)}%", escape="\\"))
    if house_no is not None:
        stmt = stmt.where(Whitelist.house_no.ilike(f"%{escape_like(house_no)}%", escape="\\"))
    if license_plate is not None:
        stmt = stmt.where(Whitelist.license_plate.ilike(f"%{escape_like(license_plate)}%", escape="\\"))
    if province is not None:
        stmt = stmt.where(Whitelist.province == province)

    count_stmt = stmt.with_only_columns(func.count()).order_by(None)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    stmt = (
        stmt.order_by(Whitelist.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(stmt)
    items = result.scalars().all()

    return PaginatedResponse[WhitelistRead](
        items=[WhitelistRead.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


async def update_whitelist_entry(
    db: AsyncSession,
    request: Request,
    current_user: User,
    entry_id: uuid.UUID,
    payload: WhitelistUpdate,
) -> WhitelistRead:
    entry = await _get_entry_or_404(db, entry_id)
    verify_village_scope(current_user, entry.village_id)

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(entry, field, value)

    await _check_not_blacklisted(db, entry.village_id, entry.license_plate, entry.province)

    await audit_service.log_action(
        db,
        request,
        action="whitelist_update",
        detail=f"updated whitelist entry: {_describe_entry(entry.name, entry.house_no, entry.license_plate, entry.province)}",
        user_id=current_user.id,
        village_id=entry.village_id,
    )

    await db.commit()
    await db.refresh(entry)
    return WhitelistRead.model_validate(entry)


async def delete_whitelist_entry(
    db: AsyncSession,
    request: Request,
    current_user: User,
    entry_id: uuid.UUID,
) -> None:
    entry = await _get_entry_or_404(db, entry_id)
    verify_village_scope(current_user, entry.village_id)

    await audit_service.log_action(
        db,
        request,
        action="whitelist_delete",
        detail=f"removed whitelist entry: {_describe_entry(entry.name, entry.house_no, entry.license_plate, entry.province)}",
        user_id=current_user.id,
        village_id=entry.village_id,
    )

    await db.delete(entry)
    await db.commit()
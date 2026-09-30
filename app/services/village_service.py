from __future__ import annotations
import uuid
from fastapi import BackgroundTasks, HTTPException, Request, status
from sqlalchemy import func, select, delete, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.blacklist import Blacklist
from app.models.camera import Camera
from app.models.group import Group
from app.models.user import User, UserRole
from app.models.whitelist import Whitelist
from app.schemas.camera import CameraBasicRead
from app.schemas.common import PaginatedResponse
from app.schemas.village import VillageCreate, VillageDetailRead, VillageMemberSummary, VillageUpdate
from app.services import audit_service, camera_service
from app.core.error_messages import Common, VillageErrors
from app.core.db_utils import escape_like

async def create_village(
    db: AsyncSession,
    request: Request,
    current_user: User,
    payload: VillageCreate,
) -> Group:
    village = Group(name=payload.name, address=payload.address, is_active=True)
    db.add(village)
    await db.flush()

    await audit_service.log_action(
        db,
        request,
        action="village_created",
        detail=f"village created: {village.name}",
        user_id=current_user.id,
        village_id=village.id,
    )
    await db.commit()
    await db.refresh(village)
    return village


async def get_village(db: AsyncSession, village_id: uuid.UUID) -> Group:
    result = await db.execute(select(Group).where(Group.id == village_id))
    village = result.scalar_one_or_none()

    if village is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=VillageErrors.NOT_FOUND)

    return village


async def get_village_detail(
    db: AsyncSession,
    current_user: User,
    village_id: uuid.UUID,
) -> VillageDetailRead:
    village = await get_village(db, village_id)

    if current_user.role != UserRole.SUPERADMIN and current_user.village_id != village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ACCESS_DENIED,
        )

    camera_result = await db.execute(
        select(Camera)
        .where(Camera.village_id == village_id)
        .order_by(Camera.created_at.desc())
    )
    cameras = camera_result.scalars().all()

    member_result = await db.execute(
        select(User).where(User.village_id == village_id).order_by(User.created_at.desc())
    )
    members = member_result.scalars().all()

    return VillageDetailRead(
        id=village.id,
        name=village.name,
        address=village.address,
        is_active=village.is_active,
        created_at=village.created_at,
        cameras=[CameraBasicRead.model_validate(camera) for camera in cameras],
        members=[VillageMemberSummary.model_validate(member) for member in members],
    )


async def list_villages(
    db: AsyncSession,
    current_user: User,
    is_active_filter: bool | None,
    search: str | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[Group]:
    stmt = select(Group)
    count_stmt = select(func.count()).select_from(Group)

    if current_user.role != UserRole.SUPERADMIN:
        stmt = stmt.where(Group.id == current_user.village_id)
        count_stmt = count_stmt.where(Group.id == current_user.village_id)

    if is_active_filter is not None:
        stmt = stmt.where(Group.is_active == is_active_filter)
        count_stmt = count_stmt.where(Group.is_active == is_active_filter)

    if search:
        stmt = stmt.where(Group.name.ilike(f"%{escape_like(search)}%", escape="\\"))
        count_stmt = count_stmt.where(Group.name.ilike(f"%{escape_like(search)}%", escape="\\"))

    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    stmt = stmt.order_by(Group.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    items = list(result.scalars().all())

    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


async def update_village(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    village_id: uuid.UUID,
    payload: VillageUpdate,
) -> Group:
    village = await get_village(db, village_id)

    update_data = payload.model_dump(exclude_unset=True, exclude_none=True)
    previous_is_active = village.is_active

    for field, value in update_data.items():
        setattr(village, field, value)

    action = "village_updated"
    detail = f"village updated: {village.name}"
    is_active_changed = "is_active" in update_data and update_data["is_active"] != previous_is_active

    cascaded_camera_ids: list[uuid.UUID] = []

    if is_active_changed:
        if update_data["is_active"]:
            action = "village_activated"
            detail = f"village activated: {village.name}"

            cascaded_camera_ids = await camera_service.cascade_reactivate_village_cameras(db, village.id)
            if cascaded_camera_ids:
                await audit_service.log_action(
                    db,
                    request,
                    action="village_cameras_reactivated",
                    detail=f"reactivated {len(cascaded_camera_ids)} camera(s) after village reactivation",
                    user_id=current_user.id,
                    village_id=village.id,
                )
        else:
            action = "village_deactivated"
            detail = f"village deactivated: {village.name}"

            cascaded_camera_ids = await camera_service.cascade_deactivate_village_cameras(db, village.id)
            if cascaded_camera_ids:
                await audit_service.log_action(
                    db,
                    request,
                    action="village_cameras_deactivated",
                    detail=f"deactivated {len(cascaded_camera_ids)} camera(s) due to village deactivation",
                    user_id=current_user.id,
                    village_id=village.id,
                )

    await audit_service.log_action(
        db,
        request,
        action=action,
        detail=detail,
        user_id=current_user.id,
        village_id=village.id,
    )
    await db.commit()
    await db.refresh(village)

    if is_active_changed:
        if update_data["is_active"]:
            background_tasks.add_task(
                camera_service.push_cameras_online, village.id, cascaded_camera_ids
            )
        else:
            background_tasks.add_task(
                camera_service.push_cameras_offline, village.id, cascaded_camera_ids
            )

    return village


async def delete_village(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    village_id: uuid.UUID,
) -> None:
    village = await get_village(db, village_id)

    # Fetch cameras before deleting to sync with AI Vision
    cameras_result = await db.execute(select(Camera).where(Camera.village_id == village_id))
    cameras_to_delete = cameras_result.scalars().all()
    camera_sync_tasks = []
    for cam in cameras_to_delete:
        camera_sync_tasks.append((cam.id, cam.village_id, cam.name))

    village_name = village.name

    # ดึง User ทั้งหมดในหมู่บ้าน
    users_result = await db.execute(select(User.id).where(User.village_id == village_id))
    user_ids = users_result.scalars().all()
    
    if user_ids:
        from app.models.contact import Contact
        from app.models.notification import Notification
        from app.models.refresh_token import RefreshToken
        # ลบข้อมูลที่ผูกกับ User ก่อนลบ User
        await db.execute(delete(Contact).where(Contact.user_id.in_(user_ids)))
        await db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
        await db.execute(delete(RefreshToken).where(RefreshToken.user_id.in_(user_ids)))

    # Manually cascade delete to prevent ForeignKey constraint errors
    await db.execute(delete(Blacklist).where(Blacklist.village_id == village_id))
    await db.execute(delete(Whitelist).where(Whitelist.village_id == village_id))
    await db.execute(delete(Camera).where(Camera.village_id == village_id))
    
    # เคลียร์ Foreign Key ใน AuditLog ที่อาจค้างอยู่
    from sqlalchemy import update
    from app.models.audit_log import AuditLog
    await db.execute(update(AuditLog).where(AuditLog.village_id == village_id).values(village_id=None))
    if user_ids:
        await db.execute(update(AuditLog).where(AuditLog.user_id.in_(user_ids)).values(user_id=None))
        
    await db.execute(delete(User).where(User.village_id == village_id))

    await audit_service.log_action(
        db,
        request,
        action="village_deleted",
        detail=f"permanently deleted village (detection history retained as anonymized stats): {village_name}",
        user_id=current_user.id,
        village_id=None,
    )

    await db.delete(village)
    await db.commit()

    # Cancel verifications and trigger sync deletes (AI Vision + Mediamtx)
    from app.services import camera_verification_service
    from app.services.camera_service import _sync_camera_delete
    for cam_id, v_id, c_name in camera_sync_tasks:
        camera_verification_service.cancel_verification(cam_id)
        background_tasks.add_task(_sync_camera_delete, cam_id, v_id, c_name)
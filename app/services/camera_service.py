from __future__ import annotations
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import BackgroundTasks, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import verify_village_scope
from app.core.config import get_settings
from app.core.rate_limit import get_rate_limiter
from app.db.session import async_session_maker
from app.models.camera import Camera, CameraDirection, CameraVerificationStatus
from app.models.group import Group
from app.models.user import User, UserRole
from app.schemas.camera import (
    CameraCreate,
    CameraRead,
    CameraResyncAllRead,
    CameraResyncFailedEntry,
    CameraStatusRead,
    CameraStreamTokenRead,
    CameraUpdate,
    CameraVerificationCheckRead,
)
from app.schemas.common import PaginatedResponse
from app.services import ai_vision_service, audit_service, camera_verification_service, mediamtx_service, notification_service, channel_service, channel_service
from app.services.ai_vision_service import VerificationCheckResult
from app.core.error_messages import CameraErrors, Common, VillageErrors
from app.core.url_utils import check_rtsp_stream
from app.services import channel_service


settings = get_settings()
logger = logging.getLogger(__name__)


async def _push_stream_config(camera_id: uuid.UUID, stream_ai: str, delay: int = 1) -> tuple[bool, list[str]]:
    failed_services: list[str] = []

    mediamtx_ok = await mediamtx_service.upsert_path(camera_id, stream_ai)
    if not mediamtx_ok:
        failed_services.append("mediamtx")

    ai_vision_ok = await ai_vision_service.push_camera_config(camera_id, stream_ai, delay)
    if not ai_vision_ok:
        failed_services.append("ai_vision")

    return ai_vision_ok, failed_services

async def _sync_camera_online(camera_id: uuid.UUID, stream_ai: str, delay: int = 1) -> tuple[bool, list[str]]:
    ai_vision_pushed, failed_services = await _push_stream_config(camera_id, stream_ai, delay)

    should_verify = False
    if ai_vision_pushed:
        should_verify = True

    return should_verify, failed_services


async def sync_camera_offline(camera_id: uuid.UUID) -> list[str]:
    failed_services: list[str] = []

    mediamtx_ok = await mediamtx_service.remove_path(camera_id)
    if not mediamtx_ok:
        failed_services.append("mediamtx")

    ai_vision_ok = await ai_vision_service.set_camera_active_status(camera_id, False)
    if not ai_vision_ok:
        failed_services.append("ai_vision")

    return failed_services

def _to_camera_read(camera: Camera) -> CameraRead:
    return CameraRead(
        id=camera.id,
        village_id=camera.village_id,
        name=camera.name,
        lat=camera.lat,
        long=camera.long,
        stream_ai=camera.stream_ai,
        direction=camera.direction,
        webhook_url=ai_vision_service.derive_webhook_url(),
        verification_status=camera.verification_status,
        ai_vision_synced_at=camera.ai_vision_synced_at,
        created_at=camera.created_at,
        is_active=camera.is_active,
        is_online=camera.is_online,
        delay=camera.delay,
    )


async def notify_sync_failure(
    village_id: uuid.UUID,
    camera_id: uuid.UUID,
    camera_name: str,
    failed_services: list[str],
) -> None:
    detail = (
        f"camera sync failed for '{camera_name}' (id={camera_id}): "f"{', '.join(failed_services)} did not accept the update"
    )
    logger.error(detail)

    async with async_session_maker() as db:
        await audit_service.log_action(
            db,
            request=None,
            action="camera_sync_failed",
            detail=detail,
            village_id=village_id,
        )
        await notification_service.notify_village(db, village_id, "camera_sync_failed", detail)
        await db.commit()

    from app.services import channel_service

    payload = {
        "camera_id": str(camera_id),
        "camera_name": camera_name,
        "failed_services": failed_services,
    }
    await channel_service.alerts.publish(village_id, "camera_sync_failed", payload)
    await channel_service.alerts.publish_global("camera_sync_failed", {**payload, "village_id": str(village_id)})

async def _sync_camera_create(
    camera_id: uuid.UUID,
    village_id: uuid.UUID,
    camera_name: str,
    stream_ai: str,
    delay: int = 1,
) -> None:
    should_verify, failed_services = await _sync_camera_online(camera_id, stream_ai, delay)

    if should_verify:
        camera_verification_service.start_verification(camera_id)

    if failed_services:
        await notify_sync_failure(village_id, camera_id, camera_name, list(dict.fromkeys(failed_services)))


async def _sync_camera_delete(camera_id, village_id, camera_name):
    mediamtx_ok, ai_vision_result = await asyncio.gather(
        mediamtx_service.remove_path(camera_id),
        ai_vision_service.delete_camera(camera_id),
    )

    failed_services = []
    if not mediamtx_ok:
        failed_services.append("mediamtx")
    if ai_vision_result not in (
        ai_vision_service.CameraDeleteResult.DELETED,
        ai_vision_service.CameraDeleteResult.NOT_FOUND,
    ):
        failed_services.append("ai_vision")

    if failed_services:
        await notify_sync_failure(village_id, camera_id, camera_name, failed_services)


async def _sync_camera_update(
    camera_id: uuid.UUID,
    village_id: uuid.UUID,
    camera_name: str,
    is_active: bool | None,
    stream_ai: str | None = None,
    delay: int | None = None,
) -> None:
    failed_services: list[str] = []

    if is_active is not None:
        status_ok = await ai_vision_service.set_camera_active_status(camera_id, is_active)
        if not status_ok:
            failed_services.append("ai_vision")
            
        if is_active and stream_ai:
            mediamtx_ok = await mediamtx_service.upsert_path(camera_id, stream_ai)
            if not mediamtx_ok:
                failed_services.append("mediamtx")
        elif not is_active:
            mediamtx_ok = await mediamtx_service.remove_path(camera_id)
            if not mediamtx_ok:
                failed_services.append("mediamtx")

    if delay is not None and stream_ai:
        update_ok = await ai_vision_service.update_camera_config(camera_id, stream_ai, delay)
        if not update_ok:
            failed_services.append("ai_vision")

    if failed_services:
        await notify_sync_failure(village_id, camera_id, camera_name, list(dict.fromkeys(failed_services)))


async def _get_village_or_404(db: AsyncSession, village_id: uuid.UUID) -> Group:
    result = await db.execute(select(Group).where(Group.id == village_id))
    village = result.scalar_one_or_none()
    if village is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=VillageErrors.NOT_FOUND)
    return village

async def create_camera(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    payload: CameraCreate,
) -> CameraRead:
    if current_user.role == UserRole.ADMIN:
        village_id = current_user.village_id
    else:
        village_id = payload.village_id
    village = await _get_village_or_404(db, village_id)

    if not village.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CameraErrors.CANNOT_CREATE_VILLAGE_INACTIVE,
        )

    existing_name_result = await db.execute(select(Camera).where(Camera.village_id == village_id, Camera.name == payload.name))
    if existing_name_result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CameraErrors.NAME_ALREADY_EXISTS,
        )

    existing_stream_result = await db.execute(select(Camera).where(Camera.stream_ai == payload.stream_ai))
    if existing_stream_result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="RTSP URL (stream_ai) is already in use by another camera",
        )

    camera = Camera(
        village_id=village_id,
        name=payload.name,
        lat=payload.lat,
        long=payload.long,
        stream_ai=payload.stream_ai,
        direction=payload.direction,
        delay=payload.delay,
        is_active=True,
        verification_status=CameraVerificationStatus.PENDING,
    )
    db.add(camera)
    await db.flush()

    await audit_service.log_action(
        db,
        request,
        action="camera_created",
        detail=f"camera created: {camera.name} (direction={camera.direction.value})",
        user_id=current_user.id,
        village_id=village_id,
    )
    await db.commit()
    await db.refresh(camera)

    background_tasks.add_task(
        _sync_camera_create, camera.id, camera.village_id, camera.name, camera.stream_ai, camera.delay
    )

    return _to_camera_read(camera)

async def get_camera(db: AsyncSession, current_user: User, camera_id: uuid.UUID) -> Camera:
    result = await db.execute(select(Camera).where(Camera.id == camera_id))
    camera = result.scalar_one_or_none()

    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CameraErrors.NOT_FOUND)

    verify_village_scope(current_user, camera.village_id)
    return camera

async def get_camera_detail(db: AsyncSession, current_user: User, camera_id: uuid.UUID) -> CameraRead:
    camera = await get_camera(db, current_user, camera_id)
    return _to_camera_read(camera)

async def get_camera_status(db: AsyncSession, current_user: User, camera_id: uuid.UUID) -> CameraStatusRead:
    camera = await get_camera(db, current_user, camera_id)
    stream_online, is_starting = await mediamtx_service.check_source_alive(camera.id)

    details = []

    if not stream_online and not is_starting:
        stream_is_healthy = False
        details.append("Stream is offline")
    elif is_starting:
        from app.core.url_utils import check_rtsp_stream
        stream_is_healthy = await check_rtsp_stream(camera.stream_ai)
        if stream_is_healthy:
            details.append("Stream is on standby")
        else:
            details.append("Camera is offline")
    else:
        stream_is_healthy = True

    if not camera.is_active:
        details.append("Camera is not active")

    if camera.verification_status != CameraVerificationStatus.VERIFIED:
        details.append(f"Verification status is '{camera.verification_status.value}'")

    # Strict condition: active AND online AND verified
    if camera.is_active and stream_is_healthy and camera.verification_status == CameraVerificationStatus.VERIFIED:
        status = True
    else:
        status = False

    if camera.is_online != stream_is_healthy:
        camera.is_online = stream_is_healthy
        await db.commit()

    return CameraStatusRead(
        id=camera.id,
        is_active=camera.is_active,
        verification_status=camera.verification_status,
        stream_online=stream_is_healthy,
        is_starting=is_starting,
        status=status,
        detail=", ".join(details) if details else None,
    )

async def get_camera_stream_token(
    db: AsyncSession,
    current_user: User,
    camera_id: uuid.UUID,
) -> CameraStreamTokenRead:
    result = await db.execute(
        select(Camera.village_id, Camera.is_active).where(Camera.id == camera_id)
    )
    row = result.one_or_none()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CameraErrors.NOT_FOUND)

    village_id, is_active = row
    verify_village_scope(current_user, village_id)

    if not is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CameraErrors.STREAM_UNAVAILABLE_INACTIVE,
        )

    stream_url = mediamtx_service.derive_stream_url(camera_id, current_user.id)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    return CameraStreamTokenRead(
        camera_id=camera_id,
        stream_url=stream_url,
        expires_at=expires_at,
    )

async def list_cameras(
    db: AsyncSession,
    current_user: User,
    village_id_filter: uuid.UUID | None,
    is_active_filter: bool | None,
    direction_filter: CameraDirection | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[CameraRead]:
    stmt = select(Camera)
    count_stmt = select(func.count()).select_from(Camera)

    if current_user.role == UserRole.SUPERADMIN:
        if village_id_filter is not None:
            stmt = stmt.where(Camera.village_id == village_id_filter)
            count_stmt = count_stmt.where(Camera.village_id == village_id_filter)
    else:
        if village_id_filter is not None and village_id_filter != current_user.village_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
            )
        stmt = stmt.where(Camera.village_id == current_user.village_id)
        count_stmt = count_stmt.where(Camera.village_id == current_user.village_id)

    if is_active_filter is not None:
        stmt = stmt.where(Camera.is_active == is_active_filter)
        count_stmt = count_stmt.where(Camera.is_active == is_active_filter)

    if direction_filter is not None:
        stmt = stmt.where(Camera.direction == direction_filter)
        count_stmt = count_stmt.where(Camera.direction == direction_filter)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    stmt = stmt.order_by(Camera.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    items = list(result.scalars().all())

    return PaginatedResponse(
        items=[_to_camera_read(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )

async def update_camera(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    camera_id: uuid.UUID,
    payload: CameraUpdate,
) -> CameraRead:
    camera = await get_camera(db, current_user, camera_id)
    village = await _get_village_or_404(db, camera.village_id)

    update_data = payload.model_dump(exclude_unset=True)

    if "is_active" in update_data and update_data["is_active"] and not village.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CameraErrors.CANNOT_ACTIVATE_VILLAGE_INACTIVE,
        )

    if "name" in update_data and update_data["name"] != camera.name:
        existing_name_result = await db.execute(select(Camera).where(Camera.village_id == camera.village_id, Camera.name == update_data["name"]))
        if existing_name_result.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=CameraErrors.NAME_ALREADY_EXISTS,
            )

    if "stream_ai" in update_data and update_data["stream_ai"] != camera.stream_ai:
        existing_stream_result = await db.execute(select(Camera).where(Camera.stream_ai == update_data["stream_ai"]))
        if existing_stream_result.scalars().first():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="RTSP URL (stream_ai) is already in use by another camera",
            )

    is_active_changed = "is_active" in update_data and update_data["is_active"] != camera.is_active
    stream_ai_changed = "stream_ai" in update_data and update_data["stream_ai"] != camera.stream_ai
    delay_changed = "delay" in update_data and update_data["delay"] != camera.delay

    for field, value in update_data.items():
        setattr(camera, field, value)

    if is_active_changed:
        if update_data["is_active"]:
            action = "camera_activated"
            detail = f"camera activated: {camera.name}"
        else:
            action = "camera_deactivated"
            detail = f"camera deactivated: {camera.name}"
    else:
        action = "camera_updated"
        detail = f"camera updated: {camera.name}"

    await audit_service.log_action(
        db,
        request,
        action=action,
        detail=detail,
        user_id=current_user.id,
        village_id=camera.village_id,
    )
    await db.commit()
    await db.refresh(camera)

    if (is_active_changed or stream_ai_changed or delay_changed) and village.is_active:
        background_tasks.add_task(
            _sync_camera_update,
            camera.id,
            camera.village_id,
            camera.name,
            camera.is_active,
            camera.stream_ai,
            camera.delay if delay_changed else None,
        )
    elif is_active_changed or stream_ai_changed:
        logger.info(
            "Skipping camera sync for %s: village %s is inactive", camera.id, camera.village_id
        )

    return _to_camera_read(camera)

async def delete_camera(db, request, background_tasks, current_user, camera_id):
    camera = await get_camera(db, current_user, camera_id)

    camera_id_value = camera.id
    village_id = camera.village_id
    camera_name = camera.name

    await audit_service.log_action(
        db, request, action="camera_deleted",
        detail=f"camera permanently deleted: {camera.name} (detection history retained via snapshot)",
        user_id=current_user.id, village_id=camera.village_id,
    )

    await db.delete(camera)
    await db.commit()

    camera_verification_service.cancel_verification(camera_id_value)
    background_tasks.add_task(_sync_camera_delete, camera_id_value, village_id, camera_name)


def _build_resync_scope_filters(
    current_user: User, village_id_filter: uuid.UUID | None
) -> list:
    if current_user.role == UserRole.SUPERADMIN:
        if village_id_filter is not None:
            return [Camera.village_id == village_id_filter]
        return []

    if village_id_filter is not None and village_id_filter != current_user.village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
        )
    return [Camera.village_id == current_user.village_id]


async def _upsert_path_guarded(
    semaphore: asyncio.Semaphore, camera: Camera
) -> tuple[Camera, bool]:
    async with semaphore:
        ok = await mediamtx_service.upsert_path(camera.id, camera.stream_ai)
        return camera, ok


async def _resync_cameras(db: AsyncSession, scope_filters: list) -> CameraResyncAllRead:
    result = await db.execute(
        select(Camera)
        .join(Group, Camera.village_id == Group.id)
        .where(Camera.is_active.is_(True), Group.is_active.is_(True), *scope_filters)
    )
    cameras = list(result.scalars().all())

    semaphore = asyncio.Semaphore(settings.camera_resync_concurrency_limit)
    outcomes = await asyncio.gather(
        *(_upsert_path_guarded(semaphore, camera) for camera in cameras)
    )

    failed_cameras = [
        CameraResyncFailedEntry(id=camera.id, name=camera.name)
        for camera, ok in outcomes
        if not ok
    ]

    return CameraResyncAllRead(
        total=len(cameras),
        succeeded=len(cameras) - len(failed_cameras),
        failed=len(failed_cameras),
        failed_cameras=failed_cameras,
    )


async def _run_resync_all_cameras_background(
    user_id: uuid.UUID,
    village_id_filter: uuid.UUID | None,
    is_superadmin: bool,
    user_village_id: uuid.UUID | None,
) -> None:
    async with async_session_maker() as db:
        if is_superadmin:
            scope_filters = [Camera.village_id == village_id_filter] if village_id_filter else []
        else:
            scope_filters = [Camera.village_id == user_village_id]

        resync_result = await _resync_cameras(db, scope_filters)

        detail = (
            f"resynced {resync_result.total} camera(s) with MediaMTX: "
            f"{resync_result.succeeded} succeeded, {resync_result.failed} failed"
        )
        await audit_service.log_action(
            db,
            request=None,
            action="camera_resync_all",
            detail=detail,
            user_id=user_id,
            village_id=village_id_filter or user_village_id,
        )
        
        payload = {
            "total": resync_result.total,
            "succeeded": resync_result.succeeded,
            "failed": resync_result.failed,
            "detail": detail
        }
        
        target_village = village_id_filter or user_village_id
        if target_village:
            await notification_service.notify_village(db, target_village, "camera_resync_all_completed", detail)
            await channel_service.alerts.publish(target_village, "camera_resync_all_completed", payload)
        else:
            await channel_service.alerts.publish_global("camera_resync_all_completed", payload)
            
        await db.commit()


async def resync_all_cameras(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User,
    village_id_filter: uuid.UUID | None,
) -> None:
    _build_resync_scope_filters(current_user, village_id_filter)

    if village_id_filter is not None:
        await _get_village_or_404(db, village_id_filter)

    background_tasks.add_task(
        _run_resync_all_cameras_background,
        current_user.id,
        village_id_filter,
        current_user.role == UserRole.SUPERADMIN,
        current_user.village_id,
    )


async def resync_all_cameras_on_startup(db: AsyncSession) -> CameraResyncAllRead:
    resync_result = await _resync_cameras(db, scope_filters=[])
    logger.info(
        "Startup camera resync completed: total=%s succeeded=%s failed=%s",
        resync_result.total,
        resync_result.succeeded,
        resync_result.failed,
    )
    return resync_result


async def check_camera_verification_now(
    db: AsyncSession,
    request: Request,
    current_user: User,
    camera_id: uuid.UUID,
) -> CameraVerificationCheckRead:
    camera = await get_camera(db, current_user, camera_id)

    get_rate_limiter().check(
        f"manual_verify_check:{camera_id}",
        settings.camera_manual_verify_rate_limit,
        settings.camera_manual_verify_rate_window_seconds,
    )

    result = await ai_vision_service.check_camera_verification(camera.id)
    ai_vision_reachable = result != VerificationCheckResult.UNREACHABLE
    is_pending_locally = camera.verification_status == CameraVerificationStatus.PENDING

    polling_restarted = False
    anomaly_detected = False
    note: str | None = None

    if result == VerificationCheckResult.UNREACHABLE:
        note = "ไม่สามารถติดต่อ ai vision service ได้ในขณะนี้ กรุณาลองใหม่ภายหลัง"

    elif result == VerificationCheckResult.PENDING:
        if not camera_verification_service.is_verification_running(camera.id):
            camera_verification_service.start_verification(camera.id)
            polling_restarted = True

    elif result == VerificationCheckResult.VERIFIED:
        if is_pending_locally:
            await camera_verification_service.finalize_verification(
                camera.id,
                verified=True,
                reason="verified by ai vision service",
                request=request,
                user_id=current_user.id,
            )
            await db.refresh(camera)
        elif camera.verification_status == CameraVerificationStatus.FAILED:
            anomaly_detected = True
            note = (
                f"ai vision service รายงานว่ากล้อง '{camera.name}' verified แล้ว แต่ในระบบเราบันทึกสถานะเป็น "
                "failed อยู่ ระบบไม่ได้แก้สถานะให้อัตโนมัติ กรุณาติดต่อผู้ดูแลระบบ"
            )

    elif result == VerificationCheckResult.NOT_FOUND:
        pushed = await ai_vision_service.push_camera_config(camera.id, camera.stream_ai, camera.delay)
        if not pushed:
            note = "ai vision service ไม่พบกล้องในระบบและพยายามลงทะเบียนใหม่แต่ไม่สำเร็จ กรุณาลองใหม่"
        else:
            active_ok = await ai_vision_service.set_camera_active_status(camera.id, camera.is_active)
            if not active_ok:
                note = "ลงทะเบียนกล้องใหม่สำเร็จ แต่ไม่สามารถตั้งค่าสถานะเปิด/ปิดได้ กรุณาลองใหม่"
            else:
                camera.verification_status = CameraVerificationStatus.PENDING
                await audit_service.log_action(
                    db,
                    request,
                    action="camera_auto_resync_ai_vision",
                    detail=f"auto re-registered camera with ai vision service: {camera.name}",
                    user_id=current_user.id,
                    village_id=camera.village_id,
                )
                await db.commit()
                await db.refresh(camera)
                camera_verification_service.start_verification(camera.id)
                polling_restarted = True
                note = "ai vision service ไม่พบกล้องในระบบ ระบบได้ทำการส่งข้อมูลลงทะเบียนกล้องใหม่ให้อัตโนมัติและกำลังรอการตรวจสอบ"

    if anomaly_detected:
        await audit_service.log_action(
            db,
            request,
            action="camera_verification_anomaly",
            detail=(
                f"anomaly on manual verification check for '{camera.name}': ai vision reports "
                f"{result.value} but local status is {camera.verification_status.value} "
                f"(is_active={camera.is_active}); no changes applied"
            ),
            user_id=current_user.id,
            village_id=camera.village_id,
        )
        await db.commit()

    return CameraVerificationCheckRead(
        id=camera.id,
        verification_status=camera.verification_status,
        is_active=camera.is_active,
        ai_vision_synced_at=camera.ai_vision_synced_at,
        ai_vision_reachable=ai_vision_reachable,
        polling_restarted=polling_restarted,
        anomaly_detected=anomaly_detected,
        note=note,
    )


async def _deactivate_camera_guarded(
    semaphore: asyncio.Semaphore, camera: Camera
) -> tuple[Camera, list[str]]:
    async with semaphore:
        failed_services = await sync_camera_offline(camera.id)
        return camera, failed_services


async def cascade_deactivate_village_cameras(
    db: AsyncSession, village_id: uuid.UUID
) -> list[uuid.UUID]:
    """
    ปิดกล้องทุกตัวที่ยัง active อยู่ในหมู่บ้านนี้ทันทีในทรานแซกชันเดียวกับการปิดหมู่บ้าน
    (DB-only ไม่ยิง network ที่นี่) คืนค่า camera_ids ที่ถูกปิด เพื่อให้ผู้เรียกเอาไป sync
    ภายนอก (mediamtx/ai_vision) เป็น background task ต่อหลัง commit

    หมายเหตุ: กล้องที่เคย active ก่อนหมู่บ้านปิดจะถูก force is_active=False โดยไม่เก็บสถานะเดิม
    เมื่อหมู่บ้าน reactivate ภายหลัง กล้องทุกตัวที่ inactive ในหมู่บ้านนี้ (รวมถึงตัวที่แอดมินเคยปิดเอง
    ก่อนหน้าด้วย) จะถูกเปิดกลับมาทั้งหมด เป็น trade-off ที่ยอมรับแล้ว
    """
    result = await db.execute(
        select(Camera.id).where(Camera.village_id == village_id, Camera.is_active.is_(True))
    )
    camera_ids = list(result.scalars().all())
    if not camera_ids:
        return camera_ids

    await db.execute(update(Camera).where(Camera.id.in_(camera_ids)).values(is_active=False))

    for camera_id in camera_ids:
        camera_verification_service.cancel_verification(camera_id)

    return camera_ids


async def push_cameras_offline(village_id: uuid.UUID, camera_ids: list[uuid.UUID]) -> None:
    if not camera_ids:
        return

    async with async_session_maker() as db:
        result = await db.execute(select(Camera).where(Camera.id.in_(camera_ids)))
        cameras = list(result.scalars().all())

    semaphore = asyncio.Semaphore(settings.camera_resync_concurrency_limit)
    outcomes = await asyncio.gather(
        *(_deactivate_camera_guarded(semaphore, camera) for camera in cameras)
    )

    for camera, failed_services in outcomes:
        if failed_services:
            await notify_sync_failure(village_id, camera.id, camera.name, list(dict.fromkeys(failed_services)))

async def _sync_camera_reactivate(camera_id: uuid.UUID, stream_ai: str) -> tuple[bool, list[str]]:
    failed_services: list[str] = []

    mediamtx_ok = await mediamtx_service.upsert_path(camera_id, stream_ai)
    if not mediamtx_ok:
        failed_services.append("mediamtx")

    ai_vision_ok = await ai_vision_service.set_camera_active_status(camera_id, True)
    if not ai_vision_ok:
        failed_services.append("ai_vision")

    return ai_vision_ok, failed_services

async def _activate_camera_guarded(
    semaphore: asyncio.Semaphore, camera: Camera
) -> tuple[Camera, bool, list[str]]:
    async with semaphore:
        should_verify, failed_services = await _sync_camera_reactivate(camera.id, camera.stream_ai)
        return camera, should_verify, failed_services


async def cascade_reactivate_village_cameras(
    db: AsyncSession, village_id: uuid.UUID
) -> list[uuid.UUID]:
    """
    เปิดกล้องทุกตัวที่ inactive อยู่ในหมู่บ้านนี้กลับมาทันทีในทรานแซกชันเดียวกับการเปิดหมู่บ้าน
    (DB-only) คืนค่า camera_ids ที่ถูกเปิด เพื่อให้ผู้เรียกเอาไป sync ภายนอกเป็น background task ต่อ
    """
    result = await db.execute(
        select(Camera.id).where(Camera.village_id == village_id, Camera.is_active.is_(False))
    )
    camera_ids = list(result.scalars().all())
    if not camera_ids:
        return camera_ids

    await db.execute(update(Camera).where(Camera.id.in_(camera_ids)).values(is_active=True))

    return camera_ids


async def push_cameras_online(village_id: uuid.UUID, camera_ids: list[uuid.UUID]) -> None:
    if not camera_ids:
        return

    async with async_session_maker() as db:
        result = await db.execute(select(Camera).where(Camera.id.in_(camera_ids)))
        cameras = list(result.scalars().all())

    semaphore = asyncio.Semaphore(settings.camera_resync_concurrency_limit)
    outcomes = await asyncio.gather(
        *(_activate_camera_guarded(semaphore, camera) for camera in cameras)
    )

    verifying_camera_ids = [camera.id for camera, should_verify, _ in outcomes if should_verify]
    if verifying_camera_ids:
        async with async_session_maker() as db:
            result = await db.execute(select(Camera).where(Camera.id.in_(verifying_camera_ids)))
            for db_camera in result.scalars().all():
                db_camera.verification_status = CameraVerificationStatus.PENDING
            await db.commit()
        for camera_id in verifying_camera_ids:
            camera_verification_service.start_verification(camera_id)

    for camera, _, failed_services in outcomes:
        if failed_services:
            await notify_sync_failure(village_id, camera.id, camera.name, list(dict.fromkeys(failed_services)))

async def check_and_update_camera_statuses(db: AsyncSession) -> int:
    result = await db.execute(select(Camera).where(Camera.is_active == True))
    cameras = result.scalars().all()
    if not cameras:
        return 0

    semaphore = asyncio.Semaphore(10)

    async def _check_camera(camera: Camera) -> tuple[Camera, bool]:
        async with semaphore:
            is_online = await check_rtsp_stream(camera.stream_ai)
            return camera, is_online

    outcomes = await asyncio.gather(*(_check_camera(c) for c in cameras))
    
    updates_made = 0
    for camera, is_online in outcomes:
        if camera.is_online != is_online:
            camera.is_online = is_online
            updates_made += 1
            
            # Real-time update to dashboard
            payload = {"camera_id": str(camera.id), "camera_name": camera.name, "is_online": is_online}
            await channel_service.alerts.publish(camera.village_id, "camera_status_changed", payload)
            await channel_service.alerts.publish_global("camera_status_changed", {**payload, "village_id": str(camera.village_id)})

            if not is_online:
                detail = f"กล้อง '{camera.name}' ขาดการเชื่อมต่อ"
                await notification_service.notify_village(
                    db, 
                    camera.village_id, 
                    "camera_offline", 
                    detail, 
                    payload
                )
                await channel_service.alerts.publish(camera.village_id, "camera_offline", payload)
                await channel_service.alerts.publish_global("camera_offline", {**payload, "village_id": str(camera.village_id)})

    if updates_made > 0:
        await db.commit()
        
    return updates_made
from __future__ import annotations
import asyncio
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Literal
from fastapi import BackgroundTasks, HTTPException, Request, UploadFile, status
from sqlalchemy import case, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import verify_village_scope
from app.models.blacklist import Blacklist
from app.models.whitelist import Whitelist
from app.models.camera import Camera, CameraDirection, CameraVerificationStatus
from app.models.car import Car
from app.models.group import Group
from app.models.user import User
from app.schemas.car import (
    CameraSummary,
    CarDetailRead,
    CarRead,
    DetectionCreate,
    DetectionDashboardRead,
    DetectionEventCamera,
    DetectionEventCameraGlobal,
    DetectionEventPayload,
    DetectionEventPayloadGlobal,
    RepeatedPlateEntry,
    DetectionCreateAck,
    RouteTrackingCarGroup,
    RouteTrackingDayEntry,
    RouteTrackingDetectionEntry,
    RouteTrackingRead,
)
from app.schemas.common import PaginatedResponse
from app.services import (
    audit_service,
    camera_verification_service,
    channel_service,
    storage_service,
    notification_service,
    blacklist_service
)
import logging
from app.core.timezone import BANGKOK_TZ
from app.core.error_messages import Common, DetectionErrors, CameraErrors
from app.core.scope_utils import build_scope_filters
from app.core.scope_utils import build_scope_filters
from app.core.db_utils import escape_like
import os
import time
from fastapi.concurrency import run_in_threadpool
from app.core.config import get_settings


_MAX_ROUTE_TRACKING_RANGE_DAYS = 360

logger = logging.getLogger(__name__)

def _to_car_read(car: Car, request: Request, village_name: str | None = None) -> CarRead:
    return CarRead(
        id=car.id,
        event_id=car.event_id,
        camera_id=car.camera_id,
        camera_name=car.camera_name,
        village_id=car.village_id,
        village_name=village_name or car.village_name,
        license_plate=car.license_plate,
        province=car.province,
        color=car.color,
        image_crop=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="crop")
        ),
        image_full=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="full")
        ),
        time_detect=car.time_detect,
        is_blacklist=car.is_blacklist,
        is_whitelist=car.is_whitelist,
        direction=car.direction,
        created_at=car.created_at,
    )


def _to_car_detail_read(car: Car, camera: Camera, village: Group, request: Request) -> CarDetailRead:
    return CarDetailRead(
        id=car.id,
        event_id=car.event_id,
        license_plate=car.license_plate,
        province=car.province,
        color=car.color,
        image_crop=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="crop")
        ),
        image_full=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="full")
        ),
        time_detect=car.time_detect,
        is_blacklist=car.is_blacklist,
        is_whitelist=car.is_whitelist,
        direction=car.direction,
        created_at=car.created_at,
        camera=CameraSummary(
            id=camera.id if camera is not None else None,
            name=camera.name if camera is not None else car.camera_name,
            village_id=car.village_id,
            village_name=village.name if village is not None else None,
            is_camera_deleted=camera is None,
        ),
    )


async def _get_camera_or_404(db: AsyncSession, camera_id: uuid.UUID) -> tuple[Camera, str | None]:
    result = await db.execute(
        select(Camera, Group.is_active, Group.name)
        .join(Group, Camera.village_id == Group.id)
        .where(Camera.id == camera_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CameraErrors.NOT_FOUND)

    camera, village_is_active, village_name = row

    if not village_is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DetectionErrors.CAMERA_VILLAGE_INACTIVE,
        )
    if not camera.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=DetectionErrors.CAMERA_INACTIVE,
        )
    return camera, village_name


async def _find_existing_car_by_event_id(db: AsyncSession, event_id: uuid.UUID) -> Car | None:
    result = await db.execute(select(Car).where(Car.event_id == event_id))
    return result.scalar_one_or_none()


async def _check_is_blacklisted(
    db: AsyncSession,
    village_id: uuid.UUID,
    license_plate: str,
    province: str,
) -> bool:
    result = await db.execute(
        select(Blacklist.id)
        .where(
            Blacklist.village_id == village_id,
            Blacklist.license_plate == license_plate,
            Blacklist.province == province,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _check_is_whitelisted(
    db: AsyncSession,
    village_id: uuid.UUID,
    license_plate: str,
    province: str,
) -> bool:
    result = await db.execute(
        select(Whitelist.id)
        .where(
            Whitelist.village_id == village_id,
            Whitelist.license_plate == license_plate,
            Whitelist.province == province,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None

async def _cleanup_written_images(written_paths: list[str]) -> None:
    for relative_path in written_paths:
        await storage_service.delete_image(relative_path)


def _build_detection_event_payload(request: Request, camera: Camera, car: Car) -> DetectionEventPayload:
    return DetectionEventPayload(
        detection_id=car.id,
        license_plate=car.license_plate,
        province=car.province,
        color=car.color,
        time_detect=car.time_detect,
        is_blacklist=car.is_blacklist,
        is_whitelist=car.is_whitelist,
        direction=car.direction,
        camera=DetectionEventCamera(
            id=camera.id,
            name=camera.name,
            lat=camera.lat,
            long=camera.long,
        ),
        image_crop=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="crop")
        ),
        image_full=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="full")
        ),
    )


def _build_global_detection_event_payload(
    request: Request, camera: Camera, car: Car
) -> DetectionEventPayloadGlobal:
    return DetectionEventPayloadGlobal(
        detection_id=car.id,
        license_plate=car.license_plate,
        province=car.province,
        color=car.color,
        time_detect=car.time_detect,
        is_blacklist=car.is_blacklist,
        is_whitelist=car.is_whitelist,
        direction=car.direction,
        camera=DetectionEventCameraGlobal(
            id=camera.id,
            name=camera.name,
            lat=camera.lat,
            long=camera.long,
            village_id=camera.village_id,
        ),
        image_crop=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="crop")
        ),
        image_full=str(
            request.url_for("get_detection_image", detection_id=car.id, variant="full")
        ),
    )


async def create_detection(
    db: AsyncSession,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: DetectionCreate,
    image_crop: UploadFile,
    image_full: UploadFile,
) -> tuple[DetectionCreateAck, bool]:
    """
    สร้าง detection ใหม่จาก webhook ของ AI vision

    Idempotent ตาม event_id: ถ้า event_id นี้เคยถูกบันทึกไปแล้ว (ไม่ว่าจะจากการ
    ประมวลผลสำเร็จรอบก่อน หรือจาก request คู่แข่งที่ insert ไปพร้อมกัน) จะไม่สร้าง
    record ใหม่ ไม่เขียนรูปซ้ำ ไม่ publish SSE ซ้ำ แต่ return ack ของ record เดิมกลับไป
    โดยตัวที่สองของ tuple ที่ return คือ is_new (True = สร้างจริง, False = replay)
    ผู้เรียกใช้ค่านี้เพื่อเลือกตอบ 201 หรือ 200 กลับไปยัง AI vision
    """
    existing = await _find_existing_car_by_event_id(db, payload.event_id)
    if existing is not None:
        logger.info(
            "duplicate detection event replay: event_id=%s detection_id=%s",
            payload.event_id, existing.id,
        )
        return DetectionCreateAck(event_id=existing.event_id), False

    camera, village_name = await _get_camera_or_404(db, payload.camera_id)

    if camera.verification_status == CameraVerificationStatus.PENDING:
        try:
            await camera_verification_service.verify_from_detection(camera.id, request)
        except Exception:
            logger.exception(
                "Failed to finalize implicit camera verification for camera_id=%s", camera.id
            )

    car_id = uuid.uuid4()

    crop_content, crop_extension = await storage_service.read_and_validate_image(image_crop)
    full_content, full_extension = await storage_service.read_and_validate_image(image_full)

    crop_path = storage_service.build_detection_image_path(
        village_id=camera.village_id,
        camera_id=camera.id,
        image_id=car_id,
        suffix="crop",
        extension=crop_extension,
    )
    full_path = storage_service.build_detection_image_path(
        village_id=camera.village_id,
        camera_id=camera.id,
        image_id=car_id,
        suffix="full",
        extension=full_extension,
    )

    is_blacklist = await _check_is_blacklisted(db, camera.village_id, payload.license_plate, payload.province)
    is_whitelist = await _check_is_whitelisted(db, camera.village_id, payload.license_plate, payload.province)

    written_paths: list[str] = []
    try:
        await storage_service.write_image(crop_path, crop_content)
        written_paths.append(crop_path)
        await storage_service.write_image(full_path, full_content)
        written_paths.append(full_path)
    except OSError:
        await _cleanup_written_images(written_paths)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=DetectionErrors.STORE_IMAGE_FAILED,
        )

    car = Car(
        id=car_id,
        event_id=payload.event_id,
        camera_id=camera.id,
        village_id=camera.village_id,
        village_name=village_name,
        camera_name=camera.name,
        camera_lat=camera.lat,
        camera_long=camera.long,
        license_plate=payload.license_plate,
        province=payload.province,
        color=payload.color,
        image_crop=crop_path,
        image_full=full_path,
        time_detect=payload.capture_time,
        is_blacklist=is_blacklist,
        is_whitelist=is_whitelist,
        direction=camera.direction,
    )
    db.add(car)

    if is_blacklist:
        await audit_service.log_action(
            db,
            request,
            action="blacklist_detection",
            detail=f"blacklisted plate detected: {payload.license_plate} ({payload.province})",
            village_id=camera.village_id,
        )
        await notification_service.notify_village(
            db, camera.village_id, "blacklist_alert",
            f"blacklisted plate detected: {payload.license_plate} ({payload.province})",
        )

    if is_whitelist:
        await notification_service.notify_village(
            db, camera.village_id, "whitelist_alert",
            f"whitelisted plate detected: {payload.license_plate} ({payload.province})",
        )

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()

        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate != "23505":
            await _cleanup_written_images(written_paths)
            raise

        await _cleanup_written_images(written_paths)

        existing = await _find_existing_car_by_event_id(db, payload.event_id)
        logger.info(
            "race on duplicate detection event, returning existing record: event_id=%s",
            payload.event_id,
        )
        return DetectionCreateAck(event_id=existing.event_id if existing else payload.event_id), False
    except Exception:
        await db.rollback()
        await _cleanup_written_images(written_paths)
        raise

    await db.refresh(car)

    if is_blacklist:
        background_tasks.add_task(
            blacklist_service.handle_blacklist_detection,
            camera.id,
            camera.village_id,
            camera.name,
            car.license_plate,
            car.province,
            car.time_detect,
        )

    try:
        event_payload = _build_detection_event_payload(request, camera, car)
        event_data = event_payload.model_dump(mode="json")
        logger.warning(f"[SSE DEBUG] Publishing detection_created for car {car.id}, is_blacklist={is_blacklist}, is_whitelist={is_whitelist}, village={camera.village_id}")
        await channel_service.alerts.publish(camera.village_id, "detection_created", event_data)
        global_payload = _build_global_detection_event_payload(request, camera, car)
        global_event_data = global_payload.model_dump(mode="json")
        await channel_service.alerts.publish_global("detection_created", global_event_data)

        if is_blacklist:
            logger.warning(f"[SSE DEBUG] Publishing blacklist_alert for car {car.id}")
            await channel_service.alerts.publish(camera.village_id, "blacklist_alert", event_data)
            await channel_service.alerts.publish_global("blacklist_alert", global_event_data)
        if is_whitelist:
            logger.warning(f"[SSE DEBUG] Publishing whitelist_alert for car {car.id}")
            await channel_service.alerts.publish(camera.village_id, "whitelist_alert", event_data)
            await channel_service.alerts.publish_global("whitelist_alert", global_event_data)
    except Exception:
        logger.exception(
            "Failed to publish SSE event for detection_id=%s event_id=%s",
            car.id, car.event_id,
        )

    return DetectionCreateAck(event_id=car.event_id), True


async def list_detections(
    db: AsyncSession,
    request: Request,
    current_user: User,
    village_id: uuid.UUID | None,
    village_name: str | None,
    camera_id: uuid.UUID | None,
    license_plate: str | None,
    province: str | None,
    color: str | None,
    time_detect_from: datetime | None,
    time_detect_to: datetime | None,
    is_blacklist: bool | None,
    is_whitelist: bool | None,
    direction: CameraDirection | None,
    order: Literal["asc", "desc"],
    page: int,
    page_size: int,
) -> PaginatedResponse[CarRead]:
    scope_filters = build_scope_filters(current_user, village_id, Car)
    stmt = select(Car).where(*scope_filters)

    if village_name is not None:
        stmt = stmt.where(Car.village_name.ilike(f"%{escape_like(village_name)}%", escape="\\"))
    if camera_id is not None:
        stmt = stmt.where(Car.camera_id == camera_id)
    if license_plate is not None:
        stmt = stmt.where(Car.license_plate.ilike(f"%{escape_like(license_plate)}%", escape="\\"))
    if province is not None:
        stmt = stmt.where(Car.province == province)
    if color is not None:
        stmt = stmt.where(Car.color.ilike(f"%{escape_like(color)}%", escape="\\"))
    if time_detect_from is not None:
        stmt = stmt.where(Car.time_detect >= time_detect_from)
    if time_detect_to is not None:
        stmt = stmt.where(Car.time_detect <= time_detect_to)
    if is_blacklist is not None:
        stmt = stmt.where(Car.is_blacklist == is_blacklist)
    if is_whitelist is not None:
        stmt = stmt.where(Car.is_whitelist == is_whitelist)
    if direction is not None:
        stmt = stmt.where(Car.direction == direction)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    if order == "asc":
        stmt = stmt.order_by(Car.time_detect.asc())
    else:
        stmt = stmt.order_by(Car.time_detect.desc())

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    items = result.scalars().all()

    village_ids = {car.village_id for car in items if car.village_id}
    village_names = {}
    if village_ids:
        group_res = await db.execute(select(Group.id, Group.name).where(Group.id.in_(village_ids)))
        village_names = {row.id: row.name for row in group_res.all()}

    return PaginatedResponse[CarRead](
        items=[_to_car_read(item, request, village_names.get(item.village_id)) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_detection_detail(
    db: AsyncSession,
    request: Request,
    current_user: User,
    detection_id: uuid.UUID,
) -> CarDetailRead:
    result = await db.execute(
        select(Car, Camera, Group)
        .outerjoin(Camera, Car.camera_id == Camera.id)
        .outerjoin(Group, Car.village_id == Group.id)
        .where(Car.id == detection_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ไม่พบข้อมูลการตรวจจับ")
    car, camera, village = row
    verify_village_scope(current_user, car.village_id)
    return _to_car_detail_read(car, camera, village, request)


async def get_detection_image_path(
    db: AsyncSession,
    current_user: User,
    detection_id: uuid.UUID,
    variant: str,
) -> tuple[Path, str]:
    result = await db.execute(select(Car).where(Car.id == detection_id))
    car = result.scalar_one_or_none()
    if car is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DetectionErrors.NOT_FOUND)
    verify_village_scope(current_user, car.village_id)

    relative_path = car.image_crop if variant == "crop" else car.image_full
    absolute_path = storage_service.resolve_storage_path(relative_path)
    if not absolute_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DetectionErrors.IMAGE_FILE_NOT_FOUND)

    return absolute_path, storage_service.guess_media_type(absolute_path)

def _today_bangkok_bounds() -> tuple[datetime, datetime, datetime]:
    now_bangkok = datetime.now(BANGKOK_TZ)
    start_of_day_bangkok = now_bangkok.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day_bangkok = start_of_day_bangkok + timedelta(days=1)
    return (
        start_of_day_bangkok,
        start_of_day_bangkok.astimezone(timezone.utc),
        end_of_day_bangkok.astimezone(timezone.utc),
    )


from app.core.scope_utils import build_scope_filters


async def get_today_dashboard(
    db: AsyncSession,
    request: Request,
    current_user: User,
    village_id: uuid.UUID | None,
    latest_limit: int,
) -> DetectionDashboardRead:
    start_of_day_bangkok, start_utc, end_utc = _today_bangkok_bounds()
    scope_filters = build_scope_filters(current_user, village_id, Car)
    base_filters = [Car.time_detect >= start_utc, Car.time_detect < end_utc, *scope_filters]

    agg_query = (
        select(
            func.count().label("total"),
            func.count(func.distinct(tuple_(Car.license_plate, Car.province))).label("unique_plates"),
            func.count(case((Car.is_blacklist.is_(True), 1))).label("blacklist"),
            func.count(case((Car.is_whitelist.is_(True), 1))).label("whitelist"),
            func.count(case((Car.direction == CameraDirection.ENTRY, 1))).label("entry"),
            func.count(case((Car.direction == CameraDirection.EXIT, 1))).label("exit"),
            func.count(case((Car.direction == CameraDirection.INTERNAL, 1))).label("internal"),
        )
        .select_from(Car)
        .where(*base_filters)
    )

    top_repeated_query = (
        select(Car.license_plate, Car.province, func.count())
        .where(*base_filters)
        .group_by(Car.license_plate, Car.province)
        .having(func.count() > 1)
        .order_by(func.count().desc())
        .limit(10)
    )

    latest_query = (
        select(Car)
        .where(*base_filters)
        .order_by(Car.time_detect.desc())
        .limit(latest_limit)
    )

    agg_result = await db.execute(agg_query)
    top_repeated_result = await db.execute(top_repeated_query)
    latest_result = await db.execute(latest_query)

    row = agg_result.one()
    unique_plates_today = row.unique_plates

    top_repeated_plates = [
        RepeatedPlateEntry(license_plate=plate, province=province, count=count)
        for plate, province, count in top_repeated_result.all()
    ]
    
    latest_detections = [_to_car_read(item, request) for item in latest_result.scalars().all()]

    return DetectionDashboardRead(
        date=start_of_day_bangkok.date(),
        total_detections_today=row.total,
        unique_plates_today=unique_plates_today,
        blacklist_detections_today=row.blacklist,
        whitelist_detections_today=row.whitelist,
        entry_detections_today=row.entry,
        exit_detections_today=row.exit,
        internal_detections_today=row.internal,
        top_repeated_plates=top_repeated_plates,
        latest_detections=latest_detections,
    )



def _validate_route_tracking_date_range(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    if date_to < date_from:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=DetectionErrors.DATE_RANGE_INVALID)

    if (date_to - date_from).days > _MAX_ROUTE_TRACKING_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=DetectionErrors.date_range_too_wide(_MAX_ROUTE_TRACKING_RANGE_DAYS),
        )

    start_utc = datetime(date_from.year, date_from.month, date_from.day, tzinfo=BANGKOK_TZ).astimezone(timezone.utc)
    end_utc = (datetime(date_to.year, date_to.month, date_to.day, tzinfo=BANGKOK_TZ) + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc, end_utc


def _build_route_tracking_filters(
    scope_filters: list,
    normalized_plate: str,
    province: str | None,
    color: str | None,
    direction: CameraDirection | None,
    start_utc: datetime,
    end_utc: datetime,
) -> list:
    escaped_plate = normalized_plate.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    filters = [
        *scope_filters,
        Car.license_plate.ilike(f"%{escaped_plate}%", escape="\\"),
        Car.time_detect >= start_utc,
        Car.time_detect < end_utc,
    ]
    if province is not None:
        filters.append(Car.province == province)
    if color is not None:
        escaped_color = color.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(Car.color.ilike(f"%{escaped_color}%", escape="\\"))
    if direction is not None:
        filters.append(Car.direction == direction)
    return filters


async def get_route_tracking(
    db: AsyncSession,
    request: Request,
    current_user: User,
    license_plate: str,
    province: str | None,
    color: str | None,
    direction: CameraDirection | None,
    village_id: uuid.UUID | None,
    date_from: date,
    date_to: date,
    page: int,
    page_size: int,
) -> RouteTrackingRead:
    normalized_plate = license_plate.strip().upper()
    if not normalized_plate:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=DetectionErrors.LICENSE_PLATE_REQUIRED)

    scope_filters = build_scope_filters(current_user, village_id, Car)
    start_utc, end_utc = _validate_route_tracking_date_range(date_from, date_to)
    filters = _build_route_tracking_filters(
        scope_filters, normalized_plate, province, color, direction, start_utc, end_utc
    )

    bangkok_date_expr = func.date(func.timezone("Asia/Bangkok", Car.time_detect))

    total_dates_result = await db.execute(
        select(func.count(func.distinct(bangkok_date_expr)))
        .select_from(Car)
        .where(*filters)
    )
    total_dates = total_dates_result.scalar_one()

    total_detections_result = await db.execute(
        select(func.count())
        .select_from(Car)
        .where(*filters)
    )
    total_detections = total_detections_result.scalar_one()

    page_dates_result = await db.execute(
        select(bangkok_date_expr.label("d"))
        .select_from(Car)
        .where(*filters)
        .group_by(bangkok_date_expr)
        .order_by(bangkok_date_expr.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    page_dates = [row.d for row in page_dates_result.all()]

    if not page_dates:
        return RouteTrackingRead(
            items=[],
            total_dates=total_dates,
            total_detections=total_detections,
            page=page,
            page_size=page_size,
        )

    detail_result = await db.execute(
        select(Car, Camera, Group)
        .outerjoin(Camera, Car.camera_id == Camera.id)
        .outerjoin(Group, Car.village_id == Group.id)
        .where(*filters, bangkok_date_expr.in_(page_dates))
        .order_by(Car.time_detect.asc())
    )

    by_date: dict[date, dict[tuple[str, str], list[tuple[Car, Camera, Group]]]] = defaultdict(lambda: defaultdict(list))
    for car, camera, village in detail_result.all():
        local_date = car.time_detect.astimezone(BANGKOK_TZ).date()
        by_date[local_date][(car.license_plate, car.province)].append((car, camera, village))

    day_entries: list[RouteTrackingDayEntry] = []
    for d in page_dates:
        groups = by_date.get(d, {})
        sorted_groups = sorted(groups.items(), key=lambda item: item[1][0][0].time_detect)

        cars: list[RouteTrackingCarGroup] = []
        for (plate, province_value), rows in sorted_groups:
            entries = [
                RouteTrackingDetectionEntry(
                    detection_id=car.id,
                    camera_id=camera.id if camera is not None else None,
                    camera_name=camera.name if camera is not None else car.camera_name,
                    village_id=car.village_id,
                    village_name=village.name if village is not None else "-",
                    lat=camera.lat if camera is not None else car.camera_lat,
                    long=camera.long if camera is not None else car.camera_long,
                    is_camera_deleted=camera is None,
                    direction=car.direction,
                    time_detect=car.time_detect,
                    color=car.color,
                    is_blacklist=car.is_blacklist,
                    is_whitelist=car.is_whitelist,
                    image_crop=str(
                        request.url_for("get_detection_image", detection_id=car.id, variant="crop")
                    ),
                    image_full=str(
                        request.url_for("get_detection_image", detection_id=car.id, variant="full")
                    ),
                )
                for car, camera, village in rows
            ]
            cars.append(
                RouteTrackingCarGroup(
                    license_plate=plate,
                    province=province_value,
                    detection_count=len(entries),
                    detections=entries,
                )
            )

        day_entries.append(RouteTrackingDayEntry(date=d, cars=cars))

    return RouteTrackingRead(
        items=day_entries,
        total_dates=total_dates,
        total_detections=total_detections,
        page=page,
        page_size=page_size,
    )

async def cleanup_orphaned_images(db: AsyncSession) -> int:
    settings = get_settings()
    storage_root = Path(settings.storage_path)
    if not storage_root.exists():
        return 0

    now = time.time()
    
    def _get_candidate_files() -> dict[uuid.UUID, list[Path]]:
        candidates = {}
        for root, dirs, files in os.walk(storage_root):
            if "avatars" in Path(root).parts:
                continue
                
            for filename in files:
                parts = filename.split("_")
                if len(parts) >= 2:
                    try:
                        car_id = uuid.UUID(parts[0])
                    except ValueError:
                        continue
                        
                    file_path = Path(root) / filename
                    # ลบเฉพาะรูปขยะที่ค้างมาเกิน 1 ชั่วโมง เพื่อไม่ให้กระทบรูปที่กำลังอัปโหลดอยู่
                    if now - file_path.stat().st_mtime > 3600:
                        candidates.setdefault(car_id, []).append(file_path)
        return candidates

    candidates = await run_in_threadpool(_get_candidate_files)
    if not candidates:
        return 0
        
    candidate_ids = list(candidates.keys())
    chunk_size = 500
    valid_ids = set()
    
    for i in range(0, len(candidate_ids), chunk_size):
        chunk = candidate_ids[i:i+chunk_size]
        result = await db.execute(select(Car.id).where(Car.id.in_(chunk)))
        valid_ids.update(result.scalars().all())
        
    orphaned_ids = set(candidate_ids) - valid_ids
    deleted_count = 0
    
    def _delete_files(ids_to_delete: set[uuid.UUID]) -> int:
        count = 0
        for cid in ids_to_delete:
            for path in candidates[cid]:
                try:
                    path.unlink(missing_ok=True)
                    count += 1
                except OSError:
                    pass
        return count
        
    if orphaned_ids:
        deleted_count = await run_in_threadpool(_delete_files, orphaned_ids)
        
    return deleted_count


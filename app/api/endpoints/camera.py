from __future__ import annotations
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.deps import require_roles
from app.db.session import get_db
from app.models.camera import CameraDirection
from app.models.user import User, UserRole
from app.schemas.camera import (
    CameraCreate,
    CameraRead,
    CameraStatusRead,
    CameraStreamTokenRead,
    CameraUpdate,
    CameraVerificationCheckRead,
    OnvifProbeRequest,
    OnvifProbeResponse,
)
from app.schemas.common import MessageResponse, PaginatedResponse
from app.services import camera_service, onvif_service

router = APIRouter(prefix="/cameras", tags=["cameras"])

_WRITE_ROLES = (UserRole.ADMIN, UserRole.SUPERADMIN)
_READ_ROLES = (UserRole.USER, UserRole.ADMIN, UserRole.SUPERADMIN)


@router.post("", response_model=CameraRead, status_code=status.HTTP_201_CREATED)
async def create_camera(
    payload: CameraCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.create_camera(db, request, background_tasks, current_user, payload)


@router.get("", response_model=PaginatedResponse[CameraRead])
async def list_cameras(
    village_id: uuid.UUID | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    direction: CameraDirection | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    current_user: User = Depends(require_roles(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.list_cameras(
        db, current_user, village_id, is_active, direction, page, page_size
    )

@router.post("/resync-all", response_model=MessageResponse)
async def resync_all_cameras(
    background_tasks: BackgroundTasks,
    village_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    await camera_service.resync_all_cameras(db, background_tasks, current_user, village_id)
    return MessageResponse(detail="กำลังดำเนินการ Resync กล้องเบื้องหลัง")


@router.get("/{camera_id}", response_model=CameraRead)
async def get_camera(
    camera_id: uuid.UUID,
    current_user: User = Depends(require_roles(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.get_camera_detail(db, current_user, camera_id)


@router.get("/{camera_id}/status", response_model=CameraStatusRead)
async def get_camera_status(
    camera_id: uuid.UUID,
    current_user: User = Depends(require_roles(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.get_camera_status(db, current_user, camera_id)


@router.get("/{camera_id}/stream-token", response_model=CameraStreamTokenRead)
async def get_camera_stream_token(
    camera_id: uuid.UUID,
    current_user: User = Depends(require_roles(*_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.get_camera_stream_token(db, current_user, camera_id)

@router.patch("/{camera_id}", response_model=CameraRead)
async def update_camera(
    camera_id: uuid.UUID,
    payload: CameraUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.update_camera(db, request, background_tasks, current_user, camera_id, payload)

@router.post("/{camera_id}/verification-check", response_model=CameraVerificationCheckRead)
async def check_camera_verification(
    camera_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    return await camera_service.check_camera_verification_now(db, request, current_user, camera_id)


@router.delete("/{camera_id}", response_model=MessageResponse)
async def delete_camera(
    camera_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    await camera_service.delete_camera(db, request, background_tasks, current_user, camera_id)
    return MessageResponse(detail="ลบกล้องถาวรสำเร็จ")

@router.post("/onvif/probe", response_model=OnvifProbeResponse)
async def probe_onvif_camera(
    payload: OnvifProbeRequest,
    current_user: User = Depends(require_roles(*_WRITE_ROLES)),
):
    return await onvif_service.probe_camera(
        payload.host, payload.port, payload.username, payload.password
    )
import uuid
from typing import Any
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.models.group import Group
from app.core.error_messages import Common, Auth


async def resolve_village_id(
    db: AsyncSession,
    current_user: User,
    requested_village_id: uuid.UUID | None,
) -> uuid.UUID:
    if current_user.role == UserRole.SUPERADMIN:
        if requested_village_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=Common.VILLAGE_ID_REQUIRED_SUPERADMIN,
            )
        result = await db.execute(select(Group).where(Group.id == requested_village_id))
        village = result.scalar_one_or_none()
        if village is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="ไม่พบข้อมูลหมู่บ้านที่ระบุ",
            )
        if not village.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=Auth.VILLAGE_INACTIVE,
            )
        return requested_village_id
    if requested_village_id is not None and requested_village_id != current_user.village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
        )
    return current_user.village_id


def build_scope_filters(
    current_user: User, 
    village_id_filter: uuid.UUID | None,
    model_class: Any
) -> list:
    if current_user.role == UserRole.SUPERADMIN:
        if village_id_filter is not None:
            return [model_class.village_id == village_id_filter]
        return []

    if village_id_filter is not None and village_id_filter != current_user.village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ID_NOT_ALLOWED_FOR_ROLE,
        )
    return [model_class.village_id == current_user.village_id]

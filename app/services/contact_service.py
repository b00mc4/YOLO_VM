from __future__ import annotations
import uuid
from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.contact import Contact, ContactType
from app.models.group import Group
from app.models.user import User, UserRole
from app.schemas.common import PaginatedResponse
from app.schemas.contact import (
    ContactCreate,
    ContactDirectoryEntry,
    ContactRead,
    ContactUpdate,
    UserContactsDetail,
)
from app.services import audit_service
from app.core.contact_format import normalize_and_validate_contact_value
from app.core.error_messages import Common, ContactErrors, UserErrors
from app.core.db_utils import escape_like

from app.core.config import get_settings
settings = get_settings()
from app.core.regex_patterns import _THAI_ENG_PATTERN

async def _get_user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UserErrors.NOT_FOUND)
    return user


def _verify_write_scope(current_user: User, target: User) -> None:
    if current_user.role == UserRole.SUPERADMIN:
        return
    if target.id == current_user.id:
        return
    if current_user.role == UserRole.ADMIN:
        if target.role != UserRole.USER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ContactErrors.SCOPE_ADMIN_ONLY_USER,
            )
        if target.village_id != current_user.village_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ContactErrors.SCOPE_OUTSIDE_VILLAGE,
            )
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=Common.INSUFFICIENT_PERMISSIONS)


def _verify_directory_scope(current_user: User, target_village_id: uuid.UUID | None) -> None:
    if current_user.role == UserRole.SUPERADMIN:
        return
    if target_village_id is None:
        return
    if target_village_id != current_user.village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ContactErrors.DIRECTORY_ACCESS_DENIED,
        )


async def _resolve_target_user(
    db: AsyncSession,
    current_user: User,
    requested_user_id: uuid.UUID | None,
) -> User:
    if requested_user_id is None or requested_user_id == current_user.id:
        return current_user

    target = await _get_user_or_404(db, requested_user_id)
    _verify_write_scope(current_user, target)
    return target


async def _get_contact_or_404(db: AsyncSession, contact_id: uuid.UUID) -> tuple[Contact, User]:
    result = await db.execute(
        select(Contact)
        .options(joinedload(Contact.user))
        .where(Contact.id == contact_id)
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ContactErrors.NOT_FOUND)
    return contact, contact.user


def _verify_contact_write_scope(
    current_user: User,
    contact: Contact,
    owner: User,
) -> None:
    if current_user.role == UserRole.SUPERADMIN:
        return
    if contact.user_id == current_user.id:
        return
    if current_user.role == UserRole.ADMIN:
        if owner.role != UserRole.USER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ContactErrors.SCOPE_ADMIN_ONLY_USER,
            )
        if owner.village_id != current_user.village_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ContactErrors.SCOPE_OUTSIDE_VILLAGE,
            )
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ContactErrors.SCOPE_NOT_OWN_CONTACT,
    )


def _validate_content_type_consistency(content_type: ContactType, custom_label: str | None, value: str | None = None) -> None:
    if content_type == ContactType.OTHER:
        if not custom_label:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ContactErrors.CUSTOM_LABEL_REQUIRED,
            )
        if not _THAI_ENG_PATTERN.fullmatch(custom_label) or (value is not None and not _THAI_ENG_PATTERN.fullmatch(value)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ContactErrors.INVALID_OTHER_FORMAT,
            )
    elif custom_label is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ContactErrors.CUSTOM_LABEL_NOT_ALLOWED,
        )


async def _check_duplicate_content_type(
    db: AsyncSession,
    user_id: uuid.UUID,
    content_type: ContactType,
    exclude_contact_id: uuid.UUID | None = None,
) -> None:
    if content_type == ContactType.OTHER:
        return

    stmt = select(Contact.id).where(
        Contact.user_id == user_id,
        Contact.content_type == content_type,
    )
    if exclude_contact_id is not None:
        stmt = stmt.where(Contact.id != exclude_contact_id)

    result = await db.execute(stmt.limit(1))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ContactErrors.DUPLICATE_CONTENT_TYPE,
        )


async def create_contact(
    db: AsyncSession,
    request: Request,
    current_user: User,
    payload: ContactCreate,
) -> ContactRead:
    target_user = await _resolve_target_user(db, current_user, payload.user_id)

    await _check_duplicate_content_type(db, target_user.id, payload.content_type)

    count_result = await db.execute(
        select(func.count()).select_from(Contact).where(Contact.user_id == target_user.id)
    )
    if count_result.scalar_one() >= settings.contact_max_per_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ContactErrors.max_contacts_reached(settings.contact_max_per_user),
        )

    contact = Contact(
        user_id=target_user.id,
        content_type=payload.content_type,
        custom_label=payload.custom_label,
        value=payload.value,
    )
    db.add(contact)

    await audit_service.log_action(
        db,
        request,
        action="contact_create",
        detail=f"added contact ({payload.content_type.value}) for user_id={target_user.id}",
        user_id=current_user.id,
        village_id=target_user.village_id,
    )

    await db.commit()
    await db.refresh(contact)
    return ContactRead.model_validate(contact)


async def get_user_contacts_detail(
    db: AsyncSession,
    current_user: User,
    user_id: uuid.UUID,
) -> UserContactsDetail:
    result = await db.execute(
        select(User, Group.name)
        .outerjoin(Group, User.village_id == Group.id)
        .where(User.id == user_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UserErrors.NOT_FOUND)

    target_user, village_name = row
    _verify_directory_scope(current_user, target_user.village_id)

    contacts_result = await db.execute(
        select(Contact)
        .where(Contact.user_id == target_user.id)
        .order_by(Contact.created_at.desc())
    )
    contacts = contacts_result.scalars().all()

    return UserContactsDetail(
        user_id=target_user.id,
        username=target_user.username,
        fullname=target_user.fullname,
        village_id=target_user.village_id,
        village_name=village_name,
        contacts=[ContactRead.model_validate(contact) for contact in contacts],
    )


def _build_directory_scope_filter(current_user: User, village_id_filter: uuid.UUID | None):
    if current_user.role == UserRole.SUPERADMIN:
        if village_id_filter is None:
            return None
        return or_(User.village_id == village_id_filter, User.role == UserRole.SUPERADMIN)
    return or_(User.village_id == current_user.village_id, User.role == UserRole.SUPERADMIN)


async def list_contact_directory(
    db: AsyncSession,
    current_user: User,
    village_id_filter: uuid.UUID | None,
    search: str | None,
    page: int,
    page_size: int,
) -> PaginatedResponse[ContactDirectoryEntry]:
    stmt = (
        select(User, Group.name, func.count(Contact.id).label("contact_count"))
        .outerjoin(Group, User.village_id == Group.id)
        .outerjoin(Contact, User.id == Contact.user_id)
        .group_by(User.id, Group.id)
    )
    count_stmt = select(func.count()).select_from(User)

    scope_filter = _build_directory_scope_filter(current_user, village_id_filter)
    if scope_filter is not None:
        stmt = stmt.where(scope_filter)
        count_stmt = count_stmt.where(scope_filter)

    if search:
        pattern = f"%{escape_like(search)}%"
        search_filter = or_(User.fullname.ilike(pattern, escape="\\"), User.username.ilike(pattern, escape="\\"))
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    stmt = stmt.order_by(User.fullname).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(stmt)
    rows = result.all()

    items = [
        ContactDirectoryEntry(
            user_id=user.id,
            username=user.username,
            fullname=user.fullname,
            role=user.role,
            village_id=user.village_id,
            village_name=village_name,
            contact_count=contact_count,
        )
        for user, village_name, contact_count in rows
    ]

    return PaginatedResponse[ContactDirectoryEntry](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def update_contact(
    db: AsyncSession,
    request: Request,
    current_user: User,
    contact_id: uuid.UUID,
    payload: ContactUpdate,
) -> ContactRead:
    contact, owner = await _get_contact_or_404(db, contact_id)
    _verify_contact_write_scope(current_user, contact, owner)

    update_data = payload.model_dump(exclude_unset=True)
    merged_content_type = update_data.get("content_type", contact.content_type)

    if merged_content_type != ContactType.OTHER and "custom_label" not in update_data:
        update_data["custom_label"] = None

    if "content_type" in update_data:
        await _check_duplicate_content_type(
            db, owner.id, merged_content_type, exclude_contact_id=contact.id
        )

    if "content_type" in update_data or "value" in update_data:
        merged_value = update_data.get("value", contact.value)
        try:
            update_data["value"] = normalize_and_validate_contact_value(merged_content_type, merged_value)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    for field, value in update_data.items():
        setattr(contact, field, value)

    _validate_content_type_consistency(contact.content_type, contact.custom_label, contact.value)

    await audit_service.log_action(
        db,
        request,
        action="contact_update",
        detail=f"updated contact ({contact.content_type.value}) id={contact.id}",
        user_id=current_user.id,
        village_id=owner.village_id,
    )

    await db.commit()
    await db.refresh(contact)
    return ContactRead.model_validate(contact)


async def delete_contact(
    db: AsyncSession,
    request: Request,
    current_user: User,
    contact_id: uuid.UUID,
) -> None:
    contact, owner = await _get_contact_or_404(db, contact_id)
    _verify_contact_write_scope(current_user, contact, owner)

    await audit_service.log_action(
        db,
        request,
        action="contact_delete",
        detail=f"removed contact ({contact.content_type.value}) id={contact.id}",
        user_id=current_user.id,
        village_id=owner.village_id,
    )

    await db.delete(contact)
    await db.commit()
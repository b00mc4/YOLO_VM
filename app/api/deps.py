from __future__ import annotations
from datetime import timezone
import secrets
import uuid
import jwt
from fastapi import Depends, HTTPException, Request, status, Query
from fastapi.security import APIKeyHeader, OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import get_settings
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User, UserRole
from app.models.group import Group
from app.core.rate_limit import get_rate_limiter
from app.core.error_messages import Auth, Common
from app.core.session_manager import session_manager
from app.services import audit_service
from app.core.request_utils import get_client_ip

settings = get_settings()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
api_key_scheme = APIKeyHeader(name=settings.api_key_header_name, auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}

_API_KEY_FAILURE_LIMIT = 3
_API_KEY_FAILURE_WINDOW_SECONDS = 5 * 60

async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
):
    try:
        user_id, issued_at, jti = decode_access_token(token)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.COULD_NOT_VALIDATE_CREDENTIALS,
            headers=_UNAUTHORIZED_HEADERS,
        )

    if not jti or not session_manager.is_valid_session(user_id, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="เซสชันหมดอายุ หรือมีการเข้าสู่ระบบจากเครื่องอื่นเกินจำนวนที่กำหนด",
            headers=_UNAUTHORIZED_HEADERS,
        )

    result = await db.execute(
        select(User, Group.is_active.label("village_is_active"))
        .outerjoin(Group, User.village_id == Group.id)
        .where(User.id == user_id)
    )
    row = result.one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.COULD_NOT_VALIDATE_CREDENTIALS,
            headers=_UNAUTHORIZED_HEADERS,
        )

    user, village_is_active = row


    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.ACCOUNT_INACTIVE,
            headers=_UNAUTHORIZED_HEADERS,
        )

    if not user.is_verify:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.ACCOUNT_NOT_VERIFIED,
            headers=_UNAUTHORIZED_HEADERS,
        )
    
    if user.role != UserRole.SUPERADMIN and not village_is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.VILLAGE_INACTIVE,
            headers=_UNAUTHORIZED_HEADERS,
        )

    pca = user.password_changed_at.replace(microsecond=0)
    if pca.tzinfo is None:
        pca = pca.replace(tzinfo=timezone.utc)
    if issued_at < pca:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.SESSION_REVOKED_PASSWORD_CHANGED,
            headers=_UNAUTHORIZED_HEADERS,
        )

    request.state.user = user
    return user

async def get_current_user_from_query(
    request: Request,
    token: str | None = Query(None),
    header_token: str | None = Depends(oauth2_scheme_optional),
    db: AsyncSession = Depends(get_db),
):
    actual_token = token or header_token
    if not actual_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.COULD_NOT_VALIDATE_CREDENTIALS,
            headers=_UNAUTHORIZED_HEADERS,
        )
    return await get_current_user(request, token=actual_token, db=db)

def require_roles(*roles: UserRole):
    async def checker(user: User = Depends(get_current_user)):
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=Common.INSUFFICIENT_PERMISSIONS,
            )
        return user

    return checker


def verify_village_scope(user: User, target_village_id: uuid.UUID) -> None:
    if user.role == UserRole.SUPERADMIN:
        return
    if user.village_id != target_village_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=Common.VILLAGE_ACCESS_DENIED,
        )


async def verify_api_key(
    request: Request,
    api_key: str | None = Depends(api_key_scheme),
    db: AsyncSession = Depends(get_db),
) -> None:
    if api_key is None or not secrets.compare_digest(api_key, settings.api_key):
        get_rate_limiter().check(
            f"api_key_rejected:ip:{get_client_ip(request)}",
            _API_KEY_FAILURE_LIMIT,
            _API_KEY_FAILURE_WINDOW_SECONDS,
        )

        await audit_service.log_action(
            db,
            request,
            action="api_key_rejected",
            detail=f"invalid or missing API key on {request.method} {request.url.path}",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=Auth.INVALID_API_KEY,
        )


def rate_limit_by_ip(prefix: str, limit: int, window_seconds: float):
    async def checker(request: Request):
        client_ip = get_client_ip(request)
        get_rate_limiter().check(f"{prefix}:ip:{client_ip}", limit, window_seconds)

    return checker
from __future__ import annotations
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.security import OAuth2PasswordRequestForm
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    EmailChangeConfirm,
    EmailChangeConfirmResponse,
    ForgotPasswordRequest,
    LoginResponse,
    SetPasswordRequest,
    SetPasswordResponse,
    TokenResponse,
    VerifyTokenRequest,
    ActiveSessionsResponse
)
from app.schemas.common import MessageResponse
from app.services import auth_service
from app.api.deps import get_current_user, rate_limit_by_ip
from app.core.error_messages import Auth
from app.core.rate_limit import get_rate_limiter, RateLimitExceeded

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

_VERIFY_SET_PASSWORD_TOKEN_IP_LIMIT = 20
_VERIFY_SET_PASSWORD_TOKEN_IP_WINDOW_SECONDS = 10 * 60

_LOGIN_IP_LIMIT = 10
_LOGIN_IP_WINDOW_SECONDS = 60
_REFRESH_IP_LIMIT = 30
_REFRESH_IP_WINDOW_SECONDS = 60
_LOGOUT_IP_LIMIT = 20
_LOGOUT_IP_WINDOW_SECONDS = 60
_FORGOT_PASSWORD_IP_LIMIT = 20
_FORGOT_PASSWORD_IP_WINDOW_SECONDS = 60 * 60

_SET_PASSWORD_IP_LIMIT = 20
_SET_PASSWORD_IP_WINDOW_SECONDS = 10 * 60
_CONFIRM_EMAIL_CHANGE_IP_LIMIT = 20
_CONFIRM_EMAIL_CHANGE_IP_WINDOW_SECONDS = 10 * 60
_CHANGE_PASSWORD_IP_LIMIT = 5
_CHANGE_PASSWORD_IP_WINDOW_SECONDS = 60 * 60
_LEGACY_REFRESH_COOKIE_PATH = "/api/auth/refresh"

def _delete_refresh_cookie(response: Response, path: str) -> None:
    response.delete_cookie(
        key="refresh_token",
        domain=settings.cookie_domain,
        path=path,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


def _delete_legacy_refresh_cookie(response: Response) -> None:
    if settings.auth_refresh_cookie_path != _LEGACY_REFRESH_COOKIE_PATH:
        _delete_refresh_cookie(response, _LEGACY_REFRESH_COOKIE_PATH)


def _set_refresh_cookie(response: Response, raw_refresh_token: str, remember_me: bool) -> None:
    cookie_kwargs = {}
    if remember_me:
        cookie_kwargs["max_age"] = settings.refresh_token_expire_days * 24 * 60 * 60

    _delete_legacy_refresh_cookie(response)
    response.set_cookie(
        key="refresh_token",
        value=raw_refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path=settings.auth_refresh_cookie_path,
        **cookie_kwargs,
    )


def _clear_refresh_cookie(response: Response) -> None:
    _delete_refresh_cookie(response, settings.auth_refresh_cookie_path)
    _delete_legacy_refresh_cookie(response)


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit_by_ip("login", _LOGIN_IP_LIMIT, _LOGIN_IP_WINDOW_SECONDS))],
)
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    remember_me: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    user = await auth_service.authenticate_user(db, request, form_data.username, form_data.password, remember_me)
    access_token, raw_refresh_token = await auth_service.issue_tokens(db, request, user, remember_me)
    _set_refresh_cookie(response, raw_refresh_token, remember_me)
    expires_in_sec = settings.access_token_expire_minutes * 60
    return LoginResponse(access_token=access_token, user=user, expires_in=expires_in_sec)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limit_by_ip("refresh", _REFRESH_IP_LIMIT, _REFRESH_IP_WINDOW_SECONDS))],
)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """แลก Refresh TOken โดยใช้ Cookie"""
    raw_refresh_token = request.cookies.get("refresh_token")
    if raw_refresh_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=Auth.MISSING_REFRESH_TOKEN)

    tokens = await auth_service.rotate_refresh_token(db, request, raw_refresh_token)
    if tokens.refresh_token is not None:
        _set_refresh_cookie(response, tokens.refresh_token, tokens.remember_me)
    expires_in_sec = settings.access_token_expire_minutes * 60
    return TokenResponse(access_token=tokens.access_token, expires_in=expires_in_sec)


@router.post(
    "/logout",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit_by_ip("logout", _LOGOUT_IP_LIMIT, _LOGOUT_IP_WINDOW_SECONDS))],
)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    raw_refresh_token = request.cookies.get("refresh_token")
    if raw_refresh_token is not None:
        await auth_service.revoke_refresh_token(db, request, raw_refresh_token)
    _clear_refresh_cookie(response)
    return MessageResponse(detail="ออกจากระบบสำเร็จ")


@router.post(
    "/forgot-password",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit_by_ip("forgot_password_ip", _FORGOT_PASSWORD_IP_LIMIT, _FORGOT_PASSWORD_IP_WINDOW_SECONDS))],
)
async def forgot_password(
    payload: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    rate_limiter = get_rate_limiter()
    
    normalized_email = payload.email.strip().lower()

    try:
        rate_limiter.check(f"forgot_cooldown:{normalized_email}", 1, 300)
    except RateLimitExceeded as e:
        minutes_left = int(e.retry_after_seconds // 60) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"กรุณารออีก {minutes_left} นาที ก่อนขอรหัสผ่านใหม่ได้อีกครั้ง"
        ) from e
        
    try:
        rate_limiter.check(f"forgot_daily:{normalized_email}", 3, 86400)
    except RateLimitExceeded:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="คุณขอรหัสผ่านใหม่เกิน 3 ครั้งแล้ว กรุณาลองใหม่ในวันพรุ่งนี้"
        ) from None

    await auth_service.request_password_reset(db, background_tasks, normalized_email)


@router.post(
    "/set-password/verify-token",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(rate_limit_by_ip(
            "verify_set_password_token",
            _VERIFY_SET_PASSWORD_TOKEN_IP_LIMIT,
            _VERIFY_SET_PASSWORD_TOKEN_IP_WINDOW_SECONDS,
        ))
    ],
)
async def verify_set_password_token(
    payload: VerifyTokenRequest,
    db: AsyncSession = Depends(get_db),
):
    await auth_service.verify_set_password_token(db, payload.token)


@router.post(
    "/set-password",
    response_model=SetPasswordResponse,
    dependencies=[Depends(rate_limit_by_ip("set_password", _SET_PASSWORD_IP_LIMIT, _SET_PASSWORD_IP_WINDOW_SECONDS))],
)
async def set_password(
    payload: SetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """ต่อจาก endpoint forgot-password เอา token url มาใส่ในนี้ user สามารถตั้งรหัสผ่านเองได้"""
    username = await auth_service.set_password(db, payload.token, payload.new_password)
    return SetPasswordResponse(
        detail=f"ตั้งรหัสผ่านสำเร็จ สามารถเข้าสู่ระบบได้เลยโดยใช้ username: {username}",
        username=username,
    )


@router.post(
    "/confirm-email-change",
    response_model=EmailChangeConfirmResponse,
    dependencies=[Depends(rate_limit_by_ip("confirm_email_change", _CONFIRM_EMAIL_CHANGE_IP_LIMIT, _CONFIRM_EMAIL_CHANGE_IP_WINDOW_SECONDS))],
)
async def confirm_email_change(
    payload: EmailChangeConfirm,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    username, email = await auth_service.confirm_email_change(db, request, payload.token)
    return EmailChangeConfirmResponse(
        detail=f"เปลี่ยนอีเมลสำเร็จ อีเมลใหม่ของคุณคือ {email}",
        username=username,
        email=email,
    )


@router.post(
    "/change-password",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limit_by_ip("change_password", _CHANGE_PASSWORD_IP_LIMIT, _CHANGE_PASSWORD_IP_WINDOW_SECONDS))],
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """อันนี้คือเปลี่ยนรหัสผ่านเองได้เมื่อ login เข้ามาแล้ว"""
    await auth_service.change_password(
        db=db,
        request=request,
        current_user=current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    _clear_refresh_cookie(response)
    return MessageResponse(detail="เปลี่ยนรหัสผ่านสำเร็จ")

@router.get(
    "/sessions",
    response_model=ActiveSessionsResponse,
)
async def get_active_sessions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """ดูเซสชันอุปกรณ์ที่กำลังเข้าสู่ระบบอยู่"""
    return await auth_service.get_active_sessions(db, request, current_user)
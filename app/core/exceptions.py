from __future__ import annotations
import logging
import math
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from app.schemas.common import ErrorResponse
from app.core.rate_limit import RateLimitExceeded
from app.core.account_lockout import AccountLocked
from app.core.error_messages import Common

logger = logging.getLogger(__name__)

_SQLSTATE_TO_RESPONSE: dict[str, tuple[int, str]] = {
    "23505": (status.HTTP_409_CONFLICT, Common.RESOURCE_ALREADY_EXISTS),
    "23503": (status.HTTP_400_BAD_REQUEST, Common.REFERENCED_RESOURCE_NOT_FOUND),
    "23514": (status.HTTP_400_BAD_REQUEST, Common.CONSTRAINT_VIOLATION),
}
_DEFAULT_INTEGRITY_RESPONSE = (status.HTTP_500_INTERNAL_SERVER_ERROR, Common.INTERNAL_SERVER_ERROR)
_PYDANTIC_VALUE_ERROR_TYPE = "value_error"


async def integrity_error_handler(request: Request, exc: IntegrityError):
    sqlstate = getattr(exc.orig, "sqlstate", None)
    status_code, detail = _SQLSTATE_TO_RESPONSE.get(sqlstate, _DEFAULT_INTEGRITY_RESPONSE)

    if status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.exception("Unhandled IntegrityError (sqlstate=%s): %s", sqlstate, exc.orig)
    else:
        logger.warning("IntegrityError sqlstate=%s detail=%s", sqlstate, exc.orig)

    return JSONResponse(status_code=status_code, content=ErrorResponse(detail=detail).model_dump())


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    logger.warning("Rate limit exceeded on %s %s", request.method, request.url.path)
    retry_after = int(exc.retry_after_seconds) + 1
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=ErrorResponse(detail=Common.TOO_MANY_REQUESTS).model_dump(),
        headers={"Retry-After": str(retry_after)},
    )


async def account_locked_handler(request: Request, exc: AccountLocked):
    logger.warning("Login attempt blocked on locked account: %s %s", request.method, request.url.path)
    retry_after = math.ceil(exc.retry_after_seconds)
    return JSONResponse(
        status_code=status.HTTP_423_LOCKED,
        content=ErrorResponse(
            detail=f"บัญชีถูกล็อคชั่วคราว กรุณาลองใหม่ในอีก {retry_after} วินาที"
        ).model_dump(),
        headers={"Retry-After": str(retry_after)},
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    messages: list[str] = []
    for err in exc.errors():
        if err["type"] == _PYDANTIC_VALUE_ERROR_TYPE:
            messages.append(err["msg"].removeprefix("Value error, "))
            continue

        loc = ".".join(str(part) for part in err["loc"] if part != "body")
        messages.append(f"{loc}: {err['msg']}" if loc else err["msg"])

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(detail="; ".join(messages)).model_dump(),
    )


async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(detail=Common.INTERNAL_SERVER_ERROR).model_dump(),
    )


def register_exception_handlers(app: FastAPI):
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_exception_handler(AccountLocked, account_locked_handler)

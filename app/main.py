from __future__ import annotations
import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.api.router import api_router
from app.core.rate_limit import get_rate_limiter, RateLimitExceeded
from app.core.error_messages import Common
from app.schemas.common import ErrorResponse
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.db.session import async_session_maker, engine
from app.services import ai_vision_service, auth_service, camera_service, camera_verification_service, mediamtx_service, detection_service

_AUTH_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

settings = get_settings()
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

_STARTUP_RESYNC_MAX_ATTEMPTS = 3
_STARTUP_RESYNC_BACKOFF_BASE_SECONDS = 2.0

_MAX_PAYLOAD_SIZE_BYTES = 10 * 1024 * 1024  
_GLOBAL_RATE_LIMIT = 200
_GLOBAL_RATE_LIMIT_WINDOW = 60.0

async def _run_background_loop(name: str, interval: int, task_fn, action_message: str = "cleanup: removed") -> None:
    consecutive_errors = 0
    while True:
        try:
            async with async_session_maker() as db:
                count = await task_fn(db)
            if count:
                logger.info("%s %s %s item(s)", name, action_message, count)
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            logger.exception("%s loop iteration failed (error count: %s)", name, consecutive_errors)

        if consecutive_errors > 0:
            backoff = min(3600, 10 * (2 ** min(consecutive_errors - 1, 10)))
            await asyncio.sleep(backoff)
        else:
            await asyncio.sleep(interval)

async def _startup_camera_resync_background() -> None:
    for attempt in range(1, _STARTUP_RESYNC_MAX_ATTEMPTS + 1):
        try:
            async with async_session_maker() as db:
                await camera_service.resync_all_cameras_on_startup(db)
            return
        except Exception:
            logger.exception(
                "Startup camera resync failed (attempt %s/%s)",
                attempt, _STARTUP_RESYNC_MAX_ATTEMPTS,
            )

        if attempt < _STARTUP_RESYNC_MAX_ATTEMPTS:
            await asyncio.sleep(_STARTUP_RESYNC_BACKOFF_BASE_SECONDS ** attempt)

    logger.error(
        "Startup camera resync gave up after %s attempts; "
        "recover manually via POST /api/cameras/resync-all",
        _STARTUP_RESYNC_MAX_ATTEMPTS,
    )


async def _resume_camera_verification_background() -> None:
    try:
        await camera_verification_service.resume_pending_verifications()
    except Exception:
        logger.exception("Failed to resume pending camera verifications on startup")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    try:
        async with async_session_maker() as db:
            restored = await auth_service.restore_active_sessions(db)
        logger.info("Restored %s active session(s) into memory", restored)
    except Exception:
        logger.exception("Failed to restore active sessions on startup")

    resync_task = asyncio.create_task(_startup_camera_resync_background())
    app.state.startup_resync_task = resync_task

    verification_resume_task = asyncio.create_task(_resume_camera_verification_background())
    app.state.startup_verification_resume_task = verification_resume_task

    clean_auth_task = asyncio.create_task(
        _run_background_loop("Auth", _AUTH_CLEANUP_INTERVAL_SECONDS, auth_service.cleanup_expired_refresh_tokens)
    )
    app.state.startup_clean_auth_task = clean_auth_task

    camera_status_task = asyncio.create_task(
        _run_background_loop(
            "CameraStatus", 
            180, 
            camera_service.check_and_update_camera_statuses,
            action_message="status sync: updated"
        )
    )
    app.state.camera_status_task = camera_status_task

    cleanup_images_task = asyncio.create_task(
        _run_background_loop(
            "CleanupImages",
            24 * 60 * 60,
            detection_service.cleanup_orphaned_images,
            action_message="cleanup: removed orphaned images"
        )
    )
    app.state.startup_cleanup_images_task = cleanup_images_task

    yield

    for task in (resync_task, verification_resume_task, clean_auth_task, camera_status_task, cleanup_images_task):
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    await mediamtx_service.close()
    await ai_vision_service.close()
    await engine.dispose()


app = FastAPI(
    title="License Plate Detection API", 
    lifespan=lifespan
)

@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if "content-length" in request.headers:
        try:
            content_length = int(request.headers["content-length"])
        except ValueError:
            content_length = 0
            
        if content_length > _MAX_PAYLOAD_SIZE_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": f"ขนาดข้อมูลใหญ่เกินไป (สูงสุด {_MAX_PAYLOAD_SIZE_BYTES // (1024 * 1024)}MB)"}
            )

    is_health = request.url.path.startswith("/health")
    is_sse = request.url.path.startswith("/api/sse")
    is_post_detection = request.url.path.rstrip("/") == "/api/detections" and request.method == "POST"

    if not is_health and not is_sse and not is_post_detection:
        client_ip = request.client.host if request.client else "127.0.0.1"
        limiter = get_rate_limiter()
        try:
            limiter.check(f"global:{client_ip}", limit=_GLOBAL_RATE_LIMIT, window_seconds=_GLOBAL_RATE_LIMIT_WINDOW)
        except RateLimitExceeded as e:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content=ErrorResponse(detail=Common.TOO_MANY_REQUESTS).model_dump(),
                headers={"Retry-After": str(int(e.retry_after_seconds) + 1)},
            )
            
    return await call_next(request)

register_exception_handlers(app)
app.include_router(api_router)
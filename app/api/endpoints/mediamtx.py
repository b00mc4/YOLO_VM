from __future__ import annotations
import uuid
import logging
import jwt
from fastapi import APIRouter, Request, HTTPException, status, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import time
from app.core.config import get_settings
from app.db.session import get_db
from app.models.user import User
from app.models.camera import Camera
from app.services import mediamtx_auth_service

router = APIRouter(prefix="/mediamtx", tags=["mediamtx"])
settings = get_settings()
logger = logging.getLogger(__name__)

_AUTH_CACHE: dict[str, float] = {}
_CACHE_TTL = 10.0

@router.post("/webhook")
async def mediamtx_auth_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    MediaMTX External Authentication Webhook.
    Handles 'read' action for HLS streaming.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = payload.get("action")
    path = payload.get("path")  # camera_id
    query = payload.get("query", "")

    if action != "read":
        return {"status": "ok"}

    token = None
    if query:
        for param in query.split("&"):
            if param.startswith("jwt="):
                token = param.split("=")[1]
                break

    if not token:
        logger.warning(f"MediaMTX Webhook: No JWT token found in query for path {path}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing JWT token")

    cache_key = f"{path}:{token}"
    now = time.time()
    if cache_key in _AUTH_CACHE:
        if now - _AUTH_CACHE[cache_key] < _CACHE_TTL:
            return {"status": "ok"}
        else:
            del _AUTH_CACHE[cache_key]

    try:
        # Decode the ES256 token that we issued in mediamtx_auth_service
        public_key = mediamtx_auth_service._load_private_key().public_key()
        
        # Verify signature and expiration
        decoded = jwt.decode(token, public_key, algorithms=["ES256"])
        
        user_id_str = decoded.get("user_id")
        if not user_id_str:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing user_id in token")
            
        user_id = uuid.UUID(user_id_str)
        camera_id = uuid.UUID(path)
        
    except jwt.ExpiredSignatureError:
        logger.info(f"MediaMTX Webhook: Token expired for path {path}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except Exception as e:
        logger.warning(f"MediaMTX Webhook: Invalid token for path {path}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    if user_id == uuid.UUID(int=0):
        # SYSTEM trigger
        _AUTH_CACHE[cache_key] = time.time()
        return {"status": "ok"}

    # Real-time DB check
    user_result = await db.execute(select(User).where(User.id == user_id, User.is_active == True))
    user = user_result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive or deleted")

    camera_result = await db.execute(select(Camera).where(Camera.id == camera_id, Camera.is_active == True))
    camera = camera_result.scalars().first()
    if not camera:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Camera inactive or deleted")

    # Check village scope
    from app.services.camera_service import verify_village_scope
    try:
        verify_village_scope(user, camera.village_id)
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User lost access to this camera")

    # Cleanup stale cache to prevent memory leak
    if len(_AUTH_CACHE) > 5000:
        current_time = time.time()
        stale = [k for k, v in _AUTH_CACHE.items() if current_time - v > _CACHE_TTL]
        for k in stale:
            _AUTH_CACHE.pop(k, None)

    _AUTH_CACHE[cache_key] = time.time()
    return {"status": "ok"}

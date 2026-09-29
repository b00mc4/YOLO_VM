from __future__ import annotations
import logging
import time
import uuid
from datetime import datetime
import httpx
from fastapi import status
from app.core.background import spawn_background
from app.core.config import get_settings
from app.services import mediamtx_auth_service
from app.core.alert_cooldown import InMemorySingleWorkerCooldown

settings = get_settings()
logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 1.5
_TRIGGER_PULL_TIMEOUT_SECONDS = 3.0

_client: httpx.AsyncClient | None = None

def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


_SOURCE_ON_DEMAND_START_TIMEOUT_SECONDS = 15.0
_TRIGGER_COOLDOWN_SECONDS = 30.0
_trigger_cooldown = InMemorySingleWorkerCooldown()


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(settings.mediamtx_api_user, settings.mediamtx_api_password)


def _path_name(camera_id: uuid.UUID) -> str:
    return str(camera_id)


def _path_config(source_rtsp_url: str) -> dict:
    return {
        "source": source_rtsp_url,
        "sourceOnDemand": False,
        "sourceOnDemandStartTimeout": f"{int(_SOURCE_ON_DEMAND_START_TIMEOUT_SECONDS)}s",
        "sourceProtocol": "tcp",
    }


def derive_stream_url(camera_id: uuid.UUID, user_id: uuid.UUID) -> tuple[str, datetime]:
    token, expires_at = mediamtx_auth_service.issue_stream_token_with_expiry(camera_id, user_id)
    return f"/mediamtx/{camera_id}/index.m3u8?jwt={token}", expires_at


async def upsert_path(camera_id: uuid.UUID, source_rtsp_url: str) -> bool:
    path_name = _path_name(camera_id)
    base_url = settings.mediamtx_api_url.rstrip("/")
    config = _path_config(source_rtsp_url)

    try:
        response = await get_client().post(
            f"{base_url}/v3/config/paths/replace/{path_name}", json=config, auth=_auth()
        )
        if response.status_code == status.HTTP_404_NOT_FOUND:
            response = await get_client().post(
                f"{base_url}/v3/config/paths/add/{path_name}", json=config, auth=_auth()
            )
    except httpx.HTTPError as exc:
        logger.error("MediaMTX upsert_path request failed for %s: %s", camera_id, exc)
        return False

    if response.status_code >= status.HTTP_400_BAD_REQUEST:
        logger.error(
            "MediaMTX upsert_path rejected for %s: status=%s body=%s",
            camera_id, response.status_code, response.text,
        )
        return False

    return True


async def remove_path(camera_id: uuid.UUID) -> bool:
    path_name = _path_name(camera_id)
    url = f"{settings.mediamtx_api_url.rstrip('/')}/v3/config/paths/delete/{path_name}"

    try:
        response = await get_client().delete(url, auth=_auth())
    except httpx.HTTPError as exc:
        logger.error("MediaMTX remove_path request failed for %s: %s", camera_id, exc)
        return False

    if response.status_code >= status.HTTP_400_BAD_REQUEST and response.status_code != status.HTTP_404_NOT_FOUND:
        logger.error(
            "MediaMTX remove_path unexpected status for %s: status=%s body=%s",
            camera_id, response.status_code, response.text,
        )
        return False

    return True


async def _get_path_info(camera_id: uuid.UUID) -> dict | None:
    path_name = _path_name(camera_id)
    url = f"{settings.mediamtx_api_url.rstrip('/')}/v3/paths/get/{path_name}"

    try:
        response = await get_client().get(url, auth=_auth())
    except httpx.HTTPError as exc:
        logger.warning("MediaMTX get_path_info request failed for %s: %s", camera_id, exc)
        return None

    if response.status_code == status.HTTP_404_NOT_FOUND:
        return {"exists": False}

    if response.status_code >= status.HTTP_400_BAD_REQUEST:
        logger.warning(
            "MediaMTX get_path_info unexpected status for %s: status=%s body=%s",
            camera_id, response.status_code, response.text,
        )
        return None

    body = response.json()
    return {
        "exists": True,
        "ready": bool(body.get("ready", False)),
        "bytes_received": int(body.get("bytesReceived", 0)),
    }


async def _trigger_on_demand_pull(camera_id: uuid.UUID) -> None:
    token = mediamtx_auth_service.issue_stream_token(camera_id, uuid.UUID(int=0))
    parsed = httpx.URL(settings.mediamtx_api_url)
    internal_hls_base = f"{parsed.scheme}://{parsed.host}:8888"
    playlist_url = f"{internal_hls_base}/{camera_id}/index.m3u8?jwt={token}"

    try:
        await get_client().get(playlist_url, timeout=_TRIGGER_PULL_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("MediaMTX trigger pull failed for camera %s: %s", camera_id, exc)



async def get_source_state(camera_id: uuid.UUID) -> bool | None:
    """
    สถานะ source ของกล้องใน MediaMTX สำหรับลูปเช็คสถานะเบื้องหลัง
    True = online, False = offline, None = ไม่รู้ (เรียก API ไม่ได้/timeout)
    ผู้เรียกไม่ควรเปลี่ยนสถานะกล้องเมื่อได้ None เพื่อกันแจ้งเตือน offline/online หลอก
    """
    info = await _get_path_info(camera_id)
    if info is None:
        return None
    return bool(info.get("exists") and info.get("ready"))


_ALIVE_CACHE: dict[str, dict] = {}
_ALIVE_CACHE_TTL = 3.0

async def check_source_alive(camera_id: uuid.UUID) -> tuple[bool, bool]:
    cid_str = str(camera_id)
    now = time.time()

    if cid_str in _ALIVE_CACHE:
        entry = _ALIVE_CACHE[cid_str]
        if now - entry["time"] < _ALIVE_CACHE_TTL:
            return entry["data"]

    _MAX_CACHE_SIZE = 1000
    if len(_ALIVE_CACHE) > _MAX_CACHE_SIZE:
        stale = [k for k, v in _ALIVE_CACHE.items() if now - v["time"] > _ALIVE_CACHE_TTL]
        for k in stale:
            _ALIVE_CACHE.pop(k, None)

    baseline = await _get_path_info(camera_id)

    result = False, False
    if baseline is None or not baseline.get("exists"):
        result = False, False
    elif baseline["ready"]:
        result = True, False
    else:
        if _trigger_cooldown.allow(cid_str, cooldown_seconds=_TRIGGER_COOLDOWN_SECONDS):
            logger.info("Triggering MediaMTX on-demand pull for camera_id=%s in background", camera_id)
            spawn_background(_trigger_on_demand_pull(camera_id))
        result = False, True

    _ALIVE_CACHE[cid_str] = {"time": time.time(), "data": result}
    return result


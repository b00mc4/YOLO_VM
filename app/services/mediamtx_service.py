from __future__ import annotations
import asyncio
import logging
import time
import uuid
import httpx
from app.core.config import get_settings
from app.services import mediamtx_auth_service
from app.core.alert_cooldown import InMemorySingleWorkerCooldown
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
_COLD_START_POLL_INTERVAL_SECONDS = 1.0
_COLD_START_POLL_BUFFER_SECONDS = 2.0
_COLD_START_MAX_WAIT_SECONDS = _SOURCE_ON_DEMAND_START_TIMEOUT_SECONDS + _COLD_START_POLL_BUFFER_SECONDS
_BYTES_CONFIRM_WINDOW_SECONDS = 2.0
_TRIGGER_COOLDOWN_SECONDS = 30.0
_trigger_cooldown = InMemorySingleWorkerCooldown()
_TRIGGER_COOLDOWN_SECONDS = 30.0
_trigger_cooldown = InMemorySingleWorkerCooldown()


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(settings.mediamtx_api_user, settings.mediamtx_api_password)


def _path_name(camera_id: uuid.UUID) -> str:
    return str(camera_id)


def derive_stream_url(camera_id: uuid.UUID, user_id: uuid.UUID) -> str:
    token = mediamtx_auth_service.issue_stream_token(camera_id, user_id)
    return f"/mediamtx/{camera_id}/index.m3u8?jwt={token}"


async def upsert_path(camera_id: uuid.UUID, source_rtsp_url: str) -> bool:
    path_name = _path_name(camera_id)
    url = f"{settings.mediamtx_api_url.rstrip('/')}/v3/config/paths/replace/{path_name}"

    try:
        response = await get_client().post(
            url,
            json={
                "source": source_rtsp_url,
                "sourceOnDemand": False,
                "sourceOnDemandStartTimeout": f"{int(_SOURCE_ON_DEMAND_START_TIMEOUT_SECONDS)}s",
                "sourceProtocol": "tcp",
            },
            auth=_auth(),
        )
    except httpx.HTTPError as exc:
        logger.error("MediaMTX upsert_path request failed for %s: %s", camera_id, exc)
        return False

    if response.status_code == 404:
        add_url = f"{settings.mediamtx_api_url.rstrip('/')}/v3/config/paths/add/{path_name}"
        try:
            response = await get_client().post(
                add_url,
                json={
                    "source": source_rtsp_url,
                    "sourceOnDemand": False,
                    "sourceOnDemandStartTimeout": f"{int(_SOURCE_ON_DEMAND_START_TIMEOUT_SECONDS)}s",
                    "sourceProtocol": "tcp",
                },
                auth=_auth(),
            )
        except httpx.HTTPError as exc:
            logger.error("MediaMTX upsert_path (add fallback) request failed for %s: %s", camera_id, exc)
            return False

    if response.status_code == 404:
        add_url = f"{settings.mediamtx_api_url.rstrip('/')}/v3/config/paths/add/{path_name}"
        try:
            response = await get_client().post(
                add_url,
                json={
                    "source": source_rtsp_url,
                    "sourceOnDemand": False,
                    "sourceOnDemandStartTimeout": f"{int(_SOURCE_ON_DEMAND_START_TIMEOUT_SECONDS)}s",
                    "sourceProtocol": "tcp",
                },
                auth=_auth(),
            )
        except httpx.HTTPError as exc:
            logger.error("MediaMTX upsert_path (add fallback) request failed for %s: %s", camera_id, exc)
            return False

    if response.status_code >= 400:
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

    if response.status_code >= 400 and response.status_code != 404:
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

    if response.status_code == 404:
        return {"exists": False}

    if response.status_code >= 400:
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
        await _client.get(playlist_url, timeout=_TRIGGER_PULL_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("MediaMTX trigger pull failed for camera %s: %s", camera_id, exc)



_ALIVE_CACHE: dict[str, dict] = {}
_ALIVE_CACHE_TTL = 3.0

async def check_source_alive(camera_id: uuid.UUID) -> tuple[bool, bool]:
    cid_str = str(camera_id)
    now = time.time()

    if cid_str in _ALIVE_CACHE:
        entry = _ALIVE_CACHE[cid_str]
        if now - entry["time"] < _ALIVE_CACHE_TTL:
            return entry["data"]

    # Clear stale cache occasionally
    if len(_ALIVE_CACHE) > 1000:
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
            asyncio.create_task(_trigger_on_demand_pull(camera_id))
        result = False, True

    _ALIVE_CACHE[cid_str] = {"time": time.time(), "data": result}
    return result
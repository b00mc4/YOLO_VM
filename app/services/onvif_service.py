from __future__ import annotations
import asyncio
import logging
from fastapi import HTTPException, status
from fastapi.concurrency import run_in_threadpool
from onvif import ONVIFCamera
from yarl import URL
from zeep.exceptions import Fault, TransportError
from zeep.transports import Transport
from app.core.error_messages import OnvifErrors
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_SECONDS = 15.0
_SOAP_REQUEST_TIMEOUT_SECONDS = 10.0
_STREAM_SETUP = {
    "Stream": "RTP-Unicast",
    "Transport": {"Protocol": "RTSP"},
}


def _fetch_device_info(camera: ONVIFCamera) -> tuple[str | None, str | None]:
    try:
        device_info = camera.devicemgmt.GetDeviceInformation()
    except Exception:
        return None, None
    return getattr(device_info, "Manufacturer", None), getattr(device_info, "Model", None)


def _fetch_stream_uri(media_service, profile_token: str) -> str:
    request = media_service.create_type("GetStreamUri")
    request.ProfileToken = profile_token
    request.StreamSetup = _STREAM_SETUP
    response = media_service.GetStreamUri(request)
    return response.Uri


def _build_profile_entry(profile, rtsp_uri: str) -> dict:
    video_encoder = getattr(profile, "VideoEncoderConfiguration", None)
    resolution = getattr(video_encoder, "Resolution", None) if video_encoder else None

    return {
        "profile_token": profile.token,
        "name": profile.Name,
        "encoding": getattr(video_encoder, "Encoding", None) if video_encoder else None,
        "width": getattr(resolution, "Width", None) if resolution else None,
        "height": getattr(resolution, "Height", None) if resolution else None,
        "rtsp_uri": rtsp_uri,
    }


def _with_rtsp_credentials(rtsp_uri: str, username: str, password: str) -> str:
    if not username:
        return rtsp_uri
    return str(URL(rtsp_uri).with_user(username).with_password(password))


def _probe(host: str, port: int, username: str, password: str) -> dict:
    transport = Transport(
        timeout=_SOAP_REQUEST_TIMEOUT_SECONDS,
        operation_timeout=_SOAP_REQUEST_TIMEOUT_SECONDS,
    )
    camera = ONVIFCamera(host, port, username, password, no_cache=True, transport=transport)

    try:
        manufacturer, model = _fetch_device_info(camera)

        media_service = camera.create_media_service()
        profiles = media_service.GetProfiles()

        if not profiles:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=OnvifErrors.NO_MEDIA_PROFILES,
            )

        profile_results = []
        for profile in profiles:
            rtsp_uri = _fetch_stream_uri(media_service, profile.token)
            
            parsed_rtsp = urlsplit(rtsp_uri)
            netloc = host
            if parsed_rtsp.port:
                netloc += f":{parsed_rtsp.port}"
            rtsp_uri = urlunsplit((parsed_rtsp.scheme, netloc, parsed_rtsp.path, parsed_rtsp.query, parsed_rtsp.fragment))
            
            rtsp_uri = _with_rtsp_credentials(rtsp_uri, username, password)
            profile_results.append(_build_profile_entry(profile, rtsp_uri))

        return {
            "device_manufacturer": manufacturer,
            "device_model": model,
            "profiles": profile_results,
        }
    finally:
        try:
            camera.close()
        except AttributeError:
            pass # บางเวอร์ชันของ onvif-zeep ไม่มีคำสั่ง close()
        except Exception:
            logger.warning(
                "onvif probe failed to close camera session cleanly for host=%s port=%s",
                host, port,
            )


async def probe_camera(host: str, port: int, username: str, password: str) -> dict:
    try:
        return await asyncio.wait_for(
            run_in_threadpool(_probe, host, port, username, password),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        logger.warning("onvif probe timed out for host=%s port=%s", host, port)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=OnvifErrors.CONNECTION_FAILED,
        ) from None
    except Fault as exc:
        message = str(exc).lower()
        if "not authorized" in message or "auth" in message:
            logger.warning("onvif probe auth failed for host=%s port=%s", host, port)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=OnvifErrors.INVALID_CREDENTIALS,
            ) from exc
        logger.warning("onvif probe SOAP fault for host=%s port=%s: %s", host, port, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=OnvifErrors.UNSUPPORTED_OR_UNREACHABLE,
        ) from exc
    except TransportError as exc:
        logger.warning("onvif probe transport error for host=%s port=%s: %s", host, port, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=OnvifErrors.CONNECTION_FAILED,
        ) from exc
    except Exception as exc:
        logger.warning("onvif probe unexpected error for host=%s port=%s: %s", host, port, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=OnvifErrors.UNSUPPORTED_OR_UNREACHABLE,
        ) from exc

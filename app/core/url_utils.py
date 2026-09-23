from __future__ import annotations
import asyncio
from urllib.parse import quote, urlsplit, urlunsplit, urlparse
from app.core.error_messages import CameraErrors

_ALLOWED_SCHEMES = ("rtsp", "rtsps")


def normalize_rtsp_url(raw: str) -> str:
    stripped = raw.strip()
    if not stripped:
        raise ValueError(CameraErrors.INVALID_RTSP_FORMAT)

    parts = urlsplit(stripped)

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(CameraErrors.INVALID_RTSP_FORMAT)

    hostname = parts.hostname
    if not hostname:
        raise ValueError(CameraErrors.INVALID_RTSP_FORMAT)

    userinfo = ""
    if parts.username:
        userinfo = quote(parts.username, safe="")
        if parts.password:
            userinfo += f":{quote(parts.password, safe='')}"
        userinfo += "@"

    netloc = f"{userinfo}{hostname}"
    if parts.port:
        netloc += f":{parts.port}"

    return urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))


def redact_rtsp_url(raw: str) -> str:
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        if not parts.hostname:
            return raw
        
        userinfo = ""
        if parts.username or parts.password:
            userinfo = "***:***@"
            
        netloc = f"{userinfo}{parts.hostname}"
        if parts.port:
            netloc += f":{parts.port}"
            
        query = "***" if parts.query else ""
        return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    except Exception:
        return raw

async def check_rtsp_stream(url: str, timeout: float = 2.0) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port or 554
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout
        )
        
        request = (
            f"DESCRIBE {url} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"Accept: application/sdp\r\n"
            f"User-Agent: LPR-Checker\r\n\r\n"
        )
        writer.write(request.encode('utf-8'))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        response = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        writer.close()
        
        if not response:
            return False
            
        resp_str = response.decode('utf-8', errors='ignore')
        if "404" in resp_str:
            return False
            
        if resp_str.startswith("RTSP/1.0 200") or resp_str.startswith("RTSP/1.0 401"):
            return True
            
        return False
    except Exception:
        return False
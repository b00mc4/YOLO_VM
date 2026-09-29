from __future__ import annotations
import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse
from app.api.deps import require_roles
from app.core.config import get_settings
from app.core.connection_limit import ConnectionLimitExceeded
from app.core.error_messages import RealtimeErrors
from app.core.sse_channel import CLOSE_SENTINEL
from app.core.stream_registry import StreamHandle, stream_registry
from app.models.user import User, UserRole
from app.schemas.presence import PresenceTicketResponse
from app.schemas.sse import SSETicketResponse
from app.services import channel_service, presence_service

router = APIRouter(prefix="/sse", tags=["sse"])

settings = get_settings()

_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.USER, UserRole.SUPERADMIN)
_SECURITY_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.SUPERADMIN)
_PING_INTERVAL_SECONDS = 15
_PADDING_EVENT = {"event": "padding", "data": " " * 4096}
_PING_EVENT = {"event": "ping", "data": ""}
_SSE_HEADERS = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache, no-store, must-revalidate",
}

_ChannelTicket = tuple[uuid.UUID, uuid.UUID | None, datetime | None]


@router.post("/ticket", response_model=SSETicketResponse, status_code=status.HTTP_201_CREATED)
async def create_sse_ticket(
    current_user: User = Depends(require_roles(*_ALLOWED_ROLES)),
):
    ticket = channel_service.alerts.issue_ticket(current_user)
    return SSETicketResponse(ticket=ticket)


@router.post(
    "/security-alerts/ticket",
    response_model=SSETicketResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_security_alert_ticket(
    current_user: User = Depends(require_roles(*_SECURITY_ALLOWED_ROLES)),
):
    ticket = channel_service.security_alerts.issue_ticket(current_user)
    return SSETicketResponse(ticket=ticket)


@router.post(
    "/presence/ticket",
    response_model=PresenceTicketResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_presence_ticket(
    village_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(require_roles(*_ALLOWED_ROLES)),
):
    ticket = presence_service.issue_presence_ticket(current_user, village_id)
    return PresenceTicketResponse(ticket=ticket)


def _resolve_identity(
    alerts: _ChannelTicket | None,
    security: _ChannelTicket | None,
    presence: presence_service.PresenceTicketData | None,
) -> _ChannelTicket:
    if alerts is not None:
        return alerts
    if security is not None:
        return security
    return presence.user_id, presence.village_id, presence.password_changed_at


async def _event_stream(
    identity: _ChannelTicket,
    alerts: _ChannelTicket | None,
    security: _ChannelTicket | None,
    presence: presence_service.PresenceTicketData | None,
) -> AsyncIterator[dict]:
    handle: StreamHandle | None = None
    alerts_registered = False
    security_registered = False
    presence_conn_id: uuid.UUID | None = None

    try:
        if alerts is not None:
            channel_service.alerts.register_connection(alerts[0])
            alerts_registered = True
        if security is not None:
            channel_service.security_alerts.register_connection(security[0])
            security_registered = True
        if presence is not None:
            presence_conn_id = presence_service.register_connection(presence)

        user_id, village_id, password_changed_at = identity
        handle = stream_registry.open(
            user_id,
            village_id,
            password_changed_at,
            settings.channel_max_connections_per_user,
        )

        if alerts is not None:
            channel_service.alerts.subscribe(alerts[1], handle.queue)
        if security is not None:
            channel_service.security_alerts.subscribe(security[1], handle.queue)
        if presence is not None:
            presence_service.register_watcher(presence, handle.queue)

        yield _PADDING_EVENT

        while True:
            try:
                event = await asyncio.wait_for(handle.queue.get(), timeout=_PING_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                yield _PING_EVENT
                continue
            if event is CLOSE_SENTINEL:
                break
            yield {"event": event["event"], "data": json.dumps(event["data"], default=str)}

    except ConnectionLimitExceeded as exc:
        yield {"event": "error", "data": json.dumps({"detail": RealtimeErrors.too_many_connections(exc.max_connections)})}

    finally:
        if handle is not None:
            if alerts is not None:
                channel_service.alerts.unsubscribe(alerts[1], handle.queue)
            if security is not None:
                channel_service.security_alerts.unsubscribe(security[1], handle.queue)
            if presence is not None:
                presence_service.unregister_watcher(presence, handle.queue)
            stream_registry.close(handle)
        if presence_conn_id is not None:
            presence_service.unregister_connection(presence_conn_id)
        if security_registered:
            channel_service.security_alerts.unregister_connection(security[0])
        if alerts_registered:
            channel_service.alerts.unregister_connection(alerts[0])


@router.get("/stream")
async def multiplex_stream(
    alerts_ticket: str | None = Query(None),
    security_ticket: str | None = Query(None),
    presence_ticket: str | None = Query(None),
):
    alerts = channel_service.alerts.resolve_ticket(alerts_ticket) if alerts_ticket else None
    security = channel_service.security_alerts.resolve_ticket(security_ticket) if security_ticket else None
    presence = presence_service.resolve_presence_ticket(presence_ticket) if presence_ticket else None

    if alerts is None and security is None and presence is None:
        raise HTTPException(status_code=400, detail="At least one ticket must be provided")

    identity = _resolve_identity(alerts, security, presence)
    return EventSourceResponse(
        _event_stream(identity, alerts, security, presence),
        headers=_SSE_HEADERS,
    )


@router.get("/test")
async def sse_test(request: Request):
    async def event_generator():
        yield _PADDING_EVENT
        for i in range(5):
            yield {"event": "test", "data": f"message {i}"}
            await asyncio.sleep(1)
    return EventSourceResponse(event_generator(), headers=_SSE_HEADERS)

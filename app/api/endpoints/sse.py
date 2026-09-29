from __future__ import annotations
import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse
from app.api.deps import require_roles
from app.core.error_messages import RealtimeErrors
from app.core.session_manager import session_manager
from app.core.sse_channel import CLOSE_SENTINEL, StreamIdentity
from app.core.stream_registry import StreamHandle, stream_registry
from app.models.user import User, UserRole
from app.schemas.presence import PresenceTicketResponse
from app.schemas.sse import SSETicketResponse
from app.services import channel_service, presence_service

router = APIRouter(prefix="/sse", tags=["sse"])

_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.USER, UserRole.SUPERADMIN)
_SECURITY_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.SUPERADMIN)
_PING_INTERVAL_SECONDS = 15
_PADDING_EVENT = {"event": "padding", "data": " " * 4096}
_PING_EVENT = {"event": "ping", "data": ""}
_SSE_HEADERS = {
    "X-Accel-Buffering": "no",
    "Cache-Control": "no-cache, no-store, must-revalidate",
}


@router.post("/ticket", response_model=SSETicketResponse, status_code=status.HTTP_201_CREATED)
async def create_sse_ticket(
    request: Request,
    current_user: User = Depends(require_roles(*_ALLOWED_ROLES)),
):
    ticket = channel_service.alerts.issue_ticket(current_user, request.state.session_id)
    return SSETicketResponse(ticket=ticket)


@router.post(
    "/security-alerts/ticket",
    response_model=SSETicketResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_security_alert_ticket(
    request: Request,
    current_user: User = Depends(require_roles(*_SECURITY_ALLOWED_ROLES)),
):
    ticket = channel_service.security_alerts.issue_ticket(current_user, request.state.session_id)
    return SSETicketResponse(ticket=ticket)


@router.post(
    "/presence/ticket",
    response_model=PresenceTicketResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_presence_ticket(
    request: Request,
    village_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(require_roles(*_ALLOWED_ROLES)),
):
    ticket = presence_service.issue_presence_ticket(current_user, request.state.session_id, village_id)
    return PresenceTicketResponse(ticket=ticket)


def _resolve_identity(
    alerts: StreamIdentity | None,
    security: StreamIdentity | None,
    presence: presence_service.PresenceTicketData | None,
) -> StreamIdentity:
    identities = [identity for identity in (alerts, security) if identity is not None]
    if presence is not None:
        identities.append(presence.to_stream_identity())

    if not identities:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RealtimeErrors.TICKET_REQUIRED)

    identity = identities[0]
    if not all(identity.belongs_to_same_session(other) for other in identities[1:]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RealtimeErrors.TICKET_SESSION_MISMATCH)

    if not session_manager.is_valid_session(identity.user_id, identity.session_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=RealtimeErrors.SESSION_REVOKED)

    return identity


async def _event_stream(
    identity: StreamIdentity,
    alerts: StreamIdentity | None,
    security: StreamIdentity | None,
    presence: presence_service.PresenceTicketData | None,
) -> AsyncIterator[dict]:
    handle: StreamHandle | None = None
    presence_conn_id: uuid.UUID | None = None

    try:
        if presence is not None:
            presence_conn_id = presence_service.register_connection(presence)

        handle = stream_registry.open(identity)

        if alerts is not None:
            channel_service.alerts.subscribe(alerts.village_id, handle.queue)
        if security is not None:
            channel_service.security_alerts.subscribe(security.village_id, handle.queue)
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

    finally:
        if handle is not None:
            if alerts is not None:
                channel_service.alerts.unsubscribe(alerts.village_id, handle.queue)
            if security is not None:
                channel_service.security_alerts.unsubscribe(security.village_id, handle.queue)
            if presence is not None:
                presence_service.unregister_watcher(presence, handle.queue)
            stream_registry.close(handle)
        if presence_conn_id is not None:
            presence_service.unregister_connection(presence_conn_id)


@router.get("/stream")
async def multiplex_stream(
    alerts_ticket: str | None = Query(None),
    security_ticket: str | None = Query(None),
    presence_ticket: str | None = Query(None),
):
    alerts = channel_service.alerts.resolve_ticket(alerts_ticket) if alerts_ticket else None
    security = channel_service.security_alerts.resolve_ticket(security_ticket) if security_ticket else None
    presence = presence_service.resolve_presence_ticket(presence_ticket) if presence_ticket else None

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

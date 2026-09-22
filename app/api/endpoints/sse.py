from __future__ import annotations
import asyncio
import json
import uuid
from time import monotonic
from collections import defaultdict, deque
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse
from app.api.deps import require_roles
from app.core.config import get_settings
from app.core.connection_limit import ConnectionLimitExceeded
from app.core.sse_channel import CLOSE_SENTINEL
from app.models.user import User, UserRole
from app.schemas.presence import PresenceTicketResponse
from app.schemas.sse import SSETicketResponse
from app.services import channel_service, presence_service, session_validation_service
from app.core.error_messages import RealtimeErrors

router = APIRouter(prefix="/sse", tags=["sse"])

settings = get_settings()

_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.USER, UserRole.SUPERADMIN)
_SECURITY_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.SUPERADMIN)
_PING_INTERVAL_SECONDS = 15
_active_streams: dict[uuid.UUID, deque[asyncio.Queue]] = defaultdict(deque)


def _connection_limit_exceeded_response(exc: ConnectionLimitExceeded) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=RealtimeErrors.too_many_connections(exc.max_connections),
    )


def _revalidation_due(last_revalidated_at: float) -> bool:
    return monotonic() - last_revalidated_at >= settings.sse_revalidation_interval_seconds


async def _base_event_generator(request: Request, user_id: uuid.UUID, village_id: uuid.UUID | None, ticket_password_changed_at: datetime, queue: asyncio.Queue | None):
    yield {"event": "padding", "data": " " * 4096}
    last_revalidated_at = monotonic()
    while True:
        if _revalidation_due(last_revalidated_at):
            if not await session_validation_service.is_session_still_valid(user_id, village_id, ticket_password_changed_at):
                break
            last_revalidated_at = monotonic()

        if queue is None:
            await asyncio.sleep(_PING_INTERVAL_SECONDS)
            continue

        try:
            event = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL_SECONDS)
            if event is CLOSE_SENTINEL:
                break
            yield {"event": event["event"], "data": json.dumps(event["data"], default=str)}
        except asyncio.TimeoutError:
            yield {"event": "ping", "data": ""}


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





@router.get("/stream")
async def multiplex_stream(
    request: Request,
    alerts_ticket: str | None = Query(None),
    security_ticket: str | None = Query(None),
    presence_ticket: str | None = Query(None),
):
    user_id, village_id, password_changed_at = None, None, None
    
    alerts_active, alerts_q, alerts_uid, alerts_vid = False, None, None, None
    security_active, security_q, security_uid, security_vid = False, None, None, None
    presence_active, presence_q, presence_ticket_data, presence_conn_id = False, None, None, None
    initial_snapshot = None

    try:
        if alerts_ticket:
            alerts_uid, alerts_vid, p_at = channel_service.alerts.resolve_ticket(alerts_ticket)
            user_id, village_id, password_changed_at = alerts_uid, alerts_vid, p_at
            channel_service.alerts.register_connection(alerts_uid)
            alerts_active = True
            alerts_q = channel_service.alerts.subscribe(alerts_vid)

        if security_ticket:
            security_uid, security_vid, p_at = channel_service.security_alerts.resolve_ticket(security_ticket)
            if not user_id: user_id, village_id, password_changed_at = security_uid, security_vid, p_at
            channel_service.security_alerts.register_connection(security_uid)
            security_active = True
            security_q = channel_service.security_alerts.subscribe(security_vid)

        if presence_ticket:
            presence_ticket_data = presence_service.resolve_presence_ticket(presence_ticket)
            if not user_id: 
                user_id, village_id, password_changed_at = presence_ticket_data.user_id, presence_ticket_data.village_id, presence_ticket_data.password_changed_at
            presence_conn_id = await presence_service.register_connection(presence_ticket_data)
            presence_active = True
            presence_q = presence_service.register_watcher(presence_ticket_data)
            initial_snapshot = await presence_service.build_snapshot_for_ticket(presence_ticket_data)

    except Exception as exc:
        if alerts_active:
            if alerts_q: channel_service.alerts.unsubscribe(alerts_vid, alerts_q)
            channel_service.alerts.unregister_connection(alerts_uid)
        if security_active:
            if security_q: channel_service.security_alerts.unsubscribe(security_vid, security_q)
            channel_service.security_alerts.unregister_connection(security_uid)
        if presence_active:
            if presence_q: presence_service.unregister_watcher(presence_ticket_data, presence_q)
            if presence_conn_id: await presence_service.unregister_connection(presence_conn_id)
            
        if isinstance(exc, ConnectionLimitExceeded):
            raise _connection_limit_exceeded_response(exc)
        raise exc

    if not (alerts_active or security_active or presence_active):
        raise HTTPException(status_code=400, detail="At least one ticket must be provided")

    master_queue = asyncio.Queue(maxsize=settings.channel_queue_size)
    
    if user_id:
        user_streams = _active_streams[user_id]
        user_streams.append(master_queue)
        limit = settings.channel_max_connections_per_user
        while len(user_streams) > limit:
            oldest_q = user_streams.popleft()
            while True:
                try:
                    oldest_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            oldest_q.put_nowait({"event": "force_close", "data": "Too many connections"})
            oldest_q.put_nowait(CLOSE_SENTINEL)

    async def forwarder(q: asyncio.Queue):
        try:
            while True:
                item = await q.get()
                if item is CLOSE_SENTINEL:
                    await master_queue.put(CLOSE_SENTINEL)
                    break
                await master_queue.put(item)
        except asyncio.CancelledError:
            pass

    tasks = []
    if alerts_q: tasks.append(asyncio.create_task(forwarder(alerts_q)))
    if security_q: tasks.append(asyncio.create_task(forwarder(security_q)))
    if presence_q: tasks.append(asyncio.create_task(forwarder(presence_q)))

    async def event_generator():
        try:
            print(f"[SSE] Starting event generator for user {user_id}")
            yield {"event": "padding", "data": " " * 4096}

            if initial_snapshot is not None:
                print(f"[SSE] Yielding initial snapshot for user {user_id}")
                yield {
                    "event": "presence_update",
                    "data": json.dumps(initial_snapshot, default=str),
                }

            last_revalidated_at = monotonic()
            while True:
                if _revalidation_due(last_revalidated_at):
                    if not await session_validation_service.is_session_still_valid(user_id, village_id, password_changed_at):
                        print(f"[SSE] Session invalid for user {user_id}")
                        break
                    last_revalidated_at = monotonic()

                try:
                    event = await asyncio.wait_for(master_queue.get(), timeout=_PING_INTERVAL_SECONDS)
                    if event is CLOSE_SENTINEL:
                        print(f"[SSE] CLOSE_SENTINEL received for user {user_id}")
                        break
                    print(f"[SSE] Yielding {event['event']} for user {user_id}")
                    yield {"event": event["event"], "data": json.dumps(event["data"], default=str)}
                except asyncio.TimeoutError:
                    print(f"[SSE] Yielding ping for user {user_id}")
                    yield {"event": "ping", "data": ""}
        finally:
            if user_id:
                user_streams = _active_streams.get(user_id)
                if user_streams and master_queue in user_streams:
                    user_streams.remove(master_queue)
                    if not user_streams:
                        _active_streams.pop(user_id, None)

            for task in tasks:
                task.cancel()
            if alerts_active:
                channel_service.alerts.unsubscribe(alerts_vid, alerts_q)
                channel_service.alerts.unregister_connection(alerts_uid)
            if security_active:
                channel_service.security_alerts.unsubscribe(security_vid, security_q)
                channel_service.security_alerts.unregister_connection(security_uid)
            if presence_active:
                presence_service.unregister_watcher(presence_ticket_data, presence_q)
                await presence_service.unregister_connection(presence_conn_id)

    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        }
    )

@router.get("/test")
async def sse_test(request: Request):
    async def event_generator():
        yield {"event": "padding", "data": " " * 4096}
        for i in range(5):
            yield {"event": "test", "data": f"message {i}"}
            await asyncio.sleep(1)
    return EventSourceResponse(
        event_generator(),
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        }
    )
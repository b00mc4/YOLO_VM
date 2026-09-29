from __future__ import annotations
from app.core.config import get_settings
from app.core.sse_channel import ChannelService

settings = get_settings()

alerts = ChannelService(ticket_expire_seconds=settings.sse_ticket_expire_seconds)

security_alerts = ChannelService(ticket_expire_seconds=settings.sse_ticket_expire_seconds)
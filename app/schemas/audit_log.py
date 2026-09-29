from __future__ import annotations

import uuid
from datetime import datetime

from enum import Enum
from pydantic import BaseModel, ConfigDict


class AuditLogAction(str, Enum):
    account_unlocked = "account_unlocked"
    api_key_rejected = "api_key_rejected"
    blacklist_create = "blacklist_create"
    blacklist_delete = "blacklist_delete"
    blacklist_detection = "blacklist_detection"
    blacklist_email_alert_skipped = "blacklist_email_alert_skipped"
    blacklist_update = "blacklist_update"
    camera_activated = "camera_activated"
    camera_auto_resync_ai_vision = "camera_auto_resync_ai_vision"
    camera_created = "camera_created"
    camera_deactivated = "camera_deactivated"
    camera_deleted = "camera_deleted"
    camera_offline = "camera_offline"
    camera_online = "camera_online"
    camera_resync_ai_vision = "camera_resync_ai_vision"
    camera_resync_all = "camera_resync_all"
    camera_sync_failed = "camera_sync_failed"
    camera_updated = "camera_updated"
    camera_verification_anomaly = "camera_verification_anomaly"
    camera_verification_failed = "camera_verification_failed"
    camera_verification_timeout = "camera_verification_timeout"
    camera_verified = "camera_verified"
    change_password = "change_password"
    contact_create = "contact_create"
    contact_delete = "contact_delete"
    contact_update = "contact_update"
    email_change_requested = "email_change_requested"
    email_changed = "email_changed"
    login_blocked_locked = "login_blocked_locked"
    login_bruteforce_detected = "login_bruteforce_detected"
    login_failed = "login_failed"
    login_success = "login_success"
    logout = "logout"
    password_reset_requested = "password_reset_requested"
    password_set = "password_set"
    rapid_login_detected = "rapid_login_detected"
    user_activated = "user_activated"
    user_avatar_added = "user_avatar_added"
    user_avatar_removed = "user_avatar_removed"
    user_avatar_replaced = "user_avatar_replaced"
    user_created = "user_created"
    user_deactivated = "user_deactivated"
    user_deleted = "user_deleted"
    user_fullname_updated = "user_fullname_updated"
    user_invite_resent = "user_invite_resent"
    user_password_reset = "user_password_reset"
    user_updated = "user_updated"
    village_activated = "village_activated"
    village_cameras_deactivated = "village_cameras_deactivated"
    village_cameras_reactivated = "village_cameras_reactivated"
    village_created = "village_created"
    village_deactivated = "village_deactivated"
    village_deleted = "village_deleted"
    village_updated = "village_updated"
    whitelist_create = "whitelist_create"
    whitelist_delete = "whitelist_delete"
    whitelist_update = "whitelist_update"


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    village_id: uuid.UUID | None
    village_name: str | None
    user_id: uuid.UUID | None
    username: str | None
    action: AuditLogAction
    detail: str
    ip_address: str
    user_agent: str
    created_at: datetime
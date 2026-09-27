from __future__ import annotations
import uuid
from typing import Any
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.models.camera import CameraDirection, CameraVerificationStatus
from app.core.url_utils import normalize_rtsp_url, redact_rtsp_url

from app.core.regex_patterns import _NO_EMOJI_REGEX

def _validate_coordinate(v: Any, max_len: int, min_val: float, max_val: float) -> float:
    if v is None:
        return v
    v_str = str(v).strip()
    if len(v_str) > max_len:
        raise ValueError(f"ความยาวห้ามเกิน {max_len} ตัวอักษร")
    if v_str.count("-") > 1:
        raise ValueError("มีเครื่องหมาย - ได้แค่ตัวเดียว")
    if "-" in v_str and not v_str.startswith("-"):
        raise ValueError("เครื่องหมาย - ต้องอยู่หน้าสุดเท่านั้น")
    if v_str.count(".") > 1:
        raise ValueError("มีเครื่องหมาย . ได้แค่ตัวเดียว")
    if v_str.startswith("-."):
        raise ValueError("ห้ามมี . ติดกับ - ต้องมีตัวเลขคั่น")
    if v_str.startswith("."):
        raise ValueError("ห้ามนำหน้าด้วย .")
        
    if v_str.endswith("."):
        v_str += "0000000"
        
    try:
        val = float(v_str)
    except ValueError:
        raise ValueError("รูปแบบพิกัดไม่ถูกต้อง")
        
    if not (min_val <= val <= max_val):
        raise ValueError(f"ค่าต้องอยู่ระหว่าง {min_val} ถึง {max_val}")
        
    return val


class CameraCreate(BaseModel):
    village_id: uuid.UUID
    name: str = Field(max_length=255)
    lat: float = Field(ge=-90, le=90)
    long: float = Field(ge=-180, le=180)
    stream_ai: str = Field(max_length=1000)
    direction: CameraDirection
    delay: int = Field(default=1, ge=1, le=300)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NO_EMOJI_REGEX.match(v):
            raise ValueError("ชื่อรับเฉพาะอักษรไทย อังกฤษ ตัวเลข และอักขระพิเศษ (ห้ามใช้อีโมจิ)")
        return v

    @field_validator("lat", mode="before")
    @classmethod
    def validate_lat(cls, v: Any) -> float:
        return _validate_coordinate(v, max_len=20, min_val=-90.0, max_val=90.0)

    @field_validator("long", mode="before")
    @classmethod
    def validate_long(cls, v: Any) -> float:
        return _validate_coordinate(v, max_len=20, min_val=-180.0, max_val=180.0)

    @field_validator("stream_ai")
    @classmethod
    def validate_stream_ai(cls, v: str) -> str:
        return normalize_rtsp_url(v)


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    lat: float | None = Field(default=None, ge=-90, le=90)
    long: float | None = Field(default=None, ge=-180, le=180)
    direction: CameraDirection | None = None
    is_active: bool | None = None
    delay: int | None = Field(default=None, ge=1, le=300)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is not None and not _NO_EMOJI_REGEX.match(v):
            raise ValueError("ชื่อรับเฉพาะอักษรไทย อังกฤษ ตัวเลข และอักขระพิเศษ (ห้ามใช้อีโมจิ)")
        return v

    @field_validator("lat", mode="before")
    @classmethod
    def validate_lat(cls, v: Any) -> float | None:
        if v is None:
            return v
        return _validate_coordinate(v, max_len=20, min_val=-90.0, max_val=90.0)

    @field_validator("long", mode="before")
    @classmethod
    def validate_long(cls, v: Any) -> float | None:
        if v is None:
            return v
        return _validate_coordinate(v, max_len=20, min_val=-180.0, max_val=180.0)


class CameraRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    village_id: uuid.UUID
    name: str
    lat: float
    long: float
    stream_ai: str
    direction: CameraDirection | None
    webhook_url: str | None = None
    verification_status: CameraVerificationStatus
    ai_vision_synced_at: datetime | None
    created_at: datetime
    is_active: bool
    is_online: bool | None = None
    delay: int

    @field_validator("stream_ai")
    @classmethod
    def redact_stream_ai(cls, v: str) -> str:
        return redact_rtsp_url(v)


class CameraBasicRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    lat: float
    long: float
    direction: CameraDirection | None
    is_active: bool
    is_online: bool | None = None


class CameraStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_active: bool
    verification_status: CameraVerificationStatus
    stream_online: bool | None
    is_starting: bool = False
    status: bool
    detail: str | None = None


class CameraResyncFailedEntry(BaseModel):
    id: uuid.UUID
    name: str


class CameraResyncAllRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    total: int
    succeeded: int
    failed: int
    failed_cameras: list[CameraResyncFailedEntry]


class CameraVerificationCheckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    verification_status: CameraVerificationStatus
    is_active: bool
    ai_vision_synced_at: datetime | None
    ai_vision_reachable: bool
    polling_restarted: bool
    anomaly_detected: bool
    note: str | None


class OnvifProbeRequest(BaseModel):
    host: str = Field(max_length=255)
    port: int = Field(ge=1, le=65535)
    username: str = Field(max_length=255)
    password: str = Field(max_length=255)


class OnvifProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    profile_token: str
    name: str
    encoding: str | None
    width: int | None
    height: int | None
    rtsp_uri: str


class OnvifProbeResponse(BaseModel):
    device_manufacturer: str | None
    device_model: str | None
    profiles: list[OnvifProfileRead]


class CameraStreamTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    camera_id: uuid.UUID
    stream_url: str
    expires_at: datetime
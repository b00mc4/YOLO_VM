from __future__ import annotations
import uuid
from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.core.timezone import BANGKOK_TZ
from app.models.camera import CameraDirection

_CAPTURE_TIME_FORMAT = "%Y/%m/%d %H:%M:%S"


from app.core.plate_format import NormalizedString

class DetectionCreate(BaseModel):
    event_id: uuid.UUID
    camera_id: uuid.UUID
    license_plate: NormalizedString = Field(max_length=255)
    province: NormalizedString = Field(max_length=255)
    color: str = Field(max_length=255)
    capture_time: datetime

    @field_validator("capture_time", mode="before")
    @classmethod
    def parse_capture_time(cls, v):
        if isinstance(v, str):
            try:
                parsed = datetime.strptime(v, _CAPTURE_TIME_FORMAT)
            except ValueError:
                raise ValueError(f"capture_time must be in format YYYY/MM/DD HH:MM:SS, got: {v!r}") from None
            return parsed.replace(tzinfo=BANGKOK_TZ)
        return v


class CarRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    camera_id: uuid.UUID | None
    camera_name: str | None
    village_id: uuid.UUID | None = None
    village_name: str | None = None
    license_plate: str
    province: str
    color: str
    image_crop: str
    image_full: str
    time_detect: datetime
    is_blacklist: bool
    is_whitelist: bool
    direction: CameraDirection | None
    created_at: datetime


class CameraSummary(BaseModel):
    id: uuid.UUID | None
    name: str
    village_id: uuid.UUID | None = None
    village_name: str | None = None
    is_camera_deleted: bool


class CarDetailRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_id: uuid.UUID
    license_plate: str
    province: str
    color: str
    image_crop: str
    image_full: str
    time_detect: datetime
    is_blacklist: bool
    is_whitelist: bool
    direction: CameraDirection | None
    created_at: datetime
    camera: CameraSummary

class RepeatedPlateEntry(BaseModel):
    license_plate: str
    province: str
    count: int


class DetectionDashboardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    date: date
    total_detections_today: int
    unique_plates_today: int
    blacklist_detections_today: int
    whitelist_detections_today: int
    entry_detections_today: int
    exit_detections_today: int
    internal_detections_today: int
    top_repeated_plates: list[RepeatedPlateEntry]
    latest_detections: list[CarRead]


class DetectionEventCamera(BaseModel):
    id: uuid.UUID
    name: str
    lat: float
    long: float


class DetectionEventPayload(BaseModel):
    detection_id: uuid.UUID
    license_plate: str
    province: str
    color: str
    time_detect: datetime
    is_blacklist: bool
    is_whitelist: bool
    direction: CameraDirection | None
    camera: DetectionEventCamera
    image_crop: str
    image_full: str

class DetectionEventCameraGlobal(DetectionEventCamera):
    village_id: uuid.UUID
 
 
class DetectionEventPayloadGlobal(DetectionEventPayload):
    camera: DetectionEventCameraGlobal



class DetectionCreateAck(BaseModel):
    event_id: uuid.UUID

class RouteTrackingDetectionEntry(BaseModel):
    detection_id: uuid.UUID
    camera_id: uuid.UUID | None
    camera_name: str | None = None
    village_id: uuid.UUID | None
    village_name: str | None = None
    lat: float | None = None
    long: float | None = None
    is_camera_deleted: bool
    direction: CameraDirection | None
    time_detect: datetime
    color: str
    is_blacklist: bool
    is_whitelist: bool
    image_crop: str
    image_full: str


class RouteTrackingCarGroup(BaseModel):
    license_plate: str
    province: str
    detection_count: int
    detections: list[RouteTrackingDetectionEntry]


class RouteTrackingDayEntry(BaseModel):
    date: date
    cars: list[RouteTrackingCarGroup]


class RouteTrackingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    items: list[RouteTrackingDayEntry]
    total_dates: int
    total_detections: int
    page: int
    page_size: int
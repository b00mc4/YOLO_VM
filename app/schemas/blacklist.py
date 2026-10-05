from __future__ import annotations
import re
import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.core.plate_format import PlateString, ProvinceString

class BlacklistCreate(BaseModel):
    village_id: uuid.UUID | None = None
    license_plate: PlateString = Field(max_length=255)
    province: ProvinceString = Field(max_length=255)
    reason: str = Field(max_length=255)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("เหตุผลต้องไม่เป็นค่าว่าง")
        if not re.fullmatch(r"^[\x20-\x7E\u0E00-\u0E7F\n\r]+$", v_stripped):
            raise ValueError("เหตุผลต้องเป็นภาษาไทย ภาษาอังกฤษ ตัวเลข และอักขระพิเศษเท่านั้น (ห้ามใส่อิโมจิ)")
        return v_stripped


class BlacklistUpdate(BaseModel):
    license_plate: PlateString | None = Field(default=None, max_length=255)
    province: ProvinceString | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=255)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str | None) -> str | None:
        if v is not None:
            v_stripped = v.strip()
            if not v_stripped:
                raise ValueError("เหตุผลต้องไม่เป็นค่าว่าง")
            if not re.fullmatch(r"^[\x20-\x7E\u0E00-\u0E7F\n\r]+$", v_stripped):
                raise ValueError("เหตุผลต้องเป็นภาษาไทย ภาษาอังกฤษ ตัวเลข และอักขระพิเศษเท่านั้น (ห้ามใส่อิโมจิ)")
            return v_stripped
        return v

class BlacklistRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    village_id: uuid.UUID
    license_plate: str
    province: str
    reason: str
    added_by: uuid.UUID | None
    created_at: datetime
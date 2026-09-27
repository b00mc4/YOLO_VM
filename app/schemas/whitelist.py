from __future__ import annotations
import re
import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator



from app.core.plate_format import PlateString, ProvinceString

class WhitelistCreate(BaseModel):
    village_id: uuid.UUID | None = None
    name: str = Field(max_length=255)
    house_no: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    license_plate: PlateString = Field(max_length=255)
    province: ProvinceString = Field(max_length=255)
    color: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"^[A-Za-z\u0E00-\u0E7F\s\.]+$", v):
            raise ValueError("ชื่อ-นามสกุลต้องประกอบด้วยภาษาไทย ภาษาอังกฤษ จุด และช่องว่างเท่านั้น (ห้ามใส่อิโมจิหรืออักขระพิเศษอื่น)")
        return v

    @field_validator("house_no", "phone", "color", "note")
    @classmethod
    def normalize_optional(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class WhitelistUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    house_no: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    license_plate: PlateString | None = Field(default=None, max_length=255)
    province: ProvinceString | None = Field(default=None, max_length=255)
    color: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not re.fullmatch(r"^[A-Za-z\u0E00-\u0E7F\s\.]+$", v):
                raise ValueError("ชื่อ-นามสกุลต้องประกอบด้วยภาษาไทย ภาษาอังกฤษ จุด และช่องว่างเท่านั้น (ห้ามใส่อิโมจิหรืออักขระพิเศษอื่น)")
            return v
        return v

    @field_validator("house_no", "phone", "color", "note")
    @classmethod
    def normalize_optional(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v


class WhitelistRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    village_id: uuid.UUID
    name: str
    house_no: str | None
    phone: str | None
    license_plate: str
    province: str
    color: str | None
    note: str | None
    added_by: uuid.UUID | None
    created_at: datetime
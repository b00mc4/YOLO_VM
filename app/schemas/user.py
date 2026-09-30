from __future__ import annotations
import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from app.core.security import validate_password_policy
from app.core.error_messages import ValidationErrors
from app.models.user import UserRole
from app.schemas.contact import ContactRead
import re

def _validate_fullname(v: str) -> str:
    v = v.strip()
    if len(v) < 4:
        raise ValueError("ชื่อ-นามสกุลต้องมีความยาวอย่างน้อย 4 ตัวอักษร")
    if len(v) > 100:
        raise ValueError("ชื่อ-นามสกุลต้องมีความยาวไม่เกิน 100 ตัวอักษร")
    if not re.fullmatch(r"^[A-Za-z\u0E00-\u0E7F\s]+$", v):
        raise ValueError("ชื่อ-นามสกุลต้องประกอบด้วยภาษาไทย ภาษาอังกฤษ และช่องว่างเท่านั้น (ห้ามใช้อิโมจิหรืออักขระพิเศษ)")
    return v
class UserCreate(BaseModel):
    username: str = Field(max_length=36)
    fullname: str = Field(max_length=100)
    email: EmailStr = Field(max_length=255)
    role: UserRole
    village_id: uuid.UUID | None = None

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        return v.strip().lower()

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if len(v) < 4:
            raise ValueError("ชื่อผู้ใช้งานต้องมีความยาวอย่างน้อย 4 ตัวอักษร")
        if len(v) > 36:
            raise ValueError("ชื่อผู้ใช้งานต้องมีความยาวไม่เกิน 36 ตัวอักษร")
        return v

    @field_validator("fullname")
    @classmethod
    def validate_fullname(cls, v: str) -> str:
        return _validate_fullname(v)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def check_village_matches_role(self) -> UserCreate:
        if self.role == UserRole.SUPERADMIN and self.village_id is not None:
            raise ValueError(ValidationErrors.SUPERADMIN_NO_VILLAGE)
        if self.role != UserRole.SUPERADMIN and self.village_id is None:
            raise ValueError(ValidationErrors.VILLAGE_REQUIRED_FOR_ROLE)
        return self


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(max_length=36)
    confirm_new_password: str = Field(max_length=36)


    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        return validate_password_policy(v)

    @model_validator(mode="after")
    def check_passwords_match(self) -> AdminResetPasswordRequest:
        if self.new_password != self.confirm_new_password:
            raise ValueError(ValidationErrors.PASSWORD_MISMATCH)
        return self


class AdminResetPasswordResponse(BaseModel):
    detail: str
    username: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    fullname: str
    email: EmailStr
    role: UserRole
    village_id: uuid.UUID | None
    is_active: bool
    is_verify: bool
    created_at: datetime


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    village_id: uuid.UUID | None
    village_name: str | None
    is_active: bool
    is_verify: bool
    created_at: datetime
    avatar_url: str | None      


class UserDetail(BaseModel):
    id: uuid.UUID
    username: str
    fullname: str
    email: EmailStr
    role: UserRole
    village_id: uuid.UUID | None
    village_name: str | None
    avatar_url: str | None
    is_active: bool
    is_verify: bool
    created_at: datetime
    contact_count: int


class UserMeDetail(BaseModel):
    id: uuid.UUID
    username: str
    fullname: str
    email: EmailStr
    role: UserRole
    village_id: uuid.UUID | None
    avatar_url: str | None
    is_active: bool
    is_verify: bool
    created_at: datetime
    contacts: list[ContactRead]

class UserRegister(BaseModel):
    id: uuid.UUID
    username: str
    role: UserRole
    village_id: uuid.UUID | None
    created_at: datetime

class LockedAccountEntry(BaseModel):
    user_id: uuid.UUID
    username: str
    fullname: str
    role: UserRole
    village_id: uuid.UUID | None
    village_name: str | None
    unlocked_at: datetime

class UserFullnameUpdate(BaseModel):
    fullname: str = Field(min_length=4, max_length=100)

    @field_validator("fullname")
    @classmethod
    def validate_fullname_update(cls, v: str) -> str:
        return _validate_fullname(v)


class UserProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    fullname: str
    email: EmailStr
    role: UserRole
    village_id: uuid.UUID | None
    avatar_url: str | None


class EmailChangeRequest(BaseModel):
    new_email: EmailStr = Field(max_length=255)

    @field_validator("new_email")
    @classmethod
    def normalize_new_email(cls, v: str) -> str:
        return v.strip().lower()
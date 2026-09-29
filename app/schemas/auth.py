from __future__ import annotations
import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator
from app.schemas.user import UserRead
from app.schemas.common import PasswordConfirmMixin


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

class LoginResponse(TokenResponse):
    user: UserRead

class SetPasswordRequest(PasswordConfirmMixin):
    token: str = Field(max_length=512)

class SetPasswordResponse(BaseModel):
    detail: str
    username: str

class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(max_length=255)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()

class ChangePasswordRequest(PasswordConfirmMixin):
    current_password: str = Field(max_length=128)

class EmailChangeConfirm(BaseModel):
    token: str = Field(max_length=512)


class EmailChangeConfirmResponse(BaseModel):
    detail: str
    username: str
    email: EmailStr

class VerifyTokenRequest(BaseModel):
    token: str = Field(max_length=512)

class SessionInfo(BaseModel):
    id: uuid.UUID
    created_at: datetime
    expire_at: datetime
    is_current: bool = False

class ActiveSessionsResponse(BaseModel):
    active_sessions_count: int
    max_sessions: int
    sessions: list[SessionInfo]
from __future__ import annotations
import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from app.models.contact import ContactType
from app.models.user import UserRole
from app.core.contact_format import normalize_and_validate_contact_value
from app.core.error_messages import ContactErrors

from app.core.regex_patterns import _THAI_ENG_PATTERN

def _normalize_content_type(value):
    if isinstance(value, str):
        return value.strip().lower()
    return value


class ContactCreate(BaseModel):
    user_id: uuid.UUID | None = None
    content_type: ContactType
    custom_label: str | None = Field(default=None, max_length=255)
    value: str = Field(max_length=255)

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, v):
        return _normalize_content_type(v)

    @field_validator("value")
    @classmethod
    def strip_value(cls, v: str) -> str:
        return v.strip()

    @field_validator("custom_label")
    @classmethod
    def normalize_custom_label(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v

    @model_validator(mode="after")
    def check_custom_label_matches_content_type(self) -> ContactCreate:
        if self.content_type == ContactType.OTHER:
            if not self.custom_label:
                raise ValueError(ContactErrors.CUSTOM_LABEL_REQUIRED)
            if not _THAI_ENG_PATTERN.fullmatch(self.custom_label) or not _THAI_ENG_PATTERN.fullmatch(self.value):
                raise ValueError(ContactErrors.INVALID_OTHER_FORMAT)
        elif self.custom_label is not None:
            raise ValueError(ContactErrors.CUSTOM_LABEL_NOT_ALLOWED)
        return self

    @model_validator(mode="after")
    def normalize_value_format(self) -> ContactCreate:
        self.value = normalize_and_validate_contact_value(self.content_type, self.value)
        return self


class ContactUpdate(BaseModel):
    content_type: ContactType | None = None
    custom_label: str | None = Field(default=None, max_length=255)
    value: str | None = Field(default=None, max_length=255)

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, v):
        return _normalize_content_type(v)

    @field_validator("value")
    @classmethod
    def strip_value(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v

    @field_validator("custom_label")
    @classmethod
    def normalize_custom_label(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else v

    @model_validator(mode="after")
    def check_custom_label_matches_content_type(self) -> ContactUpdate:
        if self.content_type == ContactType.OTHER:
            if not self.custom_label:
                raise ValueError(ContactErrors.CUSTOM_LABEL_REQUIRED)
            if not _THAI_ENG_PATTERN.fullmatch(self.custom_label):
                raise ValueError(ContactErrors.INVALID_OTHER_FORMAT)
            if self.value is not None and not _THAI_ENG_PATTERN.fullmatch(self.value):
                raise ValueError(ContactErrors.INVALID_OTHER_FORMAT)
        elif self.content_type is not None:
            if self.custom_label is not None:
                raise ValueError(ContactErrors.CUSTOM_LABEL_NOT_ALLOWED)
        else:
            if self.custom_label is not None and not _THAI_ENG_PATTERN.fullmatch(self.custom_label):
                raise ValueError(ContactErrors.INVALID_OTHER_FORMAT)
        return self


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    content_type: ContactType
    custom_label: str | None
    value: str
    created_at: datetime


class UserContactsDetail(BaseModel):
    user_id: uuid.UUID
    username: str
    fullname: str
    village_id: uuid.UUID | None
    village_name: str | None
    contacts: list[ContactRead]


class ContactDirectoryEntry(BaseModel):
    user_id: uuid.UUID
    username: str
    fullname: str
    role: UserRole
    village_id: uuid.UUID | None
    village_name: str | None
    contact_count: int
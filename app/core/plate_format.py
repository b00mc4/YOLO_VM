from typing import Annotated
from pydantic import AfterValidator, BeforeValidator

from app.core.regex_patterns import _THAI_PLATE_PATTERN

def _normalize_string(v: object) -> object:
    if not isinstance(v, str):
        return v
    return v.strip().upper()

def _validate_thai_plate(v: str) -> str:
    if not _THAI_PLATE_PATTERN.match(v):
        raise ValueError("ป้ายทะเบียนต้องเป็นอักขระภาษาไทย อังกฤษ หรือตัวเลขเท่านั้น")
    return v

PlateString = Annotated[str, BeforeValidator(_normalize_string), AfterValidator(_validate_thai_plate)]
ProvinceString = Annotated[str, BeforeValidator(_normalize_string)]
NormalizedString = Annotated[str, BeforeValidator(_normalize_string)]

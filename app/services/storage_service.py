from __future__ import annotations
import io
import logging
import uuid
from pathlib import Path
from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError
from app.core.config import get_settings
from app.core.error_messages import StorageErrors

settings = get_settings()
logger = logging.getLogger(__name__)

_ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
}

_ALLOWED_PILLOW_FORMATS = {
    "JPEG": "jpg",
    "PNG": "png",
}

_EXTENSION_TO_CONTENT_TYPE = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}

_MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024
_READ_CHUNK_SIZE_BYTES = 1024 * 1024


def validate_image_content_type(upload: UploadFile) -> None:
    if upload.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=StorageErrors.unsupported_image_content_type(upload.content_type),
        )

async def _read_with_size_limit(upload: UploadFile, max_size_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total_size = 0

    while chunk := await upload.read(_READ_CHUNK_SIZE_BYTES):
        total_size += len(chunk)
        if total_size > max_size_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=StorageErrors.image_too_large(_MAX_IMAGE_SIZE_BYTES // (1024 * 1024)),
            )
        chunks.append(chunk)

    return b"".join(chunks)

def _detect_image_extension(content: bytes) -> str:
    try:
        with Image.open(io.BytesIO(content)) as img:
            detected_format = img.format
            img.verify()
    except (UnidentifiedImageError, DecompressionBombError, OSError, SyntaxError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=StorageErrors.INVALID_IMAGE,
        ) from None
 
    extension = _ALLOWED_PILLOW_FORMATS.get(detected_format)
    if extension is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=StorageErrors.unsupported_format(detected_format),
        )
 
    return extension

async def read_and_validate_image(
    upload: UploadFile, max_size_bytes: int = _MAX_IMAGE_SIZE_BYTES
) -> tuple[bytes, str]:
    validate_image_content_type(upload)
    content = await _read_with_size_limit(upload, max_size_bytes)
    extension = await run_in_threadpool(_detect_image_extension, content)
    return content, extension


def build_detection_image_path(
    village_id: uuid.UUID,
    camera_id: uuid.UUID,
    image_id: uuid.UUID,
    suffix: str,
    extension: str,
) -> str:
    relative_path = Path(str(village_id)) / str(camera_id) / f"{image_id}_{suffix}.{extension}"
    return relative_path.as_posix()


def build_avatar_path(user_id: uuid.UUID, image_id: uuid.UUID, extension: str) -> str:
    relative_path = Path("avatars") / str(user_id) / f"{image_id}.{extension}"
    return relative_path.as_posix()


def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _safe_resolve(relative_path: str) -> Path:
    """Resolve *relative_path* under the storage root, rejecting traversal."""
    base = Path(settings.storage_path).resolve()
    resolved = (base / relative_path).resolve()
    if not resolved.is_relative_to(base):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=StorageErrors.INVALID_PATH,
        )
    return resolved


async def write_image(relative_path: str, content: bytes) -> None:
    absolute_path = _safe_resolve(relative_path)
    await run_in_threadpool(_write_file, absolute_path, content)


def _delete_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete orphaned image: %s", path)


async def delete_image(relative_path: str) -> None:
    absolute_path = _safe_resolve(relative_path)
    await run_in_threadpool(_delete_file, absolute_path)


def resolve_storage_path(relative_path: str) -> Path:
    return _safe_resolve(relative_path)


def guess_media_type(path: Path) -> str:
    extension = path.suffix.lstrip(".").lower()
    return _EXTENSION_TO_CONTENT_TYPE.get(extension, "application/octet-stream")
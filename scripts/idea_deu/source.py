"""Validate an IntelliJ source distribution against its product binding."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from scripts.idea_deu.config import ProductConfig


_HASH_CHUNK_SIZE = 1024 * 1024
_PRODUCT_INFO_PATH = "product-info.json"


class SourceValidationError(ValueError):
    """Raised when a source archive does not match its product binding."""


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceInfo:
    archive: Path
    sha256: str
    version: str
    build_number: str
    product_code: str


def validate_source(config: ProductConfig) -> SourceInfo:
    """Validate the configured archive and return its bound product identity."""
    archive = Path(config.archive)
    actual_sha256 = _archive_sha256(archive)
    if actual_sha256 != config.sha256:
        raise SourceValidationError(
            f"source SHA-256 mismatch: expected {config.sha256}, actual {actual_sha256}"
        )

    product_info = _read_product_info(archive)
    identity = {
        "version": config.version,
        "buildNumber": config.build_number,
        "productCode": config.product_code,
    }
    missing = [key for key in identity if key not in product_info]
    if missing:
        raise SourceValidationError(
            f"{_PRODUCT_INFO_PATH} is missing required fields: {', '.join(missing)}"
        )

    for field, expected in identity.items():
        actual = product_info[field]
        if not isinstance(actual, str):
            raise SourceValidationError(
                f"{_PRODUCT_INFO_PATH} field {field} must be a string"
            )
        if actual != expected:
            raise SourceValidationError(
                f"{field} mismatch: expected {expected}, actual {actual}"
            )

    return SourceInfo(
        archive=archive,
        sha256=actual_sha256,
        version=product_info["version"],
        build_number=product_info["buildNumber"],
        product_code=product_info["productCode"],
    )


def _archive_sha256(archive: Path) -> str:
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as source_file:
            while chunk := source_file.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as error:
        raise SourceValidationError(
            f"cannot read source archive {archive}: {error.strerror or 'I/O error'}"
        ) from None
    return digest.hexdigest()


def _read_product_info(archive: Path) -> dict[str, Any]:
    try:
        with ZipFile(archive) as source_zip:
            try:
                document = source_zip.read(_PRODUCT_INFO_PATH)
            except KeyError:
                raise SourceValidationError(
                    f"source archive is missing root {_PRODUCT_INFO_PATH}"
                ) from None
            except RuntimeError:
                raise SourceValidationError(
                    f"{_PRODUCT_INFO_PATH} is password-protected and cannot be read"
                ) from None
    except BadZipFile:
        raise SourceValidationError(
            f"source archive {archive} is not a valid ZIP archive"
        ) from None
    except OSError as error:
        raise SourceValidationError(
            f"cannot read source archive {archive}: {error.strerror or 'I/O error'}"
        ) from None

    try:
        value: Any = json.loads(document, object_pairs_hook=_unique_object)
    except _DuplicateKeyError as error:
        raise SourceValidationError(
            f"duplicate key {error} in {_PRODUCT_INFO_PATH}"
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SourceValidationError(
            f"{_PRODUCT_INFO_PATH} must contain valid JSON"
        ) from None

    if not isinstance(value, dict):
        raise SourceValidationError(f"{_PRODUCT_INFO_PATH} must be a JSON object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result

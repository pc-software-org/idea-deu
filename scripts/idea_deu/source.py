"""Validate an IntelliJ source distribution against its product binding."""

from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from zipfile import BadZipFile, ZipFile

from scripts.idea_deu.config import ProductConfig


_HASH_CHUNK_SIZE = 1024 * 1024
_MAX_PRODUCT_INFO_SIZE = 1024 * 1024
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


def validate_source(config: ProductConfig, archive_file: BinaryIO | None = None) -> SourceInfo:
    """Validate the configured archive and return its bound product identity."""
    archive = Path(config.archive)
    try:
        if archive_file is not None:
            source_file = archive_file; source_file.seek(0)
            actual_sha256 = _archive_sha256(source_file)
            if actual_sha256 != config.sha256:
                raise SourceValidationError(
                    "source SHA-256 mismatch: "
                    f"expected {config.sha256}, actual {actual_sha256}"
                )
            source_file.seek(0)
            product_info = _read_product_info(source_file)
        else:
            with archive.open("rb") as source_file:
                actual_sha256 = _archive_sha256(source_file)
                if actual_sha256 != config.sha256:
                    raise SourceValidationError("source SHA-256 mismatch: "
                        f"expected {config.sha256}, actual {actual_sha256}")
                source_file.seek(0); product_info = _read_product_info(source_file)
    except OSError as error:
        raise SourceValidationError(
            f"cannot read source archive {archive}: {error.strerror or 'I/O error'}"
        ) from None
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


def _archive_sha256(source_file: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := source_file.read(_HASH_CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def _read_product_info(source_file: BinaryIO) -> dict[str, Any]:
    try:
        with ZipFile(source_file) as source_zip:
            try:
                info = source_zip.getinfo(_PRODUCT_INFO_PATH)
            except KeyError:
                raise SourceValidationError(
                    f"source archive is missing root {_PRODUCT_INFO_PATH}"
                ) from None

            if info.file_size > _MAX_PRODUCT_INFO_SIZE:
                raise SourceValidationError(
                    f"{_PRODUCT_INFO_PATH} size {info.file_size} exceeds "
                    f"limit {_MAX_PRODUCT_INFO_SIZE}"
                )

            try:
                with source_zip.open(info) as product_info_file:
                    document = product_info_file.read(_MAX_PRODUCT_INFO_SIZE + 1)
            except NotImplementedError:
                raise SourceValidationError(
                    f"{_PRODUCT_INFO_PATH} uses unsupported compression"
                ) from None
            except RuntimeError:
                if info.flag_bits & 1:
                    raise SourceValidationError(
                        f"{_PRODUCT_INFO_PATH} is password-protected and cannot be read"
                    ) from None
                raise SourceValidationError(
                    f"{_PRODUCT_INFO_PATH} could not be read from the ZIP archive"
                ) from None
            except (BadZipFile, EOFError, zlib.error):
                raise SourceValidationError(
                    f"{_PRODUCT_INFO_PATH} is a damaged ZIP entry"
                ) from None

            if len(document) > _MAX_PRODUCT_INFO_SIZE:
                raise SourceValidationError(
                    f"{_PRODUCT_INFO_PATH} content exceeds limit "
                    f"{_MAX_PRODUCT_INFO_SIZE}"
                )
    except BadZipFile:
        raise SourceValidationError(
            "source archive is not a valid ZIP archive"
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

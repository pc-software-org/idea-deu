"""Strict loading of the immutable IntelliJ product binding."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


_EXACT_BUILD = "262.8665.337"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
# since/until-build may be a widened compatibility range (e.g. "261", "261.*")
# while build_number stays pinned to the exact scanned distribution.
_BUILD_RANGE_PATTERN = re.compile(r"\d+(\.\d+)*(\.\*)?")


class ConfigError(ValueError):
    """Raised when a product configuration is structurally invalid."""


@dataclass(frozen=True, slots=True)
class ProductConfig:
    archive: str
    version: str
    build_number: str
    product_code: str
    sha256: str
    since_build: str
    until_build: str
    plugin_id: str
    plugin_version: str


def load_product_config(path: Path) -> ProductConfig:
    """Load and validate the exact product configuration from *path*."""
    with path.open(encoding="utf-8") as config_file:
        data: Any = json.load(config_file, object_pairs_hook=_unique_object)

    if not isinstance(data, dict):
        raise ValueError("product configuration must be a JSON object")

    expected_keys = {field.name for field in fields(ProductConfig)}
    actual_keys = set(data)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise ValueError(f"invalid product configuration keys: missing={missing}, extra={extra}")

    if any(not isinstance(data[key], str) for key in expected_keys):
        raise ValueError("all product configuration values must be strings")

    if _SHA256_PATTERN.fullmatch(data["sha256"]) is None:
        raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")

    if data["build_number"] != _EXACT_BUILD:
        raise ValueError(f"build_number must be exactly {_EXACT_BUILD}")
    for key in ("since_build", "until_build"):
        if _BUILD_RANGE_PATTERN.fullmatch(data[key]) is None:
            raise ValueError(f"{key} must be a valid build range (digits, dots, optional .*)")

    return ProductConfig(**data)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate product configuration key: {key}")
        result[key] = value
    return result

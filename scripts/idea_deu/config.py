"""Strict loading of the immutable IntelliJ product binding."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


_EXACT_BUILD = "253.29346.240"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


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

    for key in ("build_number", "since_build", "until_build"):
        if data[key] != _EXACT_BUILD:
            raise ValueError(f"{key} must be exactly {_EXACT_BUILD}")

    return ProductConfig(**data)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate product configuration key: {key}")
        result[key] = value
    return result

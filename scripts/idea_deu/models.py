"""Immutable, serializable scanner domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ExclusionReason(StrEnum):
    CORRUPT_ARCHIVE = "corrupt_archive"
    DIRECTORY = "directory"
    DUPLICATE_MEMBER = "duplicate_member"
    LOCALIZED = "localized"
    NESTED_ARCHIVE = "nested_archive"
    NESTED_JAR_TOO_LARGE = "nested_jar_too_large"
    NOT_JAR = "not_jar"
    RESOURCE_TOO_LARGE = "resource_too_large"
    UNSAFE_PATH = "unsafe_path"
    UNSUPPORTED_COMPRESSION = "unsupported_compression"
    UNSUPPORTED_RESOURCE = "unsupported_resource"


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    resource_id: str
    container: str
    resource_path: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "container": self.container,
            "resource_path": self.resource_path,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ExclusionRecord:
    container: str
    resource_path: str
    reason: ExclusionReason
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "resource_path": self.resource_path,
            "reason": self.reason.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CollisionRecord:
    resource_path: str
    resources: tuple[ResourceRecord, ...]
    unresolved: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_path": self.resource_path,
            "resources": [item.to_dict() for item in self.resources],
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True, slots=True)
class Inventory:
    resources: tuple[ResourceRecord, ...]
    exclusions: tuple[ExclusionRecord, ...]
    collisions: tuple[CollisionRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": [item.to_dict() for item in self.resources],
            "exclusions": [item.to_dict() for item in self.exclusions],
            "collisions": [item.to_dict() for item in self.collisions],
        }

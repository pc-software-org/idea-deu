"""Immutable, serializable scanner domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .validation import Finding


class ExclusionReason(StrEnum):
    CONTAINER_BUDGET_EXCEEDED = "container_budget_exceeded"
    CORRUPT_ARCHIVE = "corrupt_archive"
    DIRECTORY = "directory"
    DUPLICATE_MEMBER = "duplicate_member"
    LOCALIZED = "localized"
    MEMBER_BUDGET_EXCEEDED = "member_budget_exceeded"
    NESTED_ARCHIVE = "nested_archive"
    NESTED_JAR_TOO_LARGE = "nested_jar_too_large"
    NOT_JAR = "not_jar"
    RESOURCE_TOO_LARGE = "resource_too_large"
    TOTAL_NESTED_JAR_BYTES_EXCEEDED = "total_nested_jar_bytes_exceeded"
    TOTAL_RESOURCE_BYTES_EXCEEDED = "total_resource_bytes_exceeded"
    THIRD_PARTY_CONTAINER = "third_party_container"
    UNSAFE_PATH = "unsafe_path"
    UNSUPPORTED_COMPRESSION = "unsupported_compression"
    UNSUPPORTED_RESOURCE = "unsupported_resource"


class ProcessingStatus(StrEnum):
    OPEN = "open"
    TRANSLATED = "translated"
    TECHNICALLY_REVIEWED = "technically_reviewed"


class ResourceType(StrEnum):
    FILE_TEMPLATE = "file_template"
    INSPECTION_DESCRIPTION = "inspection_description"
    INTENTION_DESCRIPTION = "intention_description"
    POSTFIX_TEMPLATE = "postfix_template"
    PROPERTIES = "properties"
    TIP = "tip"


@dataclass(frozen=True, slots=True)
class TranslationContext:
    bundle: str
    key: str
    container: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "bundle": self.bundle,
            "key": self.key,
            "container": self.container,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    id: str
    source: str
    source_sha256: str
    target: str
    context: TranslationContext
    status: ProcessingStatus
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_sha256": self.source_sha256,
            "target": self.target,
            "context": self.context.to_dict(),
            "status": self.status.value,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    resource_id: str
    container: str
    resource_path: str
    resource_type: ResourceType
    size: int
    source_sha256: str
    processing_status: ProcessingStatus = ProcessingStatus.OPEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "container": self.container,
            "resource_path": self.resource_path,
            "resource_type": self.resource_type.value,
            "size": self.size,
            "source_sha256": self.source_sha256,
            "processing_status": self.processing_status.value,
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
    content_identical: bool
    unresolved: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_path": self.resource_path,
            "resources": [item.to_dict() for item in self.resources],
            "content_identical": self.content_identical,
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

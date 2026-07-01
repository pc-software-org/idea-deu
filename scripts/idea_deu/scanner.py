"""Bounded inventory scanning for resources inside IntelliJ JARs."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import Any

from .models import (
    CollisionRecord,
    ExclusionReason,
    ExclusionRecord,
    Inventory,
    ResourceRecord,
    ResourceType,
)


_LOCALIZED_PROPERTIES = re.compile(r"(?:^|/)[^/]+_(?:[a-z]{2,3})(?:_(?:[A-Z]{2}|[A-Za-z]{4})(?:_[A-Z]{2})?)?\.properties$")
_RESOURCE_PATTERN_TYPES = {
    "*.properties": ResourceType.PROPERTIES,
    "inspectionDescriptions/**/*.html": ResourceType.INSPECTION_DESCRIPTION,
    "intentionDescriptions/**/*.html": ResourceType.INTENTION_DESCRIPTION,
    "fileTemplates/**/*.html": ResourceType.FILE_TEMPLATE,
    "postfixTemplates/**/*.xml": ResourceType.POSTFIX_TEMPLATE,
    "tips/**/*.html": ResourceType.TIP,
}


class ScannerError(ValueError):
    """Raised when scanner configuration or the outer archive is invalid."""


@dataclass(slots=True)
class _ScanBudget:
    containers: int = 0
    members: int = 0
    nested_jar_bytes: int = 0
    resource_bytes: int = 0


@dataclass(frozen=True, slots=True)
class ContainerExclusionRule:
    glob: str
    reason: ExclusionReason


@dataclass(frozen=True, slots=True)
class ScannerConfig:
    """Explicit rules and byte ceilings for bounded nested scanning.

    The shipped 512 MiB JAR ceiling leaves headroom for the roughly 240 MiB
    ``app.jar``. The 100,000 outer-member, 20,000-container, 2,000,000 nested-
    member and 8 GiB cumulative JAR budgets leave substantial headroom for a
    full IDEA installation while bounding sorting, record allocation, central-
    directory and decompression work. Individual translation resources are
    capped at 16 MiB and 4 GiB cumulatively. Only the first 64 MiB of a JAR
    stays in memory before spooling to disk.

    Container exclusions use exact known third-party native/API metadata
    library names (JNA and JSR-305). They deliberately avoid broad vendor or
    directory heuristics that could hide JetBrains UI resources.
    """

    container_exclusions: tuple[ContainerExclusionRule, ...]
    resource_patterns: tuple[str, ...]
    localization_directories: tuple[str, ...]
    max_containers: int
    max_members: int
    max_nested_jar_bytes: int
    max_outer_members: int
    max_resource_bytes: int
    max_total_nested_jar_bytes: int
    max_total_resource_bytes: int
    spool_memory_bytes: int

    def with_limits(
        self,
        *,
        max_nested_jar_bytes: int | None = None,
        max_resource_bytes: int | None = None,
    ) -> ScannerConfig:
        return replace(
            self,
            max_nested_jar_bytes=self.max_nested_jar_bytes if max_nested_jar_bytes is None else max_nested_jar_bytes,
            max_resource_bytes=self.max_resource_bytes if max_resource_bytes is None else max_resource_bytes,
        )


def load_scanner_config(path: Path) -> ScannerConfig:
    try:
        with path.open(encoding="utf-8") as config_file:
            data: Any = json.load(config_file, object_pairs_hook=_unique_object)
    except ScannerError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ScannerError(f"cannot load scanner configuration: {path}") from error
    if not isinstance(data, dict) or set(data) != {
        "container_exclusions",
        "resource_patterns",
        "localization_directories",
        "limits",
    }:
        raise ScannerError("scanner configuration has invalid top-level keys")
    limits = data["limits"]
    expected_limits = {
        "max_containers",
        "max_members",
        "max_nested_jar_bytes",
        "max_outer_members",
        "max_resource_bytes",
        "max_total_nested_jar_bytes",
        "max_total_resource_bytes",
        "spool_memory_bytes",
    }
    if not isinstance(limits, dict) or set(limits) != expected_limits:
        raise ScannerError("scanner configuration has invalid limit keys")
    patterns = data["resource_patterns"]
    directories = data["localization_directories"]
    raw_exclusions = data["container_exclusions"]
    if not isinstance(patterns, list) or not patterns or not all(
        isinstance(value, str) and value.strip() for value in patterns
    ):
        raise ScannerError("resource_patterns must be a non-empty string list")
    _validate_resource_patterns(patterns)
    if not isinstance(directories, list) or not directories or not all(
        isinstance(value, str) and value.strip() for value in directories
    ):
        raise ScannerError("localization_directories must be a non-empty string list")
    if not all(type(limits[key]) is int and limits[key] > 0 for key in limits):
        raise ScannerError("scanner limits must be positive integers")
    if not isinstance(raw_exclusions, list) or not raw_exclusions or not all(
        isinstance(rule, dict) and set(rule) == {"glob", "reason"} for rule in raw_exclusions
    ):
        raise ScannerError("container_exclusions must contain glob/reason objects")
    if not all(
        isinstance(rule["glob"], str)
        and bool(rule["glob"].strip())
        and rule["reason"] == ExclusionReason.THIRD_PARTY_CONTAINER.value
        for rule in raw_exclusions
    ):
        raise ScannerError("container exclusion glob/reason values are invalid")
    if limits["max_containers"] > limits["max_outer_members"]:
        raise ScannerError("max_containers cannot exceed max_outer_members")
    if limits["max_nested_jar_bytes"] > limits["max_total_nested_jar_bytes"]:
        raise ScannerError("max_nested_jar_bytes cannot exceed its total budget")
    if limits["max_resource_bytes"] > limits["max_total_resource_bytes"]:
        raise ScannerError("max_resource_bytes cannot exceed its total budget")
    if limits["spool_memory_bytes"] > limits["max_nested_jar_bytes"]:
        raise ScannerError("spool_memory_bytes cannot exceed max_nested_jar_bytes")
    try:
        container_exclusions = tuple(
            ContainerExclusionRule(rule["glob"], ExclusionReason(rule["reason"])) for rule in raw_exclusions
        )
    except (TypeError, ValueError) as error:
        raise ScannerError("container exclusion rule is invalid") from error
    return ScannerConfig(
        container_exclusions,
        tuple(patterns),
        tuple(value.lower() for value in directories),
        limits["max_containers"],
        limits["max_members"],
        limits["max_nested_jar_bytes"],
        limits["max_outer_members"],
        limits["max_resource_bytes"],
        limits["max_total_nested_jar_bytes"],
        limits["max_total_resource_bytes"],
        limits["spool_memory_bytes"],
    )


def scan_archive(path: Path, config: ScannerConfig) -> Inventory:
    _validate_resource_patterns(config.resource_patterns)
    resources: list[ResourceRecord] = []
    exclusions: list[ExclusionRecord] = []
    budget = _ScanBudget()
    try:
        with zipfile.ZipFile(path) as outer:
            outer_member_count = len(outer.filelist)
            if outer_member_count > config.max_outer_members:
                raise ScannerError(
                    "outer archive member budget exceeded: "
                    f"{outer_member_count} > {config.max_outer_members}"
                )
            for member in sorted(outer.filelist, key=lambda item: item.filename):
                container = member.filename
                if member.is_dir():
                    exclusions.append(ExclusionRecord("<outer>", container, ExclusionReason.DIRECTORY))
                elif not _safe_path(container):
                    exclusions.append(ExclusionRecord("<outer>", container, ExclusionReason.UNSAFE_PATH))
                elif not container.lower().endswith(".jar"):
                    exclusions.append(ExclusionRecord("<outer>", container, ExclusionReason.NOT_JAR))
                elif exclusion_reason := _container_exclusion_reason(container, config.container_exclusions):
                    exclusions.append(ExclusionRecord(container, "", exclusion_reason))
                elif budget.containers >= config.max_containers:
                    exclusions.append(ExclusionRecord(container, "", ExclusionReason.CONTAINER_BUDGET_EXCEEDED))
                elif member.file_size > config.max_nested_jar_bytes:
                    exclusions.append(ExclusionRecord(container, "", ExclusionReason.NESTED_JAR_TOO_LARGE, str(member.file_size)))
                elif budget.nested_jar_bytes + member.file_size > config.max_total_nested_jar_bytes:
                    exclusions.append(
                        ExclusionRecord(container, "", ExclusionReason.TOTAL_NESTED_JAR_BYTES_EXCEEDED, str(member.file_size))
                    )
                else:
                    budget.nested_jar_bytes += member.file_size
                    _scan_nested(outer, member, config, budget, resources, exclusions)
                if container.lower().endswith(".jar") and _safe_path(container) and not member.is_dir():
                    budget.containers += 1
    except (OSError, zipfile.BadZipFile) as error:
        raise ScannerError(f"cannot read outer archive: {path}") from error

    resources.sort(key=lambda item: (item.container, item.resource_path))
    exclusions.sort(key=lambda item: (item.container, item.resource_path, item.reason.value))
    by_path: dict[str, list[ResourceRecord]] = {}
    for resource in resources:
        by_path.setdefault(resource.resource_path, []).append(resource)
    collisions = tuple(
        CollisionRecord(
            resource_path,
            tuple(records),
            content_identical=len({record.source_sha256 for record in records}) == 1,
        )
        for resource_path, records in sorted(by_path.items())
        if len({record.container for record in records}) > 1
    )
    return Inventory(tuple(resources), tuple(exclusions), collisions)


def _scan_nested(
    outer: zipfile.ZipFile,
    jar_info: zipfile.ZipInfo,
    config: ScannerConfig,
    budget: _ScanBudget,
    resources: list[ResourceRecord],
    exclusions: list[ExclusionRecord],
) -> None:
    container = jar_info.filename
    try:
        with SpooledTemporaryFile(max_size=config.spool_memory_bytes) as temporary:
            with outer.open(jar_info) as nested_stream:
                _copy_bounded(nested_stream, temporary, config.max_nested_jar_bytes)
            temporary.seek(0)
            with zipfile.ZipFile(temporary) as nested:
                members = sorted(nested.infolist(), key=lambda item: item.filename)
                if budget.members + len(members) > config.max_members:
                    exclusions.append(
                        ExclusionRecord(container, "", ExclusionReason.MEMBER_BUDGET_EXCEEDED, str(len(members)))
                    )
                    return
                budget.members += len(members)
                seen: set[str] = set()
                for member in members:
                    resource_path = member.filename
                    if resource_path in seen:
                        exclusions.append(ExclusionRecord(container, resource_path, ExclusionReason.DUPLICATE_MEMBER))
                        continue
                    seen.add(resource_path)
                    _classify_member(nested, member, container, config, budget, resources, exclusions)
    except (zipfile.BadZipFile, EOFError) as error:
        exclusions.append(ExclusionRecord(container, "", ExclusionReason.CORRUPT_ARCHIVE, str(error)))
    except (NotImplementedError, RuntimeError) as error:
        exclusions.append(ExclusionRecord(container, "", ExclusionReason.UNSUPPORTED_COMPRESSION, str(error)))


def _classify_member(
    nested: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    container: str,
    config: ScannerConfig,
    budget: _ScanBudget,
    resources: list[ResourceRecord],
    exclusions: list[ExclusionRecord],
) -> None:
    path = member.filename
    if member.is_dir():
        exclusions.append(ExclusionRecord(container, path, ExclusionReason.DIRECTORY))
    elif not _safe_path(path):
        exclusions.append(ExclusionRecord(container, path, ExclusionReason.UNSAFE_PATH))
    elif path.lower().endswith((".jar", ".zip")):
        exclusions.append(ExclusionRecord(container, path, ExclusionReason.NESTED_ARCHIVE))
    elif _is_localized(path, config.localization_directories):
        exclusions.append(ExclusionRecord(container, path, ExclusionReason.LOCALIZED))
    elif not _matches_resource(path, config.resource_patterns):
        exclusions.append(ExclusionRecord(container, path, ExclusionReason.UNSUPPORTED_RESOURCE))
    elif member.file_size > config.max_resource_bytes:
        exclusions.append(ExclusionRecord(container, path, ExclusionReason.RESOURCE_TOO_LARGE, str(member.file_size)))
    elif budget.resource_bytes + member.file_size > config.max_total_resource_bytes:
        exclusions.append(
            ExclusionRecord(container, path, ExclusionReason.TOTAL_RESOURCE_BYTES_EXCEEDED, str(member.file_size))
        )
    else:
        # Reserve candidate bytes before opening the member. Failed or
        # unsupported decompression must still consume the global work budget.
        budget.resource_bytes += member.file_size
        try:
            with nested.open(member) as stream:
                source_sha256 = _hash_bounded(stream, config.max_resource_bytes)
        except (NotImplementedError, RuntimeError) as error:
            exclusions.append(ExclusionRecord(container, path, ExclusionReason.UNSUPPORTED_COMPRESSION, str(error)))
            return
        except (zipfile.BadZipFile, EOFError) as error:
            exclusions.append(ExclusionRecord(container, path, ExclusionReason.CORRUPT_ARCHIVE, str(error)))
            return
        resource_id = hashlib.sha256(f"{container}\0{path}".encode()).hexdigest()
        resources.append(
            ResourceRecord(
                resource_id,
                container,
                path,
                _resource_type(path, config.resource_patterns),
                member.file_size,
                source_sha256,
            )
        )


def _matches_resource(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_pattern_matches(path, pattern) for pattern in patterns)


def _pattern_matches(path: str, pattern: str) -> bool:
    if fnmatchcase(path, pattern):
        return True
    # Treat ``**/`` as zero-or-more directories, unlike fnmatch's purely
    # textual wildcard handling.
    return "/**/" in pattern and fnmatchcase(path, pattern.replace("/**/", "/"))


def _validate_resource_patterns(patterns: Any) -> None:
    for pattern in patterns:
        if pattern not in _RESOURCE_PATTERN_TYPES:
            raise ScannerError(f"unsupported resource pattern: {pattern}")


def _container_exclusion_reason(
    container: str,
    rules: tuple[ContainerExclusionRule, ...],
) -> ExclusionReason | None:
    for rule in rules:
        if fnmatchcase(container, rule.glob):
            return rule.reason
    return None


def _resource_type(path: str, patterns: tuple[str, ...]) -> ResourceType:
    for pattern in patterns:
        if _pattern_matches(path, pattern):
            return _RESOURCE_PATTERN_TYPES[pattern]
    raise ScannerError(f"matched resource has no type: {path}")


def _is_localized(path: str, directory_names: tuple[str, ...]) -> bool:
    segments = PurePosixPath(path).parts
    return bool(_LOCALIZED_PROPERTIES.search(path)) or any(segment.lower() in directory_names for segment in segments)


def _safe_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return not pure.is_absolute() and ".." not in pure.parts and "\\" not in path


def _copy_bounded(source: Any, target: Any, maximum: int) -> None:
    copied = 0
    while chunk := source.read(min(1024 * 1024, maximum + 1 - copied)):
        copied += len(chunk)
        if copied > maximum:
            raise zipfile.BadZipFile("nested JAR exceeds configured size")
        target.write(chunk)


def _hash_bounded(source: Any, maximum: int) -> str:
    read = 0
    digest = hashlib.sha256()
    while chunk := source.read(min(1024 * 1024, maximum + 1 - read)):
        read += len(chunk)
        if read > maximum:
            raise zipfile.BadZipFile("resource exceeds configured size")
        digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScannerError(f"duplicate scanner configuration key: {key}")
        result[key] = value
    return result

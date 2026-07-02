"""Verified generation of localized resources from an immutable inventory."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import zipfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from .models import CollisionRecord, Inventory, ProcessingStatus, ResourceRecord, ResourceType, TranslationUnit
from .path_safety import OutputPathError, atomic_materialize_tree
from .properties import PropertiesError, parse_properties, render_properties
from .validation import Severity


class GenerationError(ValueError): pass


@dataclass(frozen=True, slots=True)
class GenerationResult:
    root: Path
    inventory: Inventory
    units: tuple[TranslationUnit, ...]
    sources: tuple[tuple[str, str, bytes], ...]
    files: tuple[tuple[str, bytes], ...]
    dedupe_identical: bool


class MappingResourceProvider:
    def __init__(self, resources: Mapping[tuple[str, str], bytes]): self._resources = dict(resources)
    def read(self, record: ResourceRecord) -> bytes:
        try: return self._resources[(record.container, record.resource_path)]
        except KeyError as exc: raise GenerationError(f"missing source bytes: {record.container}!/{record.resource_path}") from exc


class DistributionResourceProvider:
    """Read an inventoried member from its nested JAR without extraction.

    Owns an open ``ZipFile`` handle and any transiently opened nested JAR;
    close via ``close()`` or the context-manager protocol to release both.
    """
    def __init__(self, archive: object):
        self.archive = archive if hasattr(archive, "read") else Path(archive)  # type: ignore[arg-type]
        if hasattr(self.archive, "seek"): self.archive.seek(0)  # type: ignore[union-attr]
        self._outer: zipfile.ZipFile | None = zipfile.ZipFile(self.archive)
        self._container_name: str | None = None
        self._nested: zipfile.ZipFile | None = None

    def __enter__(self) -> "DistributionResourceProvider":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    def close(self) -> None:
        nested, self._nested = self._nested, None
        outer, self._outer = self._outer, None
        self._container_name = None
        try:
            if nested is not None: nested.close()
        finally:
            if outer is not None: outer.close()

    def read(self, record: ResourceRecord) -> bytes:
        if self._outer is None:
            raise GenerationError("distribution provider is closed")
        try:
            if self._container_name != record.container:
                if self._nested is not None:
                    self._nested.close(); self._nested = None
                info = _unique_zip_member(self._outer, record.container)
                if _zip_symlink(info): raise GenerationError(f"symbolic-link container: {record.container}")
                with self._outer.open(info) as stream:
                    self._nested = zipfile.ZipFile(io.BytesIO(stream.read()))
                self._container_name = record.container
            assert self._nested is not None
            resource = _unique_zip_member(self._nested, record.resource_path)
            if _zip_symlink(resource): raise GenerationError(f"symbolic-link resource: {record.resource_path}")
            return self._nested.read(resource)
        except GenerationError: raise
        except (OSError, zipfile.BadZipFile, KeyError) as exc: raise GenerationError(f"cannot read source resource: {exc}") from exc


class BlobResourceProvider:
    """Read immutable content-addressed scan blobs without following links."""
    def __init__(self, root: Path): self.root = Path(root)
    def read(self, record: ResourceRecord) -> bytes:
        path = self.root / record.source_sha256
        if path.is_symlink(): raise GenerationError(f"symbolic source blob: {record.source_sha256}")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream: data = stream.read(record.size + 1)
        except OSError as exc: raise GenerationError(f"cannot read source blob: {record.source_sha256}: {exc}") from exc
        if len(data) != record.size or hashlib.sha256(data).hexdigest() != record.source_sha256:
            raise GenerationError(f"invalid source blob: {record.source_sha256}")
        return data


def generate_resources(inventory: Inventory, units: Sequence[TranslationUnit], provider: object,
                       output: Path, *, dedupe_identical: bool = False,
                       trusted_root: Path | None = None) -> GenerationResult:
    _validate_generation_structure(inventory, units, dedupe_identical)
    sources: dict[tuple[str, str], bytes] = {}
    for record in inventory.resources:
        key = (record.container, record.resource_path)
        if key not in sources:
            sources[key] = provider.read(record)  # type: ignore[attr-defined]
    rendered = recompute_generation(inventory, units, sources, dedupe_identical=dedupe_identical)
    _write_tree(output, rendered, trusted_root=trusted_root)
    source_evidence = tuple((container, path, data) for (container, path), data in sorted(sources.items()))
    return GenerationResult(Path(output).absolute(), inventory, tuple(units), source_evidence,
                            tuple(sorted(rendered.items())), dedupe_identical)


def recompute_result(result: GenerationResult) -> dict[str, bytes]:
    if not isinstance(result, GenerationResult):
        raise GenerationError("GenerationResult required")
    if not result.inventory.resources or not result.units or not result.sources or not result.files:
        raise GenerationError("generation evidence must not be empty")
    sources: dict[tuple[str, str], bytes] = {}
    for container, path, data in result.sources:
        key = (container, path)
        if key in sources:
            raise GenerationError(f"duplicate source evidence: {container}!/{path}")
        sources[key] = data
    return recompute_generation(result.inventory, result.units, sources,
                                dedupe_identical=result.dedupe_identical)


def recompute_generation(inventory: Inventory, units: Sequence[TranslationUnit],
                         sources: Mapping[tuple[str, str], bytes], *,
                         dedupe_identical: bool = False) -> dict[str, bytes]:
    _validate_generation_structure(inventory, units, dedupe_identical)
    by_resource: dict[tuple[str,str], list[TranslationUnit]] = defaultdict(list)
    for unit in units: by_resource[(unit.context.container, unit.context.path)].append(unit)
    inventory_keys = {(record.container, record.resource_path) for record in inventory.resources}
    expected_source_keys = inventory_keys
    if set(sources) != expected_source_keys:
        raise GenerationError("source evidence does not exactly match inventory")

    rendered: dict[str, bytes] = {}
    for record in inventory.resources:
        source = sources[(record.container, record.resource_path)]
        if hashlib.sha256(source).hexdigest() != record.source_sha256:
            raise GenerationError(f"source SHA-256 mismatch: {record.resource_id}")
        if len(source) != record.size:
            raise GenerationError(f"source size mismatch: {record.resource_id}")
        selected = by_resource[(record.container,record.resource_path)]
        if record.resource_type is ResourceType.PROPERTIES:
            try:
                doc = parse_properties(source)
                counts: dict[str, int] = defaultdict(int)
                for unit in selected: counts[unit.context.key] += 1
                missing_keys = sorted(set(doc.values) - set(counts))
                extra_keys = sorted(set(counts) - set(doc.values))
                duplicate_keys = sorted(key for key, count in counts.items() if count != 1)
                problems = [("missing", key) for key in missing_keys] + [("extra", key) for key in extra_keys] + [("duplicate", key) for key in duplicate_keys]
                if problems:
                    raise GenerationError("incomplete properties units: " + ", ".join(
                        f"{record.resource_path}:{key} ({kind})" for kind, key in problems))
                translations = {u.context.key: u.target for u in selected}
                for unit in selected:
                    if unit.context.key not in doc.values or doc.values[unit.context.key] != unit.source:
                        raise GenerationError(f"source value mismatch: {unit.id}")
                data = render_properties(doc, translations)
            except PropertiesError as exc: raise GenerationError(str(exc)) from exc
            target = _localized_properties(record.resource_path)
        else:
            if len(selected) != 1 or selected[0].context.key:
                raise GenerationError(f"whole-file resource requires exactly one empty-key unit: {record.resource_id}")
            unit = selected[0]
            decoded = _decode_source(source)
            if decoded != unit.source: raise GenerationError(f"source value mismatch: {unit.id}")
            if record.resource_path.lower().endswith(".xml"):
                try: ElementTree.fromstring(unit.target)
                except ElementTree.ParseError as exc: raise GenerationError(f"invalid target XML: {unit.id}: {exc}") from exc
            normalized = _normalize_whole_file_target(decoded, unit.target)
            data = ((b"\xef\xbb\xbf" if source.startswith(b"\xef\xbb\xbf") else b"") + normalized.encode("utf-8"))
            target = record.resource_path
        previous = rendered.get(target)
        if previous is not None:
            allowed_dedupe = dedupe_identical and any(
                collision.resource_path == record.resource_path and collision.content_identical
                for collision in inventory.collisions
            )
            if not allowed_dedupe or previous != data:
                raise GenerationError(f"duplicate output collision: {target}")
        rendered[target] = data
    return rendered


def _validate_generation_structure(inventory: Inventory, units: Sequence[TranslationUnit],
                                   dedupe_identical: bool) -> None:
    _validate_inventory_paths(inventory.resources)
    _validate_collision_evidence(inventory)
    unresolved = [c for c in inventory.collisions if c.unresolved and not (dedupe_identical and c.content_identical)]
    if unresolved:
        details = "; ".join(f"{c.resource_path}: " + ", ".join(r.container for r in c.resources) for c in unresolved)
        raise GenerationError(f"unresolved collision: {details}")
    bad = [u.id for u in units if u.status not in {ProcessingStatus.TECHNICALLY_REVIEWED,
        ProcessingStatus.LINGUISTICALLY_REVIEWED} or any(f.severity is Severity.BLOCKING for f in u.findings)]
    if bad: raise GenerationError("units not review-complete or blocking: " + ", ".join(sorted(bad)))
    bad_hashes = [u.id for u in units if hashlib.sha256(u.source.encode("utf-8")).hexdigest() != u.source_sha256]
    if bad_hashes: raise GenerationError("translation source SHA-256 mismatch: " + ", ".join(sorted(bad_hashes)))
    by_resource: dict[tuple[str,str], list[TranslationUnit]] = defaultdict(list)
    for unit in units: by_resource[(unit.context.container, unit.context.path)].append(unit)
    inventory_keys = {(record.container, record.resource_path) for record in inventory.resources}
    unknown_resources = sorted(set(by_resource) - inventory_keys)
    if unknown_resources:
        raise GenerationError("translation units without inventory resource: " + ", ".join(
            f"{container}!/{path}" for container, path in unknown_resources))
    missing = [r.resource_id for r in inventory.resources if (r.container,r.resource_path) not in by_resource]
    if missing: raise GenerationError("missing translation units: " + ", ".join(missing))


def _validate_collision_evidence(inventory: Inventory) -> None:
    groups: dict[str, list[ResourceRecord]] = defaultdict(list)
    for record in inventory.resources:
        groups[record.resource_path].append(record)
    derived = {path: records for path, records in groups.items() if len(records) > 1}
    claimed: dict[str, CollisionRecord] = {}
    for collision in inventory.collisions:
        if collision.resource_path in claimed:
            raise GenerationError(f"duplicate collision classification: {collision.resource_path}")
        claimed[collision.resource_path] = collision
    missing = set(derived) - set(claimed)
    extra = set(claimed) - set(derived)
    if missing:
        raise GenerationError("missing collision classification: " + ", ".join(sorted(missing)))
    if extra:
        raise GenerationError("extra collision classification: " + ", ".join(sorted(extra)))
    key = lambda record: (record.resource_id, record.container, record.resource_path,
                          record.resource_type.value, record.size, record.source_sha256,
                          record.processing_status.value)
    for path, records in derived.items():
        collision = claimed[path]
        if tuple(sorted(collision.resources, key=key)) != tuple(sorted(records, key=key)):
            raise GenerationError(f"collision classification members mismatch: {path}")
        identical = len({record.source_sha256 for record in records}) == 1
        if collision.content_identical is not identical:
            raise GenerationError(f"collision classification content mismatch: {path}")


def _localized_properties(path: str) -> str:
    return path[:-11] + "_de.properties"


def _decode_source(data: bytes) -> str:
    try: return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc: raise GenerationError(f"invalid UTF-8 source at byte {exc.start}") from exc


def _normalize_whole_file_target(source: str, target: str) -> str:
    endings = re.findall(r"\r\n|\r|\n", source)
    styles = set(endings)
    if len(styles) > 1:
        raise GenerationError("mixed newline styles in whole-file source")
    target_lines = re.split(r"\r\n|\r|\n", target)
    target_had_final = bool(re.search(r"(?:\r\n|\r|\n)$", target))
    if target_had_final: target_lines.pop()
    source_final = bool(re.search(r"(?:\r\n|\r|\n)$", source))
    if not endings:
        if len(target_lines) > 1: raise GenerationError("target contains newlines but source has no newline policy")
        return target_lines[0]
    newline = endings[0]
    normalized = newline.join(target_lines)
    return normalized + (newline if source_final else "")


def _validate_inventory_paths(records: Sequence[ResourceRecord]) -> None:
    seen: dict[str,str] = {}
    for record in records:
        path = record.resource_path; pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path or path.startswith("/"):
            raise GenerationError(f"unsafe resource path: {path}")
        if not _supported_record(record):
            raise GenerationError(f"unsupported resource content: {path}")
        container = record.container; container_path = PurePosixPath(container)
        if not container or container_path.is_absolute() or ".." in container_path.parts or "\\" in container:
            raise GenerationError(f"unsafe container path: {container}")
        output = _localized_properties(path) if record.resource_type is ResourceType.PROPERTIES else path
        folded = output.casefold()
        if folded in seen and seen[folded] != output: raise GenerationError(f"case-fold output collision: {seen[folded]}, {output}")
        seen[folded] = output


def _supported_record(record: ResourceRecord) -> bool:
    path = record.resource_path
    if record.resource_type is ResourceType.PROPERTIES:
        return path.endswith(".properties")
    prefixes = {
        ResourceType.INSPECTION_DESCRIPTION: "inspectionDescriptions/",
        ResourceType.INTENTION_DESCRIPTION: "intentionDescriptions/",
        ResourceType.FILE_TEMPLATE: "fileTemplates/",
        ResourceType.POSTFIX_TEMPLATE: "postfixTemplates/",
        ResourceType.TIP: "tips/",
    }
    prefix = prefixes.get(record.resource_type)
    suffix = ".xml" if record.resource_type is ResourceType.POSTFIX_TEMPLATE else ".html"
    return prefix is not None and path.startswith(prefix) and path.endswith(suffix)


def _write_tree(root: Path, resources: Mapping[str,bytes], *, trusted_root: Path | None = None) -> None:
    try:
        atomic_materialize_tree(root, resources, trusted_root=trusted_root)
    except (OSError, OutputPathError) as exc:
        raise GenerationError(str(exc)) from exc


def _unique_zip_member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    matches = [i for i in archive.infolist() if i.filename == name]
    if len(matches) != 1: raise GenerationError(f"missing or duplicate ZIP member: {name}")
    return matches[0]


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xffff)

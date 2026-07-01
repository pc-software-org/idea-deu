"""Verified generation of localized resources from an immutable inventory."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import stat
import zipfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from .models import Inventory, ProcessingStatus, ResourceRecord, ResourceType, TranslationUnit
from .path_safety import unsafe_output_parent
from .properties import PropertiesError, parse_properties, render_properties
from .validation import Severity


class GenerationError(ValueError): pass


_GENERATION_KEY = os.urandom(32)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    root: Path
    files: tuple[tuple[str, str], ...]
    units: tuple[TranslationUnit, ...]
    unresolved_collisions: tuple[str, ...]
    complete: bool
    _seal: str

    @classmethod
    def _verified(cls, root: Path, files: Mapping[str, bytes], units: Sequence[TranslationUnit]) -> "GenerationResult":
        normalized_files = tuple((path, hashlib.sha256(data).hexdigest()) for path, data in sorted(files.items()))
        normalized_units = tuple(units)
        seal = _result_seal(root.resolve(), normalized_files, normalized_units, (), True)
        return cls(root.resolve(), normalized_files, normalized_units, (), True, seal)

    def is_verified(self) -> bool:
        expected = _result_seal(self.root, self.files, self.units, self.unresolved_collisions, self.complete)
        return hmac.compare_digest(self._seal, expected)


def _result_seal(root: Path, files: tuple[tuple[str, str], ...], units: tuple[TranslationUnit, ...],
                 collisions: tuple[str, ...], complete: bool) -> str:
    value = {"root": str(root), "files": files, "units": [unit.to_dict() for unit in units],
             "collisions": collisions, "complete": complete}
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.blake2b(payload, key=_GENERATION_KEY, digest_size=32).hexdigest()


class MappingResourceProvider:
    def __init__(self, resources: Mapping[tuple[str, str], bytes]): self._resources = dict(resources)
    def read(self, record: ResourceRecord) -> bytes:
        try: return self._resources[(record.container, record.resource_path)]
        except KeyError as exc: raise GenerationError(f"missing source bytes: {record.container}!/{record.resource_path}") from exc


class DistributionResourceProvider:
    """Read an inventoried member from its nested JAR without extraction."""
    def __init__(self, archive: Path): self.archive = Path(archive)
    def read(self, record: ResourceRecord) -> bytes:
        try:
            with zipfile.ZipFile(self.archive) as outer:
                info = _unique_zip_member(outer, record.container)
                if _zip_symlink(info): raise GenerationError(f"symbolic-link container: {record.container}")
                with outer.open(info) as stream, zipfile.ZipFile(io.BytesIO(stream.read())) as nested:
                    resource = _unique_zip_member(nested, record.resource_path)
                    if _zip_symlink(resource): raise GenerationError(f"symbolic-link resource: {record.resource_path}")
                    return nested.read(resource)
        except GenerationError: raise
        except (OSError, zipfile.BadZipFile, KeyError) as exc: raise GenerationError(f"cannot read source resource: {exc}") from exc


def generate_resources(inventory: Inventory, units: Sequence[TranslationUnit], provider: object,
                       output: Path, *, dedupe_identical: bool = False) -> GenerationResult:
    _validate_inventory_paths(inventory.resources)
    collision_paths = {collision.resource_path for collision in inventory.collisions}
    grouped_paths: dict[str, list[ResourceRecord]] = defaultdict(list)
    for record in inventory.resources:
        grouped_paths[record.resource_path].append(record)
    unclassified = sorted(path for path, records in grouped_paths.items()
                          if len(records) > 1 and path not in collision_paths)
    if unclassified:
        raise GenerationError("missing collision classification: " + ", ".join(unclassified))
    unresolved = [c for c in inventory.collisions if c.unresolved and not (dedupe_identical and c.content_identical)]
    if unresolved:
        details = "; ".join(f"{c.resource_path}: " + ", ".join(r.container for r in c.resources) for c in unresolved)
        raise GenerationError(f"unresolved collision: {details}")
    bad = [u.id for u in units if u.status not in {ProcessingStatus.TECHNICALLY_REVIEWED,
        ProcessingStatus.LINGUISTICALLY_REVIEWED} or any(f.severity is Severity.BLOCKING for f in u.findings)]
    if bad: raise GenerationError("units not review-complete or blocking: " + ", ".join(sorted(bad)))
    by_resource: dict[tuple[str,str], list[TranslationUnit]] = defaultdict(list)
    for unit in units: by_resource[(unit.context.container, unit.context.path)].append(unit)
    inventory_keys = {(record.container, record.resource_path) for record in inventory.resources}
    unknown_resources = sorted(set(by_resource) - inventory_keys)
    if unknown_resources:
        raise GenerationError("translation units without inventory resource: " + ", ".join(
            f"{container}!/{path}" for container, path in unknown_resources))
    missing = [r.resource_id for r in inventory.resources if (r.container,r.resource_path) not in by_resource]
    if missing: raise GenerationError("missing translation units: " + ", ".join(missing))

    rendered: dict[str, bytes] = {}
    for record in inventory.resources:
        source = provider.read(record)  # type: ignore[attr-defined]
        if hashlib.sha256(source).hexdigest() != record.source_sha256:
            raise GenerationError(f"source SHA-256 mismatch: {record.resource_id}")
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
    _write_tree(output, rendered)
    return GenerationResult._verified(output, rendered, units)


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


def _write_tree(root: Path, resources: Mapping[str,bytes]) -> None:
    root = Path(root)
    _assert_no_symlink_ancestors(root)
    if root.exists() and (root.is_symlink() or not root.is_dir()): raise GenerationError(f"unsafe output root: {root}")
    if root.exists() and any(root.iterdir()): raise GenerationError(f"output root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    for relative,data in sorted(resources.items()):
        target = root.joinpath(*PurePosixPath(relative).parts); target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_symlink(): raise GenerationError(f"symbolic-link output: {relative}")
        target.write_bytes(data)


def _assert_no_symlink_ancestors(path: Path) -> None:
    unsafe = unsafe_output_parent(path)
    if unsafe is not None:
        reason, component = unsafe
        raise GenerationError(f"{reason}: {component}")


def _unique_zip_member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    matches = [i for i in archive.infolist() if i.filename == name]
    if len(matches) != 1: raise GenerationError(f"missing or duplicate ZIP member: {name}")
    return matches[0]


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xffff)

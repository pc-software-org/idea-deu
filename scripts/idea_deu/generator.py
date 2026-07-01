"""Verified generation of localized resources from an immutable inventory."""
from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from .models import Inventory, ProcessingStatus, ResourceRecord, ResourceType, TranslationUnit
from .properties import PropertiesError, parse_properties, render_properties
from .validation import Severity


class GenerationError(ValueError): pass


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
                       output: Path, *, dedupe_identical: bool = False) -> tuple[Path, ...]:
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
                translations = {u.context.key: u.target for u in selected}
                if len(translations) != len(selected): raise GenerationError(f"duplicate translation key: {record.resource_id}")
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
            data = ((b"\xef\xbb\xbf" if source.startswith(b"\xef\xbb\xbf") else b"") + unit.target.encode("utf-8"))
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
    return tuple(output / path for path in sorted(rendered))


def _localized_properties(path: str) -> str:
    return path[:-11] + "_de.properties"


def _decode_source(data: bytes) -> str:
    try: return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc: raise GenerationError(f"invalid UTF-8 source at byte {exc.start}") from exc


def _validate_inventory_paths(records: Sequence[ResourceRecord]) -> None:
    seen: dict[str,str] = {}
    for record in records:
        path = record.resource_path; pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path or path.startswith("/"):
            raise GenerationError(f"unsafe resource path: {path}")
        container = record.container; container_path = PurePosixPath(container)
        if not container or container_path.is_absolute() or ".." in container_path.parts or "\\" in container:
            raise GenerationError(f"unsafe container path: {container}")
        output = _localized_properties(path) if record.resource_type is ResourceType.PROPERTIES else path
        folded = output.casefold()
        if folded in seen and seen[folded] != output: raise GenerationError(f"case-fold output collision: {seen[folded]}, {output}")
        seen[folded] = output


def _write_tree(root: Path, resources: Mapping[str,bytes]) -> None:
    root = Path(root)
    if root.exists() and (root.is_symlink() or not root.is_dir()): raise GenerationError(f"unsafe output root: {root}")
    if root.exists() and any(root.iterdir()): raise GenerationError(f"output root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    for relative,data in sorted(resources.items()):
        target = root.joinpath(*PurePosixPath(relative).parts); target.parent.mkdir(parents=True,exist_ok=True)
        if target.is_symlink(): raise GenerationError(f"symbolic-link output: {relative}")
        target.write_bytes(data)


def _unique_zip_member(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    matches = [i for i in archive.infolist() if i.filename == name]
    if len(matches) != 1: raise GenerationError(f"missing or duplicate ZIP member: {name}")
    return matches[0]


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xffff)

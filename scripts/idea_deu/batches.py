"""Resumable, bounded translation batch export and validated import."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .models import ProcessingStatus, TranslationUnit
from .state import StateError, read_jsonl, write_jsonl_atomic
from .validation import validate_translation

SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 1000


class BatchError(ValueError):
    """Raised for unsafe, stale, or malformed batch operations."""


@dataclass(frozen=True, slots=True)
class Glossary:
    retained_terms: tuple[str, ...]
    preferred_terms: dict[str, tuple[str, ...]]
    primary_style: str
    fallback_style: str

    def validation_terms(self) -> dict[str, tuple[str, ...]]:
        result = {term: () for term in self.retained_terms}
        result.update(self.preferred_terms)
        return result


@dataclass(frozen=True, slots=True)
class ImportResult:
    imported: int
    reviewed: int
    blocking: int


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BatchError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_glossary(path: Path) -> Glossary:
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except BatchError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchError(f"invalid glossary: {exc}") from exc
    expected = {"schema_version", "locale", "style", "retained_terms", "preferred_terms", "forbidden_blanket_replacements"}
    if not isinstance(data, dict) or set(data) != expected:
        raise BatchError("glossary fields mismatch")
    if data["schema_version"] != 1 or data["locale"] != "de":
        raise BatchError("unsupported glossary schema or locale")
    style = data["style"]
    if not isinstance(style, dict) or set(style) != {"primary", "fallback"} or style["primary"] != "neutral" or style["fallback"] != "Sie":
        raise BatchError("invalid glossary style")
    retained = _nonempty_unique_strings(data["retained_terms"], "retained_terms")
    forbidden = _nonempty_unique_strings(data["forbidden_blanket_replacements"], "forbidden_blanket_replacements")
    if not forbidden:
        raise BatchError("forbidden_blanket_replacements must not be empty")
    preferred_raw = data["preferred_terms"]
    if not isinstance(preferred_raw, dict):
        raise BatchError("preferred_terms must be an object")
    preferred: dict[str, tuple[str, ...]] = {}
    for term, variants in preferred_raw.items():
        if not isinstance(term, str) or not term.strip():
            raise BatchError("preferred term must be non-empty")
        preferred[term] = _nonempty_unique_strings(variants, f"preferred_terms.{term}")
    return Glossary(retained, preferred, style["primary"], style["fallback"])


def _nonempty_unique_strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BatchError(f"{name} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise BatchError(f"{name} entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise BatchError(f"{name} contains duplicates")
    return tuple(value)


def export_next_batch(root: Path, units_path: Path, *, limit: int = 100) -> Path | None:
    root = _safe_root(root)
    units_path = _safe_path(root, units_path, must_exist=True)
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH_SIZE:
        raise BatchError(f"limit must be an integer from 1 to {MAX_BATCH_SIZE}")
    checkpoint_path = root / "translations/checkpoint.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    if checkpoint["current_batch"] is not None:
        return _safe_path(root, root / checkpoint["current_batch"], must_exist=True)
    units = _read_jsonl(units_path, TranslationUnit)
    selected = sorted((unit for unit in units if unit.status is ProcessingStatus.OPEN), key=lambda unit: unit.id)[:limit]
    if not selected:
        return None
    sequence = checkpoint["completed_sequence"] + 1
    relative = Path("translations/batches") / f"{sequence}-{selected[0].id[:12]}.jsonl"
    batch_path = _safe_path(root, root / relative)
    records = [_batch_record(unit, sequence, relative.as_posix(), len(selected)) for unit in selected]
    write_jsonl_atomic(batch_path, records)
    next_checkpoint = _checkpoint(checkpoint["completed_sequence"], sequence, relative.as_posix(), len(selected), "translate")
    try:
        write_jsonl_atomic(checkpoint_path, [next_checkpoint])
    except Exception:
        batch_path.unlink(missing_ok=True)
        raise
    return batch_path


def import_batch(root: Path, units_path: Path, batch_path: Path, glossary_path: Path) -> ImportResult:
    root = _safe_root(root)
    units_path = _safe_path(root, units_path, must_exist=True)
    batch_path = _safe_path(root, batch_path, must_exist=True)
    glossary_path = _safe_path(root, glossary_path, must_exist=True)
    checkpoint_path = root / "translations/checkpoint.json"
    checkpoint = _read_checkpoint(checkpoint_path)
    expected_relative = batch_path.relative_to(root.resolve(strict=True)).as_posix()
    if checkpoint["current_batch"] != expected_relative:
        raise BatchError("wrong or out-of-order batch")
    sequence = checkpoint["current_sequence"]
    records = _read_jsonl(batch_path, dict)
    units = _read_jsonl(units_path, TranslationUnit)
    unit_by_id = {unit.id: unit for unit in units}
    if len(records) != checkpoint["counts"]["exported"]:
        raise BatchError("partial batch is not allowed")
    glossary = load_glossary(glossary_path)
    replacements: dict[str, TranslationUnit] = {}
    for record in records:
        expected_keys = {"id", "source", "source_sha256", "target", "context", "status", "findings", "batch"}
        if set(record) != expected_keys:
            raise BatchError("batch record fields mismatch")
        identifier = record["id"]
        if identifier not in unit_by_id or identifier in replacements:
            raise BatchError(f"unknown or duplicate ID: {identifier}")
        unit = unit_by_id[identifier]
        expected = _batch_record(unit, sequence, expected_relative, len(records))
        for key in ("source", "source_sha256", "context", "status", "findings", "batch"):
            if record[key] != expected[key]:
                raise BatchError(f"changed {key} for {identifier}")
        target = record["target"]
        if not isinstance(target, str) or not target.strip():
            raise BatchError(f"missing target for {identifier}")
        validation = validate_translation(unit.source, target, glossary=glossary.validation_terms(), context=unit.context.to_dict())
        status = ProcessingStatus.TRANSLATED if validation.is_blocking else ProcessingStatus.TECHNICALLY_REVIEWED
        replacements[identifier] = replace(unit, target=target, status=status, findings=validation.findings)
    updated = [replacements.get(unit.id, unit) for unit in units]
    blocking = sum(unit.status is ProcessingStatus.TRANSLATED for unit in replacements.values())
    next_checkpoint = _checkpoint(sequence, None, None, len(records), "export-next-batch")
    old_units = units
    try:
        write_jsonl_atomic(units_path, updated)
        write_jsonl_atomic(checkpoint_path, [next_checkpoint])
    except Exception:
        write_jsonl_atomic(units_path, old_units)
        raise
    return ImportResult(len(records), len(records) - blocking, blocking)


def _batch_record(unit: TranslationUnit, sequence: int, relative: str, count: int) -> dict[str, Any]:
    value = unit.to_dict()
    value["batch"] = {"schema_version": SCHEMA_VERSION, "sequence": sequence, "path": relative, "count": count}
    return value


def _checkpoint(completed: int, current_sequence: int | None, current_batch: str | None, count: int, command: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "completed_sequence": completed, "current_sequence": current_sequence, "current_batch": current_batch, "counts": {"exported": count}, "next_command": command}


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _checkpoint(0, None, None, 0, "export-next-batch")
    records = _read_jsonl(path, dict)
    if len(records) != 1:
        raise BatchError("invalid checkpoint")
    value = records[0]
    expected = {"schema_version", "completed_sequence", "current_sequence", "current_batch", "counts", "next_command"}
    if set(value) != expected or value["schema_version"] != SCHEMA_VERSION:
        raise BatchError("invalid checkpoint schema")
    sequence = value["current_sequence"]
    batch = value["current_batch"]
    counts = value["counts"]
    if (
        type(value["completed_sequence"]) is not int
        or value["completed_sequence"] < 0
        or (sequence is not None and (type(sequence) is not int or sequence < 1))
        or (batch is not None and (not isinstance(batch, str) or not batch))
        or (sequence is None) != (batch is None)
        or not isinstance(counts, dict)
        or set(counts) != {"exported"}
        or type(counts["exported"]) is not int
        or counts["exported"] < 0
        or not isinstance(value["next_command"], str)
        or not value["next_command"]
    ):
        raise BatchError("invalid checkpoint field types")
    return value


def _safe_root(root: Path) -> Path:
    try:
        candidate = Path(root).absolute()
        if not candidate.is_dir():
            raise OSError("root is not a directory")
        return candidate
    except OSError as exc:
        raise BatchError(f"invalid root: {exc}") from exc


def _safe_path(root: Path, path: Path, *, must_exist: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = candidate.absolute()
    current = lexical
    resolved_root = root.resolve(strict=True)
    while True:
        if current.resolve(strict=False) == resolved_root:
            break
        if current.is_symlink():
            raise BatchError(f"symbolic link in path: {path}")
        if current == current.parent:
            break
        current = current.parent
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise BatchError(f"path escapes root: {path}") from exc
    return resolved


def _read_jsonl(path: Path, record_type: type[Any]) -> list[Any]:
    try:
        return read_jsonl(path, record_type)
    except StateError as exc:
        raise BatchError(str(exc)) from exc

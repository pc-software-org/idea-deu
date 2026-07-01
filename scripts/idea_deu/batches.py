"""Resumable, bounded translation batch export and validated import."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .models import ProcessingStatus, TranslationUnit
from .state import (
    StateError,
    read_jsonl,
    read_jsonl_at,
    write_jsonl_atomic,
    write_jsonl_atomic_at,
)
from .validation import validate_translation

SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 1000
_LOCK_STATE = threading.local()


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


def validate_all_units(root: Path, units_path: Path, glossary_path: Path) -> tuple[TranslationUnit, ...]:
    """Validate every unit and commit units/checkpoint through the batch transaction."""
    root = _safe_root(root)
    with _exclusive_lock(root) as root_fd:
        with _state_descriptors(root_fd, root) as (state_fd, _batches_fd):
            units_path, _checkpoint_path, identity = _validate_state_layout(root, units_path)
            _recover_transaction(root, identity, state_fd)
            glossary_path = _safe_path(root, glossary_path, must_exist=True)
            glossary = load_glossary(glossary_path).validation_terms()
            checkpoint = _read_checkpoint_at(state_fd)
            if not _regular_exists_at(state_fd, "checkpoint.json"):
                _write_jsonl_at(state_fd, "checkpoint.json", [checkpoint])
            updated: list[TranslationUnit] = []
            for unit in _read_jsonl_at(state_fd, "units.jsonl", TranslationUnit):
                validation = validate_translation(unit.source, unit.target, glossary=glossary,
                                                  context=unit.context.to_dict())
                if not unit.target:
                    status = ProcessingStatus.OPEN
                elif validation.is_blocking:
                    status = ProcessingStatus.TRANSLATED
                else:
                    status = ProcessingStatus.TECHNICALLY_REVIEWED
                updated.append(replace(unit, status=status, findings=validation.findings))
            _commit_transaction(root, updated, checkpoint, identity, state_fd)
            return tuple(updated)


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
    with _exclusive_lock(root) as root_fd:
        with _state_descriptors(root_fd, root) as (state_fd, batches_fd):
            return _export_next_batch_locked(root, units_path, limit, state_fd, batches_fd)


def _export_next_batch_locked(root: Path, units_path: Path, limit: int, state_fd: int, batches_fd: int) -> Path | None:
    units_path, checkpoint_path, identity = _validate_state_layout(root, units_path)
    _layout_hook()
    _assert_state_identity(root, identity)
    _recover_transaction(root, identity, state_fd)
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH_SIZE:
        raise BatchError(f"limit must be an integer from 1 to {MAX_BATCH_SIZE}")
    checkpoint = _read_checkpoint_at(state_fd)
    units = _read_jsonl_at(state_fd, "units.jsonl", TranslationUnit)
    if checkpoint["current_batch"] is not None:
        current = _safe_path(root, root / checkpoint["current_batch"], must_exist=True)
        _validate_batch_manifest_at(batches_fd, current.name, checkpoint, units)
        return current
    selected = sorted((unit for unit in units if unit.status is ProcessingStatus.OPEN), key=lambda unit: unit.id)[:limit]
    if not selected:
        return None
    sequence = checkpoint["completed_sequence"] + 1
    relative = Path("translations/batches") / f"{sequence}-{selected[0].id[:12]}.jsonl"
    batch_path = _safe_path(root, root / relative)
    unit_ids = [unit.id for unit in selected]
    digest = _batch_digest(selected, sequence, relative.as_posix(), unit_ids)
    records = [_batch_record(unit, sequence, relative.as_posix(), unit_ids, digest) for unit in selected]
    _assert_state_identity(root, identity)
    _write_hook()
    _write_jsonl_at(batches_fd, batch_path.name, records)
    next_checkpoint = _checkpoint(checkpoint["completed_sequence"], sequence, relative.as_posix(), unit_ids, digest, "translate-current-batch")
    try:
        _assert_state_identity(root, identity)
        _write_jsonl_at(state_fd, "checkpoint.json", [next_checkpoint])
    except Exception:
        try:
            os.unlink(batch_path.name, dir_fd=batches_fd)
            os.fsync(batches_fd)
        except FileNotFoundError:
            pass
        raise
    return batch_path


def import_batch(root: Path, units_path: Path, batch_path: Path, glossary_path: Path) -> ImportResult:
    root = _safe_root(root)
    with _exclusive_lock(root) as root_fd:
        with _state_descriptors(root_fd, root) as (state_fd, batches_fd):
            return _import_batch_locked(root, units_path, batch_path, glossary_path, state_fd, batches_fd)


def _import_batch_locked(root: Path, units_path: Path, batch_path: Path, glossary_path: Path, state_fd: int, batches_fd: int) -> ImportResult:
    units_path, checkpoint_path, identity = _validate_state_layout(root, units_path)
    _layout_hook()
    _assert_state_identity(root, identity)
    _recover_transaction(root, identity, state_fd)
    batch_path = _safe_path(root, batch_path, must_exist=True)
    glossary_path = _safe_path(root, glossary_path, must_exist=True)
    checkpoint = _read_checkpoint_at(state_fd)
    expected_relative = batch_path.relative_to(root.resolve(strict=True)).as_posix()
    if checkpoint["current_batch"] != expected_relative:
        raise BatchError("wrong or out-of-order batch")
    sequence = checkpoint["current_sequence"]
    records = _read_jsonl_at(batches_fd, batch_path.name, dict)
    units = _read_jsonl_at(state_fd, "units.jsonl", TranslationUnit)
    unit_by_id = {unit.id: unit for unit in units}
    record_ids = [record.get("id") for record in records]
    if record_ids != checkpoint["unit_ids"]:
        raise BatchError("batch unit IDs do not match exported manifest")
    exported_units = [unit_by_id.get(identifier) for identifier in checkpoint["unit_ids"]]
    if any(unit is None for unit in exported_units):
        raise BatchError("exported batch contains unknown unit IDs")
    digest = _batch_digest(
        exported_units,  # type: ignore[arg-type]
        sequence,
        expected_relative,
        checkpoint["unit_ids"],
    )
    if digest != checkpoint["batch_digest"]:
        raise BatchError("batch digest does not match current immutable units")
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
        expected = _batch_record(unit, sequence, expected_relative, checkpoint["unit_ids"], checkpoint["batch_digest"])
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
    next_checkpoint = _checkpoint(sequence, None, None, [], None, "export-next-batch")
    _write_hook()
    _assert_state_identity(root, identity)
    _commit_transaction(root, updated, next_checkpoint, identity, state_fd)
    return ImportResult(len(records), len(records) - blocking, blocking)


def _batch_record(unit: TranslationUnit, sequence: int, relative: str, unit_ids: list[str], digest: str) -> dict[str, Any]:
    value = unit.to_dict()
    value["batch"] = {"schema_version": SCHEMA_VERSION, "sequence": sequence, "path": relative, "count": len(unit_ids), "unit_ids": unit_ids, "digest": digest}
    return value


def _checkpoint(completed: int, current_sequence: int | None, current_batch: str | None, unit_ids: list[str], digest: str | None, command: str) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "completed_sequence": completed, "current_sequence": current_sequence, "current_batch": current_batch, "counts": {"exported": len(unit_ids)}, "unit_ids": unit_ids, "batch_digest": digest, "next_command": command}


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _checkpoint(0, None, None, [], None, "export-next-batch")
    records = _read_jsonl(path, dict)
    return _validate_checkpoint_records(records)


def _read_checkpoint_at(state_fd: int) -> dict[str, Any]:
    try:
        os.stat("checkpoint.json", dir_fd=state_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _checkpoint(0, None, None, [], None, "export-next-batch")
    records = _read_jsonl_at(state_fd, "checkpoint.json", dict)
    return _validate_checkpoint_records(records)


def _validate_checkpoint_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(records) != 1:
        raise BatchError("invalid checkpoint")
    value = records[0]
    expected = {"schema_version", "completed_sequence", "current_sequence", "current_batch", "counts", "unit_ids", "batch_digest", "next_command"}
    if set(value) != expected or value["schema_version"] != SCHEMA_VERSION:
        raise BatchError("invalid checkpoint schema")
    sequence = value["current_sequence"]
    batch = value["current_batch"]
    counts = value["counts"]
    unit_ids = value["unit_ids"]
    digest = value["batch_digest"]
    active = sequence is not None
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
        or not isinstance(unit_ids, list)
        or any(not isinstance(item, str) or not item for item in unit_ids)
        or len(set(unit_ids)) != len(unit_ids)
        or counts["exported"] != len(unit_ids)
        or (active and sequence != value["completed_sequence"] + 1)
        or (active and not unit_ids)
        or (active and (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)))
        or (not active and (unit_ids or digest is not None or counts["exported"] != 0))
        or value["next_command"] != ("translate-current-batch" if active else "export-next-batch")
    ):
        raise BatchError("invalid checkpoint field types")
    if active:
        expected_batch = f"translations/batches/{sequence}-{unit_ids[0][:12]}.jsonl"
        if batch != expected_batch:
            raise BatchError("invalid checkpoint batch path")
    return value


def _batch_digest(units: list[TranslationUnit], sequence: int, relative: str, unit_ids: list[str]) -> str:
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "path": relative,
        "unit_ids": unit_ids,
        "units": [
            {key: value for key, value in unit.to_dict().items() if key != "target"}
            for unit in units
        ],
    }
    payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _transaction_paths(root: Path) -> tuple[Path, Path, Path]:
    directory = root / "translations"
    return (
        directory / ".batch-txn.staging",
        directory / ".batch-txn.active",
        directory / ".batch-txn.committed",
    )


def _commit_transaction(
    root: Path,
    units: list[TranslationUnit],
    checkpoint: dict[str, Any],
    identity: tuple[int, int],
    state_fd: int,
) -> None:
    """Commit through staging -> active -> committed directory states."""
    _recover_transaction(root, identity, state_fd)
    _assert_state_identity(root, identity)
    old_units = _read_jsonl_at(state_fd, "units.jsonl", TranslationUnit)
    old_checkpoint = _read_jsonl_at(state_fd, "checkpoint.json", dict)
    os.mkdir(".batch-txn.staging", mode=0o700, dir_fd=state_fd)
    transaction_fd = os.open(
        ".batch-txn.staging",
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=state_fd,
    )
    committed_durable = False
    try:
        _write_jsonl_at(transaction_fd, "units.jsonl", old_units)
        _transaction_hook("after_one_backup")
        _write_jsonl_at(transaction_fd, "checkpoint.json", old_checkpoint)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "units_present": _regular_exists_at(state_fd, "units.jsonl"),
            "checkpoint_present": _regular_exists_at(state_fd, "checkpoint.json"),
            "units_sha256": _file_sha256_at(transaction_fd, "units.jsonl"),
            "checkpoint_sha256": _file_sha256_at(transaction_fd, "checkpoint.json"),
        }
        _write_jsonl_at(transaction_fd, "manifest.json", [manifest])
        os.fsync(transaction_fd)
        _transaction_hook("before_active_rename")
        os.rename(".batch-txn.staging", ".batch-txn.active", src_dir_fd=state_fd, dst_dir_fd=state_fd)
        os.fsync(state_fd)
        _transaction_hook("after_active_rename")
        _assert_state_identity(root, identity)
        _write_jsonl_at(state_fd, "units.jsonl", units)
        _transaction_hook("between_state_writes")
        _assert_state_identity(root, identity)
        _write_jsonl_at(state_fd, "checkpoint.json", [checkpoint])
        os.rename(".batch-txn.active", ".batch-txn.committed", src_dir_fd=state_fd, dst_dir_fd=state_fd)
        os.fsync(state_fd)
        committed_durable = True
        _transaction_hook("after_committed_rename")
        _remove_transaction_directory_at(state_fd, ".batch-txn.committed", validate=False, hook_cleanup=True)
    except Exception:
        if committed_durable:
            return
        _recover_transaction(root, identity, state_fd)
        raise
    except BaseException:
        raise
    finally:
        os.close(transaction_fd)


def _recover_transaction(root: Path, identity: tuple[int, int] | None = None, state_fd: int | None = None) -> None:
    """Rollback an interrupted two-file commit before any public operation."""
    if state_fd is not None:
        _recover_transaction_at(state_fd)
        return
    staging, active, committed = _transaction_paths(root)
    if identity is not None:
        _assert_state_identity(root, identity)
    for path in (staging, active, committed):
        if path.is_symlink():
            raise BatchError(f"symbolic transaction path: {path}")
        if path.exists() and not path.is_dir():
            raise BatchError(f"transaction path is not a directory: {path}")
    if active.exists() and committed.exists():
        raise BatchError("conflicting transaction states")
    if committed.exists():
        if identity is not None:
            _assert_state_identity(root, identity)
        _remove_transaction_directory(committed, validate=False)
    if active.exists():
        if identity is not None:
            _assert_state_identity(root, identity)
        manifest = _validate_transaction_directory(active)
        units_path = root / "translations/units.jsonl"
        checkpoint_path = root / "translations/checkpoint.json"
        if state_fd is None:
            _restore_backup(active / "units.jsonl", units_path, manifest["units_present"], TranslationUnit)
            _restore_backup(active / "checkpoint.json", checkpoint_path, manifest["checkpoint_present"], dict)
        else:
            _restore_backup_at(active / "units.jsonl", state_fd, "units.jsonl", manifest["units_present"], TranslationUnit)
            _restore_backup_at(active / "checkpoint.json", state_fd, "checkpoint.json", manifest["checkpoint_present"], dict)
        _fsync_directory(active.parent)
        os.rename(active, committed)
        _fsync_directory(committed.parent)
        _remove_transaction_directory(committed, validate=False)
    if staging.exists():
        _remove_transaction_directory(staging, validate=False)


def _recover_transaction_at(state_fd: int) -> None:
    states = {
        name: _directory_exists_at(state_fd, name)
        for name in (".batch-txn.staging", ".batch-txn.active", ".batch-txn.committed")
    }
    if states[".batch-txn.active"] and states[".batch-txn.committed"]:
        raise BatchError("conflicting transaction states")
    if states[".batch-txn.committed"]:
        _remove_transaction_directory_at(state_fd, ".batch-txn.committed", validate=False)
    if states[".batch-txn.active"]:
        transaction_fd = _open_directory_at(state_fd, ".batch-txn.active")
        try:
            manifest = _validate_transaction_directory_at(transaction_fd)
            _restore_backup_fd(transaction_fd, "units.jsonl", state_fd, "units.jsonl", manifest["units_present"], TranslationUnit)
            _restore_backup_fd(transaction_fd, "checkpoint.json", state_fd, "checkpoint.json", manifest["checkpoint_present"], dict)
        finally:
            os.close(transaction_fd)
        os.rename(".batch-txn.active", ".batch-txn.committed", src_dir_fd=state_fd, dst_dir_fd=state_fd)
        os.fsync(state_fd)
        _remove_transaction_directory_at(state_fd, ".batch-txn.committed", validate=False)
    if states[".batch-txn.staging"]:
        _remove_transaction_directory_at(state_fd, ".batch-txn.staging", validate=False)


def _directory_exists_at(directory_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BatchError(f"symbolic or unsafe transaction directory: {name}")
    return True


def _regular_exists_at(directory_fd: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BatchError(f"symbolic or unsafe state file: {name}")
    return True


def _open_directory_at(directory_fd: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise BatchError(f"cannot open transaction directory safely: {name}: {exc}") from exc


def _validate_transaction_directory_at(transaction_fd: int) -> dict[str, Any]:
    if set(os.listdir(transaction_fd)) != {"units.jsonl", "checkpoint.json", "manifest.json"}:
        raise BatchError("invalid transaction directory contents")
    records = _read_jsonl_at(transaction_fd, "manifest.json", dict)
    expected = {"schema_version", "units_present", "checkpoint_present", "units_sha256", "checkpoint_sha256"}
    if len(records) != 1 or set(records[0]) != expected:
        raise BatchError("invalid transaction manifest")
    manifest = records[0]
    if (
        manifest["schema_version"] != SCHEMA_VERSION
        or type(manifest["units_present"]) is not bool
        or type(manifest["checkpoint_present"]) is not bool
        or manifest["units_sha256"] != _file_sha256_at(transaction_fd, "units.jsonl")
        or manifest["checkpoint_sha256"] != _file_sha256_at(transaction_fd, "checkpoint.json")
    ):
        raise BatchError("invalid transaction manifest")
    return manifest


def _restore_backup_fd(source_fd: int, source_name: str, destination_fd: int, destination_name: str, present: bool, record_type: type[Any]) -> None:
    if present:
        _write_jsonl_at(destination_fd, destination_name, _read_jsonl_at(source_fd, source_name, record_type))
    else:
        try:
            os.unlink(destination_name, dir_fd=destination_fd)
        except FileNotFoundError:
            pass
        os.fsync(destination_fd)


def _remove_transaction_directory_at(
    state_fd: int, name: str, *, validate: bool, hook_cleanup: bool = False
) -> None:
    transaction_fd = _open_directory_at(state_fd, name)
    try:
        if validate:
            _validate_transaction_directory_at(transaction_fd)
        for index, item in enumerate(os.listdir(transaction_fd)):
            info = os.stat(item, dir_fd=transaction_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise BatchError(f"unsafe transaction artifact: {item}")
            os.unlink(item, dir_fd=transaction_fd)
            if hook_cleanup and index == 0:
                _transaction_hook("during_cleanup")
        os.fsync(transaction_fd)
    finally:
        os.close(transaction_fd)
    os.rmdir(name, dir_fd=state_fd)
    os.fsync(state_fd)


def _validate_transaction_directory(directory: Path) -> dict[str, Any]:
    expected_names = {"units.jsonl", "checkpoint.json", "manifest.json"}
    if {item.name for item in directory.iterdir()} != expected_names:
        raise BatchError("invalid transaction directory contents")
    for name in expected_names:
        _require_regular(directory / name)
    records = _read_jsonl(directory / "manifest.json", dict)
    expected_fields = {"schema_version", "units_present", "checkpoint_present", "units_sha256", "checkpoint_sha256"}
    if len(records) != 1 or set(records[0]) != expected_fields:
        raise BatchError("invalid transaction manifest")
    manifest = records[0]
    if (
        manifest["schema_version"] != SCHEMA_VERSION
        or type(manifest["units_present"]) is not bool
        or type(manifest["checkpoint_present"]) is not bool
        or manifest["units_sha256"] != _file_sha256(directory / "units.jsonl")
        or manifest["checkpoint_sha256"] != _file_sha256(directory / "checkpoint.json")
    ):
        raise BatchError("invalid transaction manifest")
    return manifest


def _restore_backup(backup: Path, destination: Path, present: bool, record_type: type[Any]) -> None:
    if present:
        write_jsonl_atomic(destination, _read_jsonl(backup, record_type))
    else:
        destination.unlink(missing_ok=True)


def _restore_backup_at(backup: Path, directory_fd: int, name: str, present: bool, record_type: type[Any]) -> None:
    if present:
        _write_jsonl_at(directory_fd, name, _read_jsonl(backup, record_type))
    else:
        os.unlink(name, dir_fd=directory_fd)


def _remove_transaction_directory(
    directory: Path, *, validate: bool = True, hook_cleanup: bool = False
) -> None:
    if validate:
        _validate_transaction_directory(directory)
    for index, item in enumerate(directory.iterdir()):
        if item.is_symlink() or not item.is_file():
            raise BatchError(f"unsafe transaction artifact: {item}")
        item.unlink()
        if hook_cleanup and index == 0:
            _transaction_hook("during_cleanup")
    directory.rmdir()
    _fsync_directory(directory.parent)


def _require_regular(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BatchError(f"unsafe transaction file: {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BatchError(f"transaction artifact is not regular: {path}")
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    _require_regular(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _file_sha256_at(directory_fd: int, name: str) -> str:
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
    )
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BatchError(f"transaction artifact is not regular: {name}")
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transaction_hook(label: str) -> None:
    """Test seam for simulating process death at durable state boundaries."""
    del label


def _validate_batch_manifest(path: Path, checkpoint: dict[str, Any], units: list[TranslationUnit]) -> None:
    records = _read_jsonl(path, dict)
    _validate_batch_records(records, checkpoint, units)


def _validate_batch_manifest_at(directory_fd: int, name: str, checkpoint: dict[str, Any], units: list[TranslationUnit]) -> None:
    records = _read_jsonl_at(directory_fd, name, dict)
    _validate_batch_records(records, checkpoint, units)


def _validate_batch_records(records: list[dict[str, Any]], checkpoint: dict[str, Any], units: list[TranslationUnit]) -> None:
    expected_record_keys = {"id", "source", "source_sha256", "target", "context", "status", "findings", "batch"}
    if any(set(record) != expected_record_keys for record in records):
        raise BatchError("batch record fields mismatch")
    if [record.get("id") for record in records] != checkpoint["unit_ids"]:
        raise BatchError("batch unit IDs do not match exported manifest")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "sequence": checkpoint["current_sequence"],
        "path": checkpoint["current_batch"],
        "count": len(checkpoint["unit_ids"]),
        "unit_ids": checkpoint["unit_ids"],
        "digest": checkpoint["batch_digest"],
    }
    if any(record.get("batch") != expected for record in records):
        raise BatchError("changed batch metadata")
    unit_by_id = {unit.id: unit for unit in units}
    exported = [unit_by_id.get(identifier) for identifier in checkpoint["unit_ids"]]
    if any(unit is None for unit in exported):
        raise BatchError("batch immutable units are unavailable")
    digest = _batch_digest(exported, checkpoint["current_sequence"], checkpoint["current_batch"], checkpoint["unit_ids"])  # type: ignore[arg-type]
    if digest != checkpoint["batch_digest"]:
        raise BatchError("batch digest does not match immutable units")
    for record, unit in zip(records, exported, strict=True):
        canonical = _batch_record(unit, checkpoint["current_sequence"], checkpoint["current_batch"], checkpoint["unit_ids"], checkpoint["batch_digest"])  # type: ignore[arg-type]
        if not isinstance(record.get("target"), str) or any(
            record.get(key) != canonical[key]
            for key in ("id", "source", "source_sha256", "context", "status", "findings")
        ):
            raise BatchError("changed immutable batch record")


def _safe_root(root: Path) -> Path:
    try:
        candidate = Path(root).absolute()
        if candidate.is_symlink() or not candidate.is_dir():
            raise OSError("root is not a directory")
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise BatchError(f"invalid root: {exc}") from exc


def _validate_state_layout(root: Path, units_path: Path) -> tuple[Path, Path, tuple[int, int]]:
    """Bind state and recovery to the one canonical, non-symlinked layout."""
    state = root / "translations"
    if state.is_symlink():
        raise BatchError("translations directory must not be symbolic")
    if not state.is_dir():
        raise BatchError("translations directory is missing")
    expected_units = state / "units.jsonl"
    candidate = Path(units_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        if candidate.resolve(strict=True) != expected_units.resolve(strict=True):
            raise BatchError("units_path must be canonical units state path")
    except OSError as exc:
        raise BatchError(f"invalid canonical units path: {exc}") from exc
    if expected_units.is_symlink() or not expected_units.is_file():
        raise BatchError("canonical units state must be a regular file")
    checkpoint = state / "checkpoint.json"
    if checkpoint.is_symlink() or (checkpoint.exists() and not checkpoint.is_file()):
        raise BatchError("canonical checkpoint must be a regular file")
    info = state.stat(follow_symlinks=False)
    return expected_units.resolve(strict=True), checkpoint, (info.st_dev, info.st_ino)


def _assert_state_identity(root: Path, expected: tuple[int, int]) -> None:
    state = root / "translations"
    if state.is_symlink():
        raise BatchError("translations directory identity changed")
    info = state.stat(follow_symlinks=False)
    if (info.st_dev, info.st_ino) != expected:
        raise BatchError("translations directory identity changed")


def _layout_hook() -> None:
    """Test seam for adversarial parent replacement after validation."""


def _write_hook() -> None:
    """Test seam immediately before an anchored mutation."""


@contextmanager
def _exclusive_lock(root: Path):
    with workflow_lock(root, shared=False) as root_fd:
        yield root_fd


@contextmanager
def workflow_lock(root: Path, *, shared: bool = False):
    """Reentrant process-wide workflow lock anchored on the root directory."""
    if os.name == "nt" or os.open not in os.supports_dir_fd:
        raise BatchError("workflow locking requires POSIX dir_fd support")
    canonical = Path(root).resolve(strict=True)
    held_root = getattr(_LOCK_STATE, "root", None)
    depth = getattr(_LOCK_STATE, "depth", 0)
    if depth:
        if held_root != canonical:
            raise BatchError("cannot nest workflow locks for different roots")
        if getattr(_LOCK_STATE, "shared", False) and not shared:
            raise BatchError("cannot upgrade a shared workflow lock")
        _LOCK_STATE.depth = depth + 1
        try:
            yield _LOCK_STATE.fd
        finally:
            _LOCK_STATE.depth -= 1
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(canonical, directory_flags)
    _bind_descriptor_identity(root_fd, canonical, "root")
    batch_fd = -1
    try:
        import fcntl
        fcntl.flock(root_fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if not shared:
            flags |= os.O_CREAT
        try:
            batch_fd = os.open(".batch.lock", flags, 0o600, dir_fd=root_fd)
        except FileNotFoundError:
            batch_fd = -1
        if batch_fd >= 0:
            if not stat.S_ISREG(os.fstat(batch_fd).st_mode):
                raise BatchError("batch lock is not a regular file")
            fcntl.flock(batch_fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        _LOCK_STATE.root = canonical; _LOCK_STATE.fd = root_fd
        _LOCK_STATE.depth = 1; _LOCK_STATE.shared = shared
        try:
            yield root_fd
        finally:
            _LOCK_STATE.depth = 0; _LOCK_STATE.root = None; _LOCK_STATE.fd = -1
            _LOCK_STATE.shared = False
            if batch_fd >= 0:
                fcntl.flock(batch_fd, fcntl.LOCK_UN)
            fcntl.flock(root_fd, fcntl.LOCK_UN)
    finally:
        if batch_fd >= 0:
            os.close(batch_fd)
        os.close(root_fd)


@contextmanager
def _state_descriptors(root_fd: int, root: Path):
    if os.name == "nt" or os.open not in os.supports_dir_fd:
        raise BatchError("batch mutation requires POSIX dir_fd support")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        state_fd = os.open("translations", flags, dir_fd=root_fd)
    except OSError as exc:
        raise BatchError(f"translations may be symbolic or unsafe: {exc}") from exc
    batches_fd = -1
    try:
        _bind_descriptor_identity(root_fd, root, "root")
        _bind_descriptor_identity(state_fd, root / "translations", "translations")
        try:
            batches_fd = os.open("batches", flags, dir_fd=state_fd)
        except FileNotFoundError:
            os.mkdir("batches", mode=0o700, dir_fd=state_fd)
            os.fsync(state_fd)
            batches_fd = os.open("batches", flags, dir_fd=state_fd)
        except OSError as exc:
            raise BatchError(f"batches may be a symbolic link or unsafe: {exc}") from exc
        _descriptor_hook()
        _bind_descriptor_identity(root_fd, root, "root")
        _bind_descriptor_identity(state_fd, root / "translations", "translations")
        _bind_descriptor_identity(
            batches_fd, root / "translations/batches", "batches"
        )
        yield state_fd, batches_fd
    finally:
        if batches_fd >= 0:
            os.close(batches_fd)
        os.close(state_fd)


def _descriptor_hook() -> None:
    """Test seam after descriptor open and before path-identity binding."""


def _bind_descriptor_identity(descriptor: int, path: Path, label: str) -> None:
    try:
        path_info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BatchError(f"{label} descriptor identity cannot be validated: {exc}") from exc
    fd_info = os.fstat(descriptor)
    if (fd_info.st_dev, fd_info.st_ino) != (path_info.st_dev, path_info.st_ino):
        raise BatchError(f"{label} descriptor identity changed")


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


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


def _read_jsonl_at(directory_fd: int, name: str, record_type: type[Any]) -> list[Any]:
    try:
        return read_jsonl_at(directory_fd, name, record_type)
    except StateError as exc:
        raise BatchError(str(exc)) from exc


def _write_jsonl_at(directory_fd: int, name: str, records: Any) -> None:
    try:
        write_jsonl_atomic_at(directory_fd, name, records)
    except StateError as exc:
        raise BatchError(str(exc)) from exc

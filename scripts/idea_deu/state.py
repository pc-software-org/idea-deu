"""Deterministic, atomic JSON Lines state persistence."""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from collections.abc import Mapping as MappingABC
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, Iterable, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T")


class StateError(ValueError):
    """Raised when persisted state is invalid or cannot be written safely."""


def read_jsonl(path: Path, record_type: type[T]) -> list[T]:
    records: list[T] = []
    seen_ids: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    raise StateError(f"line {line_number} is empty")
                try:
                    value = json.loads(
                        line,
                        object_pairs_hook=_object_without_duplicates,
                        parse_constant=_reject_json_constant,
                    )
                except (json.JSONDecodeError, StateError) as exc:
                    raise StateError(
                        f"{path}: invalid JSON on line {line_number}: {exc}"
                    ) from exc
                if not isinstance(value, dict):
                    raise StateError(f"line {line_number} must be a JSON object")
                _check_duplicate_id(value, seen_ids)
                records.append(_construct(record_type, value))
    except StateError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise StateError(str(exc)) from exc
    return records


def write_jsonl_atomic(path: Path, records: Iterable[T]) -> None:
    path = Path(path)
    if path.is_symlink():
        raise StateError(f"refusing to replace symbolic link: {path}")
    try:
        objects = [_as_json_object(record) for record in records]
        seen_ids: set[str] = set()
        for value in objects:
            _check_duplicate_id(value, seen_ids)
        serialized = [
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for value in objects
        ]
        serialized.sort(key=lambda line: (_record_id(json.loads(line)) or "", line))
        payload = (("\n".join(serialized) + "\n") if serialized else "").encode("utf-8")
    except (StateError, TypeError, ValueError) as exc:
        if isinstance(exc, StateError):
            raise
        raise StateError(str(exc)) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor = -1
    temp_path: Path | None = None
    backup_path: Path | None = None
    preserve_backup = False
    had_existing_file = path.exists()
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        os.fchmod(descriptor, existing_mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            _write_payload(stream, payload)
        if had_existing_file:
            backup_path = _backup_file(path)
        os.replace(temp_path, path)
        temp_path = None
        try:
            _fsync_directory(path.parent)
        except OSError as exc:
            try:
                _rollback_replace(path, backup_path, had_existing_file)
            except OSError as rollback_exc:
                preserve_backup = backup_path is not None and backup_path.exists()
                recovery = (
                    f"; backup retained at {backup_path}"
                    if preserve_backup
                    else ""
                )
                raise StateError(
                    f"{exc}; rollback failed: {rollback_exc}{recovery}"
                ) from exc
            raise
    except OSError as exc:
        raise StateError(str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        if backup_path is not None and not preserve_backup:
            try:
                backup_path.unlink()
            except FileNotFoundError:
                pass


def _write_payload(stream: Any, payload: bytes) -> None:
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())


def _backup_file(path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".bak", dir=path.parent
    )
    os.close(descriptor)
    backup_path = Path(name)
    backup_path.unlink()
    try:
        os.link(path, backup_path)
    except OSError:
        try:
            backup_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup_path


def _rollback_replace(
    path: Path, backup_path: Path | None, had_existing_file: bool
) -> None:
    if had_existing_file:
        if backup_path is None:
            raise OSError("original-file backup is unavailable")
        os.replace(backup_path, path)
    else:
        path.unlink()
    _fsync_directory(path.parent)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> None:
    raise StateError(f"non-finite JSON number is not allowed: {token}")


def _as_json_object(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        value = record.to_dict()
    elif isinstance(record, dict):
        value = record
    elif is_dataclass(record) and not isinstance(record, type):
        value = {field.name: getattr(record, field.name) for field in fields(record)}
    else:
        raise TypeError(f"record {record!r} is not serializable")
    converted = _json_value(value, "$")
    if not isinstance(converted, dict):
        raise TypeError("record must serialize to a JSON object")
    return converted


def _json_value(value: Any, path: str) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(
                getattr(value, field.name), _json_child_path(path, field.name)
            )
            for field in fields(value)
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise StateError(
                    f"{path}: JSON objects require string keys; "
                    f"got {type(key).__name__} key {key!r}"
                )
            result[key] = _json_value(item, _json_child_path(path, key))
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _json_child_path(path: str, key: str) -> str:
    if key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{key!r}]"


def _record_id(value: dict[str, Any]) -> str | None:
    if "resource_id" in value:
        identifier = value["resource_id"]
    elif "id" in value:
        identifier = value["id"]
    else:
        return None
    if not isinstance(identifier, str) or not identifier:
        raise StateError("record ID must be a non-empty string")
    return identifier


def _check_duplicate_id(value: dict[str, Any], seen: set[str]) -> None:
    identifier = _record_id(value)
    if identifier is None:
        return
    if identifier in seen:
        raise StateError(f"duplicate record ID: {identifier}")
    seen.add(identifier)


def _construct(record_type: type[T], value: dict[str, Any]) -> T:
    if record_type is dict:
        return value  # type: ignore[return-value]
    if not is_dataclass(record_type):
        if hasattr(record_type, "from_dict"):
            return record_type.from_dict(value)  # type: ignore[attr-defined,no-any-return]
        return record_type(**value)
    hints = get_type_hints(record_type)
    field_names = {field.name for field in fields(record_type)}
    missing = sorted(field_names - value.keys())
    extra = sorted(value.keys() - field_names)
    if missing or extra:
        raise ValueError(f"record fields mismatch: missing={missing}, extra={extra}")
    converted: dict[str, Any] = {}
    for key, item in value.items():
        try:
            converted[key] = _convert_type(item, hints.get(key, Any))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: {exc}") from exc
    return record_type(**converted)


def _convert_type(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    if annotation is Any:
        return value
    if annotation in (str, int, bool, float):
        if type(value) is not annotation:
            raise TypeError(
                f"expected {annotation.__name__}, got {type(value).__name__}"
            )
        return value
    if annotation in (None, type(None)):
        if value is not None:
            raise TypeError(f"expected null, got {type(value).__name__}")
        return None
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        member_types = {type(member.value) for member in annotation}
        if type(value) not in member_types:
            raise TypeError(f"invalid {annotation.__name__} input type")
        return annotation(value)
    if annotation is Path:
        if not isinstance(value, str):
            raise TypeError(f"expected path string, got {type(value).__name__}")
        return Path(value)
    if origin is list:
        if not isinstance(value, list):
            raise TypeError(f"expected list, got {type(value).__name__}")
        item_type = get_args(annotation)[0] if get_args(annotation) else Any
        return [_convert_type(item, item_type) for item in value]
    if origin is tuple:
        if not isinstance(value, list):
            raise TypeError(f"expected JSON array, got {type(value).__name__}")
        item_types = get_args(annotation)
        if len(item_types) == 2 and item_types[1] is Ellipsis:
            return tuple(_convert_type(item, item_types[0]) for item in value)
        if item_types and len(item_types) != len(value):
            raise ValueError(f"expected tuple of length {len(item_types)}")
        return tuple(
            _convert_type(item, item_types[index] if item_types else Any)
            for index, item in enumerate(value)
        )
    if origin in (dict, MappingABC):
        if not isinstance(value, dict):
            raise TypeError(f"expected object, got {type(value).__name__}")
        arguments = get_args(annotation)
        key_type, item_type = arguments if len(arguments) == 2 else (Any, Any)
        return {
            _convert_type(key, key_type): _convert_type(item, item_type)
            for key, item in value.items()
        }
    if origin in (Union, UnionType):
        errors: list[str] = []
        for candidate in get_args(annotation):
            try:
                return _convert_type(value, candidate)
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
        raise TypeError("does not match union: " + "; ".join(errors))
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, dict):
            raise TypeError(f"expected object, got {type(value).__name__}")
        return _construct(annotation, value)
    if isinstance(annotation, type) and type(value) is annotation:
        return value
    raise TypeError(f"unsupported or mismatched annotation {annotation!r}")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIRECTORY_SYNC_ERRNOS:
            raise
    finally:
        os.close(descriptor)


_UNSUPPORTED_DIRECTORY_SYNC_ERRNOS = {
    errno.EINVAL,
    getattr(errno, "ENOSYS", errno.EINVAL),
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}

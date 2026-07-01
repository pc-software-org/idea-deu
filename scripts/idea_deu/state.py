"""Deterministic, atomic JSON Lines state persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
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
                    value = json.loads(line, object_pairs_hook=_object_without_duplicates)
                except (json.JSONDecodeError, StateError) as exc:
                    raise StateError(f"invalid JSON on line {line_number}: {exc}") from exc
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
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for value in objects
        ]
        serialized.sort(key=lambda line: (_record_id(json.loads(line)) or "", line))
        payload = (("\n".join(serialized) + "\n") if serialized else "").encode("utf-8")
    except (StateError, TypeError, ValueError) as exc:
        if isinstance(exc, StateError):
            raise
        raise StateError(str(exc)) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor = -1
    temp_path: Path | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        os.fchmod(descriptor, existing_mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            _write_payload(stream, payload)
        os.replace(temp_path, path)
        temp_path = None
        _fsync_directory(path.parent)
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


def _write_payload(stream: Any, payload: bytes) -> None:
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _as_json_object(record: Any) -> dict[str, Any]:
    if hasattr(record, "to_dict"):
        value = record.to_dict()
    elif isinstance(record, dict):
        value = record
    elif is_dataclass(record) and not isinstance(record, type):
        value = {field.name: getattr(record, field.name) for field in fields(record)}
    else:
        raise TypeError(f"record {record!r} is not serializable")
    converted = _json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("record must serialize to a JSON object")
    return converted


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _record_id(value: dict[str, Any]) -> str | None:
    identifier = value.get("resource_id", value.get("id"))
    return identifier if isinstance(identifier, str) else None


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
    converted = {
        key: _convert_type(item, hints.get(key, Any)) for key, item in value.items()
    }
    return record_type(**converted)


def _convert_type(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if annotation is Path:
        return Path(value)
    if origin in (tuple, list):
        item_type = get_args(annotation)[0] if get_args(annotation) else Any
        converted = [_convert_type(item, item_type) for item in value]
        return tuple(converted) if origin is tuple else converted
    if origin in (Union, UnionType):
        for candidate in get_args(annotation):
            if candidate is type(None) and value is None:
                return None
            try:
                return _convert_type(value, candidate)
            except (TypeError, ValueError):
                continue
    if (
        isinstance(annotation, type)
        and is_dataclass(annotation)
        and isinstance(value, dict)
    ):
        return _construct(annotation, value)
    return value


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {getattr(os, "EINVAL", 22), getattr(os, "ENOTSUP", 45)}:
            raise
    finally:
        os.close(descriptor)

from __future__ import annotations

import io
import warnings
import zipfile
from pathlib import Path


def jar_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, content in entries:
                archive.writestr(name, content)
    return target.getvalue()


def write_outer_archive(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return path


def with_unsupported_compression(archive: bytes) -> bytes:
    result = bytearray(archive)
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        position = result.find(signature)
        if position < 0:
            raise AssertionError("ZIP signature not found")
        result[position + offset : position + offset + 2] = (99).to_bytes(2, "little")
    return bytes(result)

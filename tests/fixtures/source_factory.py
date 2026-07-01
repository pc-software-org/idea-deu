"""Factories for small IntelliJ source archives."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def make_source_archive(
    directory: Path,
    *,
    version: str = "2025.3.1.1",
    build_number: str = "253.29346.240",
    product_code: str = "IU",
    product_info: str | None = None,
    include_product_info: bool = True,
) -> tuple[Path, str]:
    archive = directory / "idea-test.win.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as source_zip:
        source_zip.writestr("bin/idea64.exe", b"fixture")
        if include_product_info:
            document = product_info or json.dumps(
                {
                    "version": version,
                    "buildNumber": build_number,
                    "productCode": product_code,
                }
            )
            source_zip.writestr("product-info.json", document)

    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def mark_product_info_encrypted(archive: Path) -> str:
    """Set the encryption flag in both ZIP headers for product-info.json."""
    content = bytearray(archive.read_bytes())
    filename = b"product-info.json"
    header_layouts = ((30, b"PK\x03\x04", 6), (46, b"PK\x01\x02", 8))
    changed_headers = 0
    position = 0
    while (position := content.find(filename, position)) >= 0:
        for filename_offset, signature, flag_offset in header_layouts:
            header = position - filename_offset
            if content[header : header + 4] != signature:
                continue
            flags = int.from_bytes(
                content[header + flag_offset : header + flag_offset + 2], "little"
            )
            content[header + flag_offset : header + flag_offset + 2] = (
                flags | 1
            ).to_bytes(2, "little")
            changed_headers += 1
            break
        position += len(filename)

    if changed_headers != 2:
        raise AssertionError(
            "could not mark both product-info.json ZIP headers encrypted"
        )
    archive.write_bytes(content)
    return hashlib.sha256(content).hexdigest()

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

"""Deterministic, code-free IntelliJ language-pack packaging."""
from __future__ import annotations
import io
import stat
import zipfile
from collections.abc import Sequence
from pathlib import Path
from xml.etree import ElementTree

from .generator import GenerationError, GenerationResult, recompute_generation
from .models import Inventory, TranslationUnit
from .path_safety import OutputPathError, atomic_write_bytes

class PackageError(ValueError): pass

_TIME=(1980,1,1,0,0,0)

def build_plugin_package(result: GenerationResult, trusted_inventory: Inventory,
                         trusted_units: Sequence[TranslationUnit], trusted_provider: object,
                         descriptor: Path, destination: Path, *,
                         dedupe_identical: bool = False) -> Path:
    if not isinstance(result, GenerationResult):
        raise PackageError("GenerationResult required")
    canonical_units = tuple(trusted_units)
    try:
        trusted_sources: dict[tuple[str, str], bytes] = {}
        for record in trusted_inventory.resources:
            key = (record.container, record.resource_path)
            if key not in trusted_sources:
                trusted_sources[key] = trusted_provider.read(record)  # type: ignore[attr-defined]
        entries = recompute_generation(trusted_inventory, canonical_units, trusted_sources,
                                       dedupe_identical=dedupe_identical)
    except GenerationError as exc:
        raise PackageError(f"invalid trusted generation inputs: {exc}") from exc
    trusted_source_evidence = tuple(
        (container, path, data) for (container, path), data in sorted(trusted_sources.items())
    )
    supplied: dict[str, bytes] = {}
    for name, data in result.files:
        if name in supplied:
            raise PackageError(f"duplicate generated evidence: {name}")
        supplied[name] = data
    if (result.inventory != trusted_inventory or result.units != canonical_units or
            result.sources != trusted_source_evidence or
            result.dedupe_identical is not dedupe_identical or supplied != entries):
        raise PackageError("result evidence does not match trusted canonical recomputation")
    try: descriptor_bytes = Path(descriptor).read_bytes()
    except OSError as exc: raise PackageError(f"cannot read plugin descriptor: {exc}") from exc
    _validate_descriptor(descriptor_bytes)
    entries["META-INF/plugin.xml"] = descriptor_bytes
    jar = _zip_bytes(entries)
    payload = {"idea-deu/lib/idea-deu.jar": jar}
    destination = Path(destination)
    try:
        atomic_write_bytes(destination, _zip_bytes(payload))
    except (OSError, OutputPathError) as exc:
        raise PackageError(str(exc)) from exc
    return destination

def _validate_descriptor(data: bytes) -> None:
    try: root=ElementTree.fromstring(data)
    except ElementTree.ParseError as exc: raise PackageError(f"invalid plugin XML: {exc}") from exc
    expected = (
        ("id", {}, "org.pc-software.idea-deu", ()),
        ("name", {}, "German Language Pack", ()),
        ("version", {}, "2025.3.1.1", ()),
        ("vendor", {}, "PC-Software", ()),
        ("idea-version", {"since-build": "253.29346.240", "until-build": "253.29346.240"}, "", ()),
        ("depends", {}, "com.intellij.modules.platform", ()),
        ("extensions", {"defaultExtensionNs": "com.intellij"}, "", (
            ("languageBundle", {"locale": "de"}, "", ()),
        )),
    )
    if root.tag != "idea-plugin" or root.attrib or _descriptor_children(root) != expected:
        raise PackageError("plugin descriptor identity or compatibility mismatch")


def _descriptor_children(element: ElementTree.Element) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (child.tag, dict(child.attrib), (child.text or "").strip(), _descriptor_children(child))
        for child in element
    )

def _zip_bytes(entries: dict[str,bytes]) -> bytes:
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in sorted(entries):
            info=zipfile.ZipInfo(name,_TIME); info.compress_type=zipfile.ZIP_DEFLATED
            info.create_system=3; info.external_attr=(stat.S_IFREG|0o644)<<16; info.flag_bits=0x800
            archive.writestr(info,entries[name],compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    return stream.getvalue()

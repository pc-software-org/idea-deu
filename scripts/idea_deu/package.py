"""Deterministic, code-free IntelliJ language-pack packaging."""
from __future__ import annotations
import io
import stat
import struct
import tempfile
import zipfile
import zlib
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
                         version: str, since_build: str, until_build: str, change_notes: str,
                         dedupe_identical: bool = False, trusted_root: Path | None = None) -> Path:
    payload = plugin_package_bytes(result, trusted_inventory, trusted_units, trusted_provider,
                                   descriptor, version=version, since_build=since_build,
                                   until_build=until_build, change_notes=change_notes,
                                   dedupe_identical=dedupe_identical)
    destination = Path(destination)
    try:
        atomic_write_bytes(destination, payload, trusted_root=trusted_root)
    except (OSError, OutputPathError) as exc:
        raise PackageError(str(exc)) from exc
    return destination


def plugin_package_bytes(result: GenerationResult, trusted_inventory: Inventory,
                         trusted_units: Sequence[TranslationUnit], trusted_provider: object,
                         descriptor: Path, *, version: str, since_build: str, until_build: str,
                         change_notes: str, dedupe_identical: bool = False) -> bytes:
    """Independently recompute the exact deterministic package bytes."""
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
    try: template_bytes = Path(descriptor).read_bytes()
    except OSError as exc: raise PackageError(f"cannot read plugin descriptor: {exc}") from exc
    descriptor_bytes = render_descriptor(template_bytes, version=version,
                                         since_build=since_build, until_build=until_build,
                                         change_notes=change_notes)
    _validate_descriptor(descriptor_bytes, version=version,
                         since_build=since_build, until_build=until_build,
                         change_notes=change_notes)
    entries["META-INF/plugin.xml"] = descriptor_bytes
    jar = _zip_bytes(entries)
    payload = {"idea-deu/lib/idea-deu.jar": jar}
    return _zip_bytes(payload)


def verify_plugin_package(path: Path, result: GenerationResult, descriptor: Path, *,
                          version: str, since_build: str, until_build: str,
                          change_notes: str) -> bool:
    """Strictly verify an existing deterministic outer and inner plugin ZIP."""
    try:
        template_bytes = Path(descriptor).read_bytes()
        descriptor_bytes = render_descriptor(template_bytes, version=version,
                                             since_build=since_build, until_build=until_build,
                                             change_notes=change_notes)
        _validate_descriptor(descriptor_bytes, version=version,
                             since_build=since_build, until_build=until_build,
                             change_notes=change_notes)
        expected = dict(result.files); expected["META-INF/plugin.xml"] = descriptor_bytes
        canonical_inner = _zip_bytes(expected)
        with Path(path).open("rb") as outer_file, zipfile.ZipFile(outer_file) as outer:
            if not _canonical_archive(outer_file, outer, {"idea-deu/lib/idea-deu.jar": canonical_inner}): return False
            if outer.namelist() != ["idea-deu/lib/idea-deu.jar"]: return False
            outer_info = outer.infolist()[0]
            if not _canonical_info(outer_info): return False
            with outer.open(outer_info) as jar_stream, tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
                remaining = outer_info.file_size
                while remaining:
                    chunk = jar_stream.read(min(1024 * 1024, remaining))
                    if not chunk: return False
                    spool.write(chunk); remaining -= len(chunk)
                if jar_stream.read(1): return False
                spool.seek(0)
                with zipfile.ZipFile(spool) as inner:
                    if not _canonical_archive(spool, inner, expected): return False
                    if inner.namelist() != sorted(expected): return False
                    if len(inner.infolist()) != len(expected): return False
                    for info in inner.infolist():
                        data = expected.get(info.filename)
                        if data is None or not _canonical_info(info) or info.file_size != len(data): return False
                        with inner.open(info) as stream:
                            if stream.read(len(data) + 1) != data: return False
        return True
    except (OSError, ValueError, RuntimeError, NotImplementedError, EOFError,
            zlib.error, zipfile.BadZipFile, PackageError):
        return False


def _canonical_info(info: zipfile.ZipInfo) -> bool:
    return (info.date_time == _TIME and info.compress_type == zipfile.ZIP_DEFLATED and
            info.flag_bits == 0 and info.extra == b"" and info.comment == b"" and
            info.internal_attr == 0 and info.create_system == 3 and
            info.create_version == 20 and info.extract_version == 20 and info.volume == 0 and
            info.external_attr == (stat.S_IFREG | 0o644) << 16 and
            not stat.S_ISLNK((info.external_attr >> 16) & 0xffff))


def _canonical_archive(stream: object, archive: zipfile.ZipFile,
                       expected: dict[str, bytes]) -> bool:
    stream.seek(0, 2)  # type: ignore[attr-defined]
    size = stream.tell()  # type: ignore[attr-defined]
    if size < 22: return False
    stream.seek(0)  # type: ignore[attr-defined]
    if stream.read(4) != b"PK\x03\x04": return False  # type: ignore[attr-defined]
    stream.seek(size - 22)  # type: ignore[attr-defined]
    eocd = stream.read(22)  # type: ignore[attr-defined]
    signature, disk, cd_disk, disk_entries, entries, cd_size, cd_offset, comment_size = struct.unpack(
        "<4s4H2LH", eocd)
    infos = archive.infolist()
    if (signature != b"PK\x05\x06" or disk or cd_disk or comment_size or
        disk_entries != entries or entries != len(infos) or
        cd_offset + cd_size != size - 22 or archive.start_dir != cd_offset or archive.comment):
        return False
    previous_end = 0
    for info in infos:
        if info.header_offset != previous_end or info.header_offset >= cd_offset: return False
        stream.seek(info.header_offset)  # type: ignore[attr-defined]
        header = stream.read(30)  # type: ignore[attr-defined]
        if len(header) != 30 or header[:4] != b"PK\x03\x04": return False
        (_signature, extract_version, flags, compression, dos_time, dos_date, crc,
         compressed_size, uncompressed_size, filename_size, extra_size) = struct.unpack("<4s5H3L2H", header)
        if (extract_version != 20 or flags != 0 or compression != zipfile.ZIP_DEFLATED or
            dos_time != 0 or dos_date != 33 or extra_size != 0 or crc != info.CRC or
            compressed_size != info.compress_size or uncompressed_size != info.file_size): return False
        raw_name = stream.read(filename_size)  # type: ignore[attr-defined]
        raw_extra = stream.read(extra_size)  # type: ignore[attr-defined]
        expected_name = info.filename.encode("utf-8" if flags & 0x800 else "cp437")
        if raw_name != expected_name or raw_extra or info.filename not in expected: return False
        compressed = stream.read(compressed_size)  # type: ignore[attr-defined]
        if compressed != _raw_deflate(expected[info.filename]): return False
        if crc != zlib.crc32(expected[info.filename]) & 0xffffffff or uncompressed_size != len(expected[info.filename]): return False
        data_end = info.header_offset + 30 + filename_size + extra_size + compressed_size
        if data_end > cd_offset: return False
        previous_end = data_end
    return bool(infos) and infos[0].header_offset == 0 and previous_end == cd_offset


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()

_DESCRIPTION = ("German (Deutsch) language pack for IntelliJ IDEA 2026.1. Translates the IDE "
                "user interface, inspection and intention descriptions, tips, and file and "
                "postfix templates into German. Deutsches Sprachpaket für IntelliJ IDEA 2026.1.")


def render_descriptor(template: bytes, *, version: str, since_build: str, until_build: str,
                      change_notes: str) -> bytes:
    """Fill the plugin.xml template's version/compatibility/change-notes placeholders.

    config/product.json is the single source of truth for the plugin version and
    the since/until-build range; a build never derives them from a git tag. The
    change-notes HTML (Marketplace "What's new") comes from CHANGELOG.md and is
    rendered into the descriptor's CDATA section — never empty, so no upload ever
    ships blank release notes."""
    if not change_notes.strip():
        raise PackageError("change-notes must not be empty")
    text = template.decode("utf-8")
    for token, value in (
        ("@PLUGIN_VERSION@", version),
        ("@SINCE_BUILD@", since_build),
        ("@UNTIL_BUILD@", until_build),
        ("@CHANGE_NOTES@", _cdata_safe(change_notes)),
    ):
        if token not in text:
            raise PackageError(f"plugin descriptor template missing placeholder {token}")
        text = text.replace(token, value)
    return text.encode("utf-8")


def _cdata_safe(text: str) -> str:
    """Escape ``]]>`` so *text* cannot close its enclosing CDATA section early."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def _validate_descriptor(data: bytes, *, version: str, since_build: str, until_build: str,
                         change_notes: str) -> None:
    try: root=ElementTree.fromstring(data)
    except ElementTree.ParseError as exc: raise PackageError(f"invalid plugin XML: {exc}") from exc
    expected = (
        ("id", {}, "org.pc-software.idea-deu", ()),
        ("name", {}, "German Language Pack", ()),
        ("description", {}, _DESCRIPTION, ()),
        ("change-notes", {}, change_notes.strip(), ()),
        ("version", {}, version, ()),
        ("vendor", {}, "PC-Software", ()),
        ("idea-version", {"since-build": since_build, "until-build": until_build}, "", ()),
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

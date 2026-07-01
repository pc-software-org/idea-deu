"""Deterministic, code-free IntelliJ language-pack packaging."""
from __future__ import annotations
import io
import stat
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from .generator import GenerationResult
from .models import ProcessingStatus
from .validation import Severity

class PackageError(ValueError): pass

_TIME=(1980,1,1,0,0,0)

def build_plugin_package(result: GenerationResult, descriptor: Path, destination: Path) -> Path:
    if not isinstance(result, GenerationResult) or not result.is_verified():
        raise PackageError("verified GenerationResult required")
    if not result.complete or result.unresolved_collisions:
        raise PackageError("GenerationResult is incomplete or has unresolved collisions")
    bad = [unit.id for unit in result.units if unit.status not in {
        ProcessingStatus.TECHNICALLY_REVIEWED, ProcessingStatus.LINGUISTICALLY_REVIEWED
    } or any(finding.severity is Severity.BLOCKING for finding in unit.findings)]
    if bad: raise PackageError("GenerationResult contains blocked units: " + ", ".join(bad))
    try: descriptor_bytes = Path(descriptor).read_bytes()
    except OSError as exc: raise PackageError(f"cannot read plugin descriptor: {exc}") from exc
    _validate_descriptor(descriptor_bytes)
    entries = _regular_tree(result.root)
    expected = dict(result.files)
    if set(entries) != set(expected): raise PackageError("generated resource set changed or contains unsupported content")
    for name, data in entries.items():
        if __import__("hashlib").sha256(data).hexdigest() != expected[name]:
            raise PackageError(f"generated resource changed: {name}")
    entries["META-INF/plugin.xml"] = descriptor_bytes
    jar = _zip_bytes(entries)
    payload = {"idea-deu/lib/idea-deu.jar": jar}
    destination = Path(destination); _assert_safe_destination_parent(destination); destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_symlink(): raise PackageError("refusing symbolic-link destination")
    destination.write_bytes(_zip_bytes(payload))
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

def _regular_tree(root: Path) -> dict[str,bytes]:
    if root.is_symlink() or not root.is_dir(): raise PackageError("resource root is not a regular directory")
    result={}; folded={}
    for path in root.rglob("*"):
        if path.is_symlink(): raise PackageError(f"symbolic link in resources: {path}")
        if path.is_dir(): continue
        if not path.is_file(): raise PackageError(f"non-regular resource: {path}")
        relative=path.relative_to(root).as_posix(); pure=PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative: raise PackageError(f"unsafe resource path: {relative}")
        if relative in result: raise PackageError(f"duplicate resource: {relative}")
        if relative.casefold() in folded: raise PackageError(f"case-fold resource collision: {folded[relative.casefold()]}, {relative}")
        if pure.suffix.lower() not in {".properties", ".html", ".xml"}:
            raise PackageError(f"unsupported generated content: {relative}")
        folded[relative.casefold()]=relative
        result[relative]=path.read_bytes()
    return result

def _assert_safe_destination_parent(destination: Path) -> None:
    current=destination.absolute().parent
    while True:
        try: mode=current.lstat().st_mode
        except FileNotFoundError:
            if current==current.parent: return
            current=current.parent
            continue
        if stat.S_ISLNK(mode): raise PackageError(f"symbolic-link destination parent: {current}")
        if not stat.S_ISDIR(mode): raise PackageError(f"unsafe destination parent: {current}")
        return

def _zip_bytes(entries: dict[str,bytes]) -> bytes:
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in sorted(entries):
            info=zipfile.ZipInfo(name,_TIME); info.compress_type=zipfile.ZIP_DEFLATED
            info.create_system=3; info.external_attr=(stat.S_IFREG|0o644)<<16; info.flag_bits=0x800
            archive.writestr(info,entries[name],compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    return stream.getvalue()

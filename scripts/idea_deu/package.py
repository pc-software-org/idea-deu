"""Deterministic, code-free IntelliJ language-pack packaging."""
from __future__ import annotations
import io
import stat
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

class PackageError(ValueError): pass

_TIME=(1980,1,1,0,0,0)

def build_plugin_package(resources: Path, descriptor: Path, destination: Path) -> Path:
    try: descriptor_bytes = Path(descriptor).read_bytes()
    except OSError as exc: raise PackageError(f"cannot read plugin descriptor: {exc}") from exc
    _validate_descriptor(descriptor_bytes)
    entries = _regular_tree(Path(resources))
    jar = _zip_bytes(entries)
    payload = {"idea-deu/META-INF/plugin.xml": descriptor_bytes, "idea-deu/lib/idea-deu.jar": jar}
    destination = Path(destination); destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_symlink(): raise PackageError("refusing symbolic-link destination")
    destination.write_bytes(_zip_bytes(payload))
    return destination

def _validate_descriptor(data: bytes) -> None:
    try: root=ElementTree.fromstring(data)
    except ElementTree.ParseError as exc: raise PackageError(f"invalid plugin XML: {exc}") from exc
    idea=root.find("idea-version"); extension=root.find("./extensions/languageBundle")
    if (root.tag!="idea-plugin" or root.findtext("id")!="org.pc-software.idea-deu" or
        root.findtext("version")!="2025.3.1.1" or idea is None or idea.attrib !=
        {"since-build":"253.29346.240","until-build":"253.29346.240"} or extension is None or
        extension.attrib.get("locale")!="de" or root.find("extensions").attrib.get("defaultExtensionNs")!="com.intellij"):
        raise PackageError("plugin descriptor identity or compatibility mismatch")

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
        folded[relative.casefold()]=relative
        result[relative]=path.read_bytes()
    return result

def _zip_bytes(entries: dict[str,bytes]) -> bytes:
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name in sorted(entries):
            info=zipfile.ZipInfo(name,_TIME); info.compress_type=zipfile.ZIP_DEFLATED
            info.create_system=3; info.external_attr=(stat.S_IFREG|0o644)<<16; info.flag_bits=0x800
            archive.writestr(info,entries[name],compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    return stream.getvalue()

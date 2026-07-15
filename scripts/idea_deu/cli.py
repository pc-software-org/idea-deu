"""Command-line workflow for the resumable language-pack pipeline."""

from __future__ import annotations

import argparse
import hashlib
import gzip
import json
import os
import shutil
import stat
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .batches import BatchError, export_next_batch, import_batch, validate_all_units, workflow_lock
from .config import ProductConfig, load_product_config
from .generator import (BlobResourceProvider, DistributionResourceProvider, GenerationError, GenerationResult,
                        generate_resources, recompute_generation)
from .models import (CollisionRecord, ExclusionRecord, Inventory, ProcessingStatus,
                     ResourceRecord, ResourceType, StaleTranslationUnit,
                     TranslationContext, TranslationUnit)
from .package import PackageError, build_plugin_package, verify_plugin_package
from .properties import PropertiesError, parse_properties
from .report import ReportSnapshot, build_report, recover_report_pair, write_report
from .scanner import ScannerError, load_scanner_config, scan_archive, translation_reference_paths
from .summary import build_summary
from .source import SourceValidationError, validate_source
from .state import StateError, read_jsonl, read_jsonl_bytes, serialize_jsonl, write_bytes_atomic, write_jsonl_atomic
from .path_safety import atomic_materialize_tree, recover_materialized_tree

DOMAIN_ERROR = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.idea_deu")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-source", "scan", "validate", "generate", "package", "report", "status"):
        commands.add_parser(name)
    next_batch = commands.add_parser("next-batch")
    next_batch.add_argument("--limit", type=int, default=100)
    imported = commands.add_parser("import-batch")
    imported.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        root = _canonical_root(args.root)
        read_only = args.command in {"status", "validate-source"}
        with workflow_lock(root, shared=read_only):
            if not read_only:
                _recover_scan(root)
                recover_report_pair(root / "reports", trusted_root=root)
            result = _dispatch(root, args)
            if not read_only:
                try:
                    _refresh_report(root)
                except Exception as exc:
                    print(f"report refresh failed: {exc}", file=sys.stderr)
                    return DOMAIN_ERROR
            return result
    except (BatchError, GenerationError, PackageError, PropertiesError, ScannerError,
            SourceValidationError, StateError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return DOMAIN_ERROR


def _canonical_root(path: Path) -> Path:
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"unsafe workflow root: {path}")
    return path.resolve(strict=True)


def _config(root: Path) -> ProductConfig:
    config = load_product_config(root / "config/product.json")
    archive = Path(config.archive)
    if archive.is_absolute() or len(archive.parts) != 1 or archive.name in {".", ".."}:
        raise ValueError("configured archive must be one relative filename under workflow root")
    current = root
    for component in archive.parts:
        current /= component
        if current.is_symlink():
            raise ValueError("configured archive path must not contain symbolic links")
    return replace(config, archive=str(root / archive))


def _dispatch(root: Path, args: argparse.Namespace) -> int:
    command = args.command
    if command == "validate-source":
        config = _config(root)
        with _opened_source(root, config) as source:
            info = validate_source(config, source)
        print(f"validated {info.version} ({info.build_number}) {info.sha256}"); return 0
    if command == "scan":
        config = _config(root)
        scanner_config = load_scanner_config(root / "config/scanner.json")
        with _opened_source(root, config) as source:
            validate_source(config, source)
            inventory = scan_archive(source, scanner_config)
            source.seek(0)
            with zipfile.ZipFile(source) as outer:
                reference_paths = translation_reference_paths(outer, scanner_config)
            old_units = _read_units(root)
            with DistributionResourceProvider(source) as source_provider:
                units = _extract_units(inventory, source_provider, old_units)
                blobs = {record.source_sha256: source_provider.read(record) for record in inventory.resources}
        stale = _stale_units(old_units, units, config.build_number)
        checkpoint = _checkpoint(root)
        if checkpoint.get("current_batch") and tuple(units) != tuple(old_units):
            raise BatchError("source inventory changed while a batch is active")
        summary = build_summary(inventory, config, scanner_config, reference_paths, len(units))
        _persist_scan(root, inventory, units, stale, blobs=blobs, checkpoint=checkpoint, summary=summary)
        print(f"scanned {len(inventory.resources)} resources, {len(units)} translation units"); return 0
    if command == "next-batch":
        path = export_next_batch(root, root / "translations/units.jsonl", limit=args.limit)
        print(path.relative_to(root) if path else "no open translation units"); return 0
    if command == "import-batch":
        batch = _confined_batch(root, args.path)
        result = import_batch(root, root / "translations/units.jsonl", batch, root / "glossary/de.json")
        print(f"imported {result.imported}: {result.reviewed} reviewed, {result.blocking} blocking"); return DOMAIN_ERROR if result.blocking else 0
    if command == "validate":
        units = _validate_all(root); print(f"validated {len(units)} translation units")
        return DOMAIN_ERROR if any(any(f.severity.value == "blocking" for f in unit.findings) for unit in units) else 0
    if command == "generate":
        result = _generate(root); print(result.root); return 0
    if command == "package":
        config = _config(root)
        generated = _trusted_generation(root, materialize=False)
        inventory, units = _read_inventory(root), _read_units(root)
        provider = BlobResourceProvider(root / "inventory/source-blobs")
        destination = root / "dist/idea-deu.zip"
        build_plugin_package(generated, inventory, units, provider, root / "plugin/META-INF/plugin.xml", destination,
                             version=config.plugin_version, since_build=config.since_build,
                             until_build=config.until_build, dedupe_identical=True, trusted_root=root)
        artifact_hash, artifact_size = _file_fingerprint(destination)
        write_jsonl_atomic(root / "dist/manifest.json", [{"schema_version": 1,
            "input_sha256": _state_input_digest(inventory, units),
            "artifact_sha256": artifact_hash, "artifact_size": artifact_size}])
        print(destination); return 0
    if command == "report":
        print(root / "reports/status.json"); return 0
    if command == "status":
        snapshot = _snapshot(root)
        print(f"Resource files: {snapshot.counts['resource_files']}")
        print(f"Translation units: {snapshot.counts['translation_units']}")
        print(f"Blocking findings: {snapshot.findings['counts']['blocking']}")
        print(f"Next: {snapshot.next_command}")
        if _recovery_markers(root): print("Recovery needed: unfinished transaction detected")
        return 0
    raise ValueError(f"unknown command: {command}")


@contextmanager
def _opened_source(root: Path, config: ProductConfig):
    root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    descriptor = -1
    try:
        descriptor = os.open(Path(config.archive).name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode): raise ValueError("configured archive must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1; yield stream
    finally:
        if descriptor >= 0: os.close(descriptor)
        os.close(root_fd)


def _read_inventory(root: Path) -> Inventory:
    resources = root / "inventory/resources.jsonl"; collisions = root / "inventory/collisions.jsonl"
    compressed = root / "inventory/exclusions.jsonl.gz"; plain = root / "inventory/exclusions.jsonl"
    if not any(path.exists() for path in (resources, collisions, compressed, plain)): return Inventory((), (), ())
    if not resources.exists() or not collisions.exists() or not (compressed.exists() or plain.exists()):
        raise StateError("incomplete inventory; recovery or scan required")
    exclusions = (read_jsonl_bytes(gzip.decompress(compressed.read_bytes()), str(compressed), ExclusionRecord)
                  if compressed.exists() else read_jsonl(plain, ExclusionRecord))
    return Inventory(tuple(read_jsonl(resources, ResourceRecord)), tuple(exclusions),
                     tuple(read_jsonl(collisions, CollisionRecord)))


def _read_units(root: Path) -> tuple[TranslationUnit, ...]:
    path = root / "translations/units.jsonl"
    return tuple(read_jsonl(path, TranslationUnit)) if path.exists() else ()


def _checkpoint(root: Path) -> dict[str, Any]:
    path = root / "translations/checkpoint.json"
    if not path.exists(): return {}
    records = read_jsonl(path, dict)
    if len(records) != 1: raise StateError("invalid checkpoint")
    return records[0]


def _extract_units(inventory: Inventory, provider: DistributionResourceProvider,
                   previous: Sequence[TranslationUnit]) -> tuple[TranslationUnit, ...]:
    old = {unit.id: unit for unit in previous}
    # Container-independent recovery. A unit id is sha256(container\0path\0key),
    # so a released IDE that renames or repackages a JAR (common between minor
    # versions) changes the id of an otherwise unchanged string and would drop
    # its translation. Recover such translations by their logical identity
    # (path, key, source hash), which does not depend on the container.
    logical: dict[tuple[str, str, str], TranslationUnit] = {}
    for unit in previous:
        if unit.status is ProcessingStatus.OPEN or not unit.target:
            continue
        logical.setdefault((unit.context.path, unit.context.key, unit.source_sha256), unit)
    units: list[TranslationUnit] = []
    for record in inventory.resources:
        data = provider.read(record)
        if hashlib.sha256(data).hexdigest() != record.source_sha256 or len(data) != record.size:
            raise ScannerError(f"source changed while extracting: {record.container}!/{record.resource_path}")
        if record.resource_type is ResourceType.PROPERTIES:
            values = parse_properties(data).values.items()
        else:
            try: values = (("", data.decode("utf-8-sig")),)
            except UnicodeDecodeError as exc: raise ScannerError(f"invalid UTF-8 resource: {record.resource_path}") from exc
        bundle = Path(record.resource_path).stem
        for key, source in values:
            context = TranslationContext(bundle, key, record.container, record.resource_path)
            identifier = hashlib.sha256(f"{record.container}\0{record.resource_path}\0{key}".encode()).hexdigest()
            source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if source == "":
                # An empty source has nothing to translate; its empty target is
                # already complete and carries no EMPTY_TARGET finding, so record
                # it as reviewed rather than surfacing a no-op translation task.
                target, status, findings = "", ProcessingStatus.TECHNICALLY_REVIEWED, ()
            else:
                prior = old.get(identifier)
                if prior is not None and prior.source == source and prior.source_sha256 == source_hash and prior.context == context:
                    inherit = prior
                else:
                    recovered = logical.get((record.resource_path, key, source_hash))
                    inherit = recovered if recovered is not None and recovered.source == source else None
                if inherit is not None:
                    target, status, findings = inherit.target, inherit.status, inherit.findings
                else:
                    target, status, findings = "", ProcessingStatus.OPEN, ()
            units.append(TranslationUnit(identifier, source, source_hash, target, context, status, findings))
    return tuple(sorted(units, key=lambda unit: unit.id))


def _stale_units(previous: Sequence[TranslationUnit], active: Sequence[TranslationUnit],
                 scan_build: str) -> tuple[StaleTranslationUnit, ...]:
    active_by_id = {unit.id: unit for unit in active}
    stale: list[StaleTranslationUnit] = []
    for unit in sorted(previous, key=lambda item: item.id):
        current = active_by_id.get(unit.id)
        reason: str | None = None
        if current is None:
            reason = "removed_from_source"
        elif current.context != unit.context:
            reason = "context_changed"
        elif current.source != unit.source or current.source_sha256 != unit.source_sha256:
            reason = "source_changed"
        if reason:
            stale.append(StaleTranslationUnit(unit.id, unit.context, unit.source_sha256,
                                              reason, scan_build))
    return tuple(stale)


def _persist_scan(root: Path, inventory: Inventory, units: Sequence[TranslationUnit],
                  stale: Sequence[StaleTranslationUnit] = (), *, blobs: dict[str, bytes] | None = None,
                  checkpoint: dict[str, Any] | None = None, summary: dict[str, Any] | None = None) -> None:
    for directory in (root / "inventory", root / "translations", root / "translations/batches"):
        directory.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint or {"schema_version": 1, "completed_sequence": 0,
        "current_sequence": None, "current_batch": None, "counts": {"exported": 0}, "unit_ids": [],
        "batch_digest": None, "next_command": "export-next-batch"}
    transaction = root / ".scan-transaction"
    if transaction.exists(): raise StateError("unfinished scan transaction requires recovery")
    transaction.mkdir(mode=0o700)
    try:
        write_jsonl_atomic(transaction / "resources.jsonl", inventory.resources)
        write_jsonl_atomic(transaction / "exclusions.jsonl", inventory.exclusions)
        write_jsonl_atomic(transaction / "collisions.jsonl", inventory.collisions)
        write_jsonl_atomic(transaction / "units.jsonl", units)
        write_jsonl_atomic(transaction / "stale-units.jsonl", stale)
        write_bytes_atomic(transaction / "summary.json",
                           (json.dumps(summary or {}, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        atomic_materialize_tree(transaction / "source-blobs", blobs or {}, trusted_root=root)
        write_jsonl_atomic(transaction / "source-manifest.jsonl", [
            {"id": record.resource_id, "sha256": record.source_sha256, "size": record.size}
            for record in inventory.resources])
        write_jsonl_atomic(transaction / "checkpoint.json", [checkpoint])
        write_jsonl_atomic(transaction / "manifest.jsonl", [{"schema_version": 1, "state": "prepared"}])
        _recover_scan(root)
    except Exception:
        if not (transaction / "manifest.jsonl").exists(): shutil.rmtree(transaction, ignore_errors=True)
        raise


def _recover_scan(root: Path) -> None:
    transaction = root / ".scan-transaction"
    if not transaction.exists(): return
    if transaction.is_symlink() or not transaction.is_dir(): raise StateError("unsafe scan transaction")
    if not (transaction / "manifest.jsonl").exists():
        shutil.rmtree(transaction)
        return
    if read_jsonl(transaction / "manifest.jsonl", dict) != [{"schema_version": 1, "state": "prepared"}]:
        raise StateError("invalid scan transaction")
    write_jsonl_atomic(root / "inventory/resources.jsonl", read_jsonl(transaction / "resources.jsonl", ResourceRecord))
    exclusions = read_jsonl(transaction / "exclusions.jsonl", ExclusionRecord)
    compressed_path = root / "inventory/exclusions.jsonl.gz"
    write_bytes_atomic(compressed_path, gzip.compress(serialize_jsonl(exclusions), mtime=0))
    plain_leftover = root / "inventory/exclusions.jsonl"
    if plain_leftover.exists() and not plain_leftover.is_symlink():
        plain_leftover.unlink()
    write_jsonl_atomic(root / "inventory/collisions.jsonl", read_jsonl(transaction / "collisions.jsonl", CollisionRecord))
    write_jsonl_atomic(root / "translations/units.jsonl", read_jsonl(transaction / "units.jsonl", TranslationUnit))
    write_jsonl_atomic(root / "inventory/stale-units.jsonl", read_jsonl(transaction / "stale-units.jsonl", StaleTranslationUnit))
    write_bytes_atomic(root / "inventory/summary.json", (transaction / "summary.json").read_bytes())
    staged_blobs = transaction / "source-blobs"
    blob_data = {path.name: path.read_bytes() for path in staged_blobs.iterdir() if path.is_file() and not path.is_symlink()}
    atomic_materialize_tree(root / "inventory/source-blobs", blob_data, trusted_root=root)
    write_jsonl_atomic(root / "inventory/source-manifest.jsonl", read_jsonl(transaction / "source-manifest.jsonl", dict))
    write_jsonl_atomic(root / "translations/checkpoint.json", read_jsonl(transaction / "checkpoint.json", dict))
    shutil.rmtree(transaction)
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _confined_batch(root: Path, supplied: Path) -> Path:
    batches = (root / "translations/batches").resolve(strict=True)
    candidate = supplied if supplied.is_absolute() else root / supplied
    if candidate.is_symlink(): raise BatchError("batch path must not be a symbolic link")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != batches or not resolved.is_file(): raise BatchError("batch path is outside translations/batches")
    return resolved


def _validate_all(root: Path) -> tuple[TranslationUnit, ...]:
    return validate_all_units(root, root / "translations/units.jsonl", root / "glossary/de.json")


def _generate(root: Path):
    return _trusted_generation(root, materialize=True)


def _trusted_generation(root: Path, *, materialize: bool) -> GenerationResult:
    inventory, units = _read_inventory(root), _read_units(root)
    provider = BlobResourceProvider(root / "inventory/source-blobs")
    if materialize:
        plugin = root / "generated/plugin"
        recover_materialized_tree(plugin, trusted_root=root)
        if plugin.exists() and _tree_matches(plugin, _expected_files(inventory, units, provider)):
            result = _generation_result(plugin, inventory, units, provider)
        else:
            result = generate_resources(inventory, units, provider, plugin, dedupe_identical=True,
                                        trusted_root=root)
        manifest = _generation_manifest(root, result)
        write_jsonl_atomic(root / "generated/manifest.json", [manifest])
        return result
    return _generation_result(root / "generated/plugin", inventory, units, provider)


def _recovery_markers(root: Path) -> tuple[Path, ...]:
    candidates = (root / "translations/.batch-txn.staging",
                  root / "translations/.batch-txn.active",
                  root / "translations/.batch-txn.committed", root / ".scan-transaction",
                  root / "reports/.report-transaction", root / "generated/.plugin.staging",
                  root / "generated/.plugin.backup")
    return tuple(path for path in candidates if path.exists())


def _generation_result(path: Path, inventory: Inventory, units: Sequence[TranslationUnit], provider: object) -> GenerationResult:
    sources = {(record.container, record.resource_path): provider.read(record) for record in inventory.resources}  # type: ignore[attr-defined]
    files = recompute_generation(inventory, units, sources, dedupe_identical=True)
    return GenerationResult(path.absolute(), inventory, tuple(units),
        tuple((container, member, data) for (container, member), data in sorted(sources.items())),
        tuple(sorted(files.items())), True)


def _expected_files(inventory: Inventory, units: Sequence[TranslationUnit], provider: object) -> dict[str, bytes]:
    return dict(_generation_result(Path("."), inventory, units, provider).files)


def _tree_matches(root: Path, expected: dict[str, bytes]) -> bool:
    if root.is_symlink() or not root.is_dir(): return False
    actual: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink(): return False
        if path.is_file(): actual[path.relative_to(root).as_posix()] = path.read_bytes()
        elif not path.is_dir(): return False
    return actual == expected


def _generation_manifest(root: Path, result: GenerationResult) -> dict[str, Any]:
    return {"schema_version": 1, "build": load_product_config(root / "config/product.json").build_number,
            "input_sha256": _state_input_digest(result.inventory, result.units),
            "outputs": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                        for name, data in result.files}}


def json_bytes(value: Any) -> bytes:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _state_input_digest(inventory: Inventory, units: Sequence[TranslationUnit]) -> str:
    envelope = {"inventory": inventory.to_dict(), "units": [unit.to_dict() for unit in units],
                "sources": [[record.container, record.resource_path, record.source_sha256]
                            for record in inventory.resources]}
    return hashlib.sha256(json_bytes(envelope)).hexdigest()


def _file_fingerprint(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size


def _verify_generation_manifest(root: Path, inventory: Inventory,
                                units: Sequence[TranslationUnit]) -> tuple[bool, dict[str, Any] | None]:
    try:
        records = read_jsonl(root / "generated/manifest.json", dict)
        if len(records) != 1: return False, None
        manifest = records[0]
        if (manifest.get("schema_version") != 1 or
            manifest.get("input_sha256") != _state_input_digest(inventory, units) or
            manifest.get("build") != load_product_config(root / "config/product.json").build_number or
            not isinstance(manifest.get("outputs"), dict)):
            return False, manifest
        plugin = root / "generated/plugin"
        if plugin.is_symlink() or not plugin.is_dir(): return False, manifest
        seen: set[str] = set()
        for path in plugin.rglob("*"):
            if path.is_symlink(): return False, manifest
            if path.is_file():
                name = path.relative_to(plugin).as_posix(); seen.add(name)
                expected = manifest["outputs"].get(name)
                digest, size = _file_fingerprint(path)
                if expected != {"sha256": digest, "size": size}: return False, manifest
            elif not path.is_dir(): return False, manifest
        return seen == set(manifest["outputs"]), manifest
    except (OSError, ValueError):
        return False, None


def _snapshot(root: Path) -> ReportSnapshot:
    inventory, units = _read_inventory(root), _read_units(root)
    product = load_product_config(root / "config/product.json")
    stale_path = root / "inventory/stale-units.jsonl"
    stale = read_jsonl(stale_path, StaleTranslationUnit) if stale_path.exists() else []
    generation_valid = False
    trusted: GenerationResult | None = None
    if units and all(unit.status is not ProcessingStatus.OPEN for unit in units):
        try:
            trusted = _trusted_generation(root, materialize=False)
            generation_valid = _tree_matches(root / "generated/plugin", dict(trusted.files))
        except (OSError, ValueError):
            generation_valid = False
    artifact = root / "dist/idea-deu.zip"
    package_valid = False
    try:
        package_valid = bool(generation_valid and trusted is not None and artifact.is_file() and
                             verify_plugin_package(artifact, trusted, root / "plugin/META-INF/plugin.xml",
                                                   version=product.plugin_version, since_build=product.since_build,
                                                   until_build=product.until_build))
    except (OSError, ValueError):
        package_valid = False
    package: dict[str, bool | str] = {"present": artifact.is_file(), "valid": package_valid,
                                      "path": "dist/idea-deu.zip"}
    if artifact.is_file():
        digest, size = _file_fingerprint(artifact); package.update({"sha256": digest, "size": str(size)})
    return build_report(inventory, units,
        source={"version": product.version, "build": product.build_number, "hash": product.sha256},
        checkpoint=_checkpoint(root),
        generation={"present": (root / "generated/plugin").is_dir(), "valid": generation_valid,
                    "path": "generated/plugin"}, package=package,
        stale_units=tuple(stale))


def _refresh_report(root: Path) -> ReportSnapshot:
    snapshot = _snapshot(root)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    write_report(snapshot, root / "reports/status.json", root / "reports/status.md", trusted_root=root)
    return snapshot

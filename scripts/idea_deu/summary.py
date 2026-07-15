"""Deterministic inventory summary (``inventory/summary.json``).

The summary is a self-describing snapshot of a scan: the source binding, the
observed resource/exclusion/collision counts, the source-blob totals, and the
coverage of the IDE's translatable resources against the translation-reference
union. It is the oracle ``tests/test_real_inventory.py`` compares the committed
inventory against.

Historically this file was regenerated out-of-band on every IDE rebind, which
made a version bump non-reproducible. ``scan`` now builds it from its own scan
data via :func:`build_summary`, so a rebind is a pure ``product.json`` +
``config.py`` change followed by ``scan``.

Coverage semantics (defined here, so the code is the source of truth):

- ``candidate_paths`` — distinct resource paths that matched a resource pattern,
  i.e. every path that became a kept resource *or* was dropped by a gate that
  only fires after the pattern check (see ``_CANDIDATE_EXCLUSION_REASONS``).
- ``reference_paths`` — size of the translation-reference union.
- ``candidate_paths_in_reference`` / ``candidate_paths_not_in_reference`` —
  candidate paths split on membership in the reference union.
- ``reference_paths_not_present_in_idea`` — reference paths with no candidate in
  the IDE, grouped by resource type in ``not_present_by_type``.
- ``suspicious_missing_candidate_paths`` — reference-listed paths that *are*
  present in the IDE but were dropped by a technical limit (size/compression/
  corruption) rather than a deliberate rule. Zero in a healthy scan.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .config import ProductConfig
from .models import ExclusionReason, Inventory
from .scanner import ScannerConfig, _matches_resource, _resource_type

SUMMARY_SCHEMA_VERSION = 1

# A path can only receive one of these reasons after passing the resource
# pattern check, so their presence proves the path was a translation candidate.
_CANDIDATE_EXCLUSION_REASONS = frozenset({
    ExclusionReason.NOT_IN_TRANSLATION_REFERENCE,
    ExclusionReason.COLLISION_NOT_SELECTED,
    ExclusionReason.RESOURCE_TOO_LARGE,
    ExclusionReason.TOTAL_RESOURCE_BYTES_EXCEEDED,
    ExclusionReason.UNSUPPORTED_COMPRESSION,
    ExclusionReason.CORRUPT_ARCHIVE,
})

# Candidate drops caused by a resource limit rather than a deliberate rule.
_TECHNICAL_DROP_REASONS = frozenset({
    ExclusionReason.RESOURCE_TOO_LARGE,
    ExclusionReason.TOTAL_RESOURCE_BYTES_EXCEEDED,
    ExclusionReason.UNSUPPORTED_COMPRESSION,
    ExclusionReason.CORRUPT_ARCHIVE,
})


def build_summary(
    inventory: Inventory,
    product: ProductConfig,
    scanner_config: ScannerConfig,
    reference_paths: frozenset[str] | None,
    translation_unit_count: int,
) -> dict:
    """Compute the full ``inventory/summary.json`` payload for a scan."""
    resources = inventory.resources
    exclusions = inventory.exclusions
    collisions = inventory.collisions
    patterns = scanner_config.resource_patterns

    unique_blobs: dict[str, int] = {}
    for record in resources:
        unique_blobs[record.source_sha256] = record.size

    candidate_paths = {record.resource_path for record in resources}
    technical_drop_paths: set[str] = set()
    for record in exclusions:
        path = record.resource_path
        if not path or not _matches_resource(path, patterns):
            continue
        if record.reason in _CANDIDATE_EXCLUSION_REASONS:
            candidate_paths.add(path)
        if record.reason in _TECHNICAL_DROP_REASONS:
            technical_drop_paths.add(path)

    reference = reference_paths if reference_paths is not None else frozenset()
    in_reference = candidate_paths & reference
    not_present = reference - candidate_paths
    not_present_by_type: Counter[str] = Counter()
    for path in not_present:
        not_present_by_type[_resource_type(path, patterns).value] += 1

    return {
        "collision_content": {
            "distinct": sum(1 for item in collisions if not item.content_identical),
            "identical": sum(1 for item in collisions if item.content_identical),
        },
        "counts": {
            "collisions": len(collisions),
            "exclusions": len(exclusions),
            "resources": len(resources),
            "source_blob_bytes": sum(unique_blobs.values()),
            "source_blobs": len(unique_blobs),
            "translation_units": translation_unit_count,
            "unresolved_collisions": sum(1 for item in collisions if item.unresolved),
        },
        "exclusion_reasons": dict(sorted(Counter(item.reason.value for item in exclusions).items())),
        "reference_coverage": {
            "candidate_paths": len(candidate_paths),
            "candidate_paths_in_reference": len(in_reference),
            "candidate_paths_not_in_reference": len(candidate_paths - reference),
            "not_present_by_type": dict(sorted(not_present_by_type.items())),
            "reference_paths": len(reference),
            "reference_paths_not_present_in_idea": len(not_present),
            "suspicious_missing_candidate_paths": len(technical_drop_paths & reference),
        },
        "resource_types": dict(sorted(Counter(item.resource_type.value for item in resources).items())),
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "source": {
            "archive": Path(product.archive).name,
            "build_number": product.build_number,
            "product_code": product.product_code,
            "sha256": product.sha256,
            "since_build": product.since_build,
            "until_build": product.until_build,
            "version": product.version,
        },
    }

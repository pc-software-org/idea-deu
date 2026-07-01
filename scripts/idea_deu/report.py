"""Deterministic machine- and human-readable workflow reports."""

from __future__ import annotations

import html
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import Inventory, ProcessingStatus, TranslationUnit


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    source: dict[str, str]
    counts: dict[str, int]
    statuses: dict[str, int]
    exclusions: dict[str, int]
    collisions: dict[str, int]
    findings: dict[str, dict[str, int]]
    batches: dict[str, int | str | None]
    generation: dict[str, bool | str]
    package: dict[str, bool | str]
    next_command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_report(
    inventory: Inventory,
    units: Sequence[TranslationUnit],
    *,
    source: Mapping[str, str] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    generation: Mapping[str, bool | str] | None = None,
    package: Mapping[str, bool | str] | None = None,
) -> ReportSnapshot:
    status_counts = Counter(unit.status.value for unit in units)
    exclusion_counts = Counter(item.reason.value for item in inventory.exclusions)
    severities: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    for unit in units:
        for finding in unit.findings:
            severities[finding.severity.value] += 1
            codes[finding.code.value] += 1
    checkpoint = checkpoint or {}
    completed = checkpoint.get("completed_sequence", 0)
    current = checkpoint.get("current_sequence")
    current_batch = checkpoint.get("current_batch")
    if current_batch:
        next_command = f"python -m scripts.idea_deu import-batch {current_batch}"
    elif any(unit.status is ProcessingStatus.OPEN for unit in units):
        next_command = "python -m scripts.idea_deu next-batch --limit 100"
    elif any(unit.status is ProcessingStatus.TRANSLATED for unit in units):
        next_command = "python -m scripts.idea_deu validate"
    elif units:
        next_command = "python -m scripts.idea_deu generate"
    else:
        next_command = "python -m scripts.idea_deu scan"
    return ReportSnapshot(
        source=dict(sorted((source or {}).items())),
        counts={"resource_files": len(inventory.resources), "translation_units": len(units)},
        statuses={status.value: status_counts[status.value] for status in ProcessingStatus},
        exclusions=dict(sorted(exclusion_counts.items())),
        collisions={"total": len(inventory.collisions), "unresolved": sum(item.unresolved for item in inventory.collisions)},
        findings={"counts": {"blocking": severities["blocking"], "warning": severities["warning"]}, "codes": dict(sorted(codes.items()))},
        batches={"last_completed": completed, "current": current, "current_batch": current_batch},
        generation=dict(generation or {"present": False, "path": "generated/plugin"}),
        package=dict(package or {"present": False, "path": "dist/idea-deu.zip"}),
        next_command=next_command,
    )


def render_json(snapshot: ReportSnapshot) -> str:
    return json.dumps(snapshot.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"


def render_markdown(snapshot: ReportSnapshot) -> str:
    data = snapshot.to_dict()
    esc = lambda value: html.escape(str(value), quote=True).replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ")
    lines = ["# idea-deu status", "", "## Source", ""]
    lines.extend(f"- {esc(key)}: `{esc(value)}`" for key, value in data["source"].items())
    lines += ["", "## Counts", "", f"- Resource files: {data['counts']['resource_files']}", f"- Translation units: {data['counts']['translation_units']}", "", "## Statuses", "", "| Status | Count |", "|---|---:|"]
    lines.extend(f"| {esc(key)} | {value} |" for key, value in data["statuses"].items())
    lines += ["", "## Exclusions", "", "| Reason | Count |", "|---|---:|"]
    lines.extend(f"| {esc(key)} | {value} |" for key, value in data["exclusions"].items())
    lines += ["", "## Findings and collisions", "", f"- Blocking findings: {data['findings']['counts']['blocking']}", f"- Warning findings: {data['findings']['counts']['warning']}", f"- Collisions: {data['collisions']['total']} ({data['collisions']['unresolved']} unresolved)", "", "| Finding code | Count |", "|---|---:|"]
    lines.extend(f"| {esc(key)} | {value} |" for key, value in data["findings"]["codes"].items())
    lines += ["", "## Workflow", "", f"- Last completed batch: {esc(data['batches']['last_completed'])}", f"- Current batch: {esc(data['batches']['current_batch'] or 'none')}", f"- Generated: {esc(data['generation'].get('present', False))} (`{esc(data['generation'].get('path', ''))}`)", f"- Packaged: {esc(data['package'].get('present', False))} (`{esc(data['package'].get('path', ''))}`)", "", "Next command:", "", f"`{esc(data['next_command'])}`", ""]
    return "\n".join(lines)


def write_report(snapshot: ReportSnapshot, json_path: Path, markdown_path: Path) -> None:
    from .path_safety import atomic_write_bytes
    atomic_write_bytes(json_path, render_json(snapshot).encode("utf-8"))
    atomic_write_bytes(markdown_path, render_markdown(snapshot).encode("utf-8"))

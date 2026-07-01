"""Deterministic machine- and human-readable workflow reports."""

from __future__ import annotations

import html
import json
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import Inventory, ProcessingStatus, StaleTranslationUnit, TranslationUnit


class WorkflowState(StrEnum):
    SCAN = "scan"
    TRANSLATE = "translate"
    VALIDATE = "validate"
    GENERATE = "generate"
    PACKAGE = "package"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class StaleUnitReport:
    count: int
    by_reason: dict[str, int]
    records: tuple[StaleTranslationUnit, ...]


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
    stale_units: StaleUnitReport
    workflow_state: WorkflowState
    next_command: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["stale_units"]["records"] = list(value["stale_units"]["records"])
        return value


def build_report(
    inventory: Inventory,
    units: Sequence[TranslationUnit],
    *,
    source: Mapping[str, str] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    generation: Mapping[str, bool | str] | None = None,
    package: Mapping[str, bool | str] | None = None,
    stale_units: Sequence[StaleTranslationUnit] = (),
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
        workflow_state = WorkflowState.TRANSLATE
        next_command = f"python -m scripts.idea_deu import-batch {current_batch}"
    elif any(unit.status is ProcessingStatus.OPEN for unit in units):
        workflow_state = WorkflowState.TRANSLATE
        next_command = "python -m scripts.idea_deu next-batch --limit 100"
    elif any(unit.status is ProcessingStatus.TRANSLATED for unit in units):
        workflow_state = WorkflowState.VALIDATE
        next_command = "python -m scripts.idea_deu validate"
    elif units and not (generation or {}).get("valid", False):
        workflow_state = WorkflowState.GENERATE
        next_command = "python -m scripts.idea_deu generate"
    elif units and not (package or {}).get("valid", False):
        workflow_state = WorkflowState.PACKAGE
        next_command = "python -m scripts.idea_deu package"
    elif units:
        workflow_state = WorkflowState.COMPLETE
        next_command = "python -m scripts.idea_deu status"
    else:
        workflow_state = WorkflowState.SCAN
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
        stale_units=StaleUnitReport(len(stale_units),
                     dict(sorted(Counter(item.reason for item in stale_units).items())),
                     tuple(sorted(stale_units, key=lambda item: (item.reason, item.id)))),
        workflow_state=workflow_state,
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
    lines += ["", "## Workflow", "", f"- Last completed batch: {esc(data['batches']['last_completed'])}", f"- Current batch: {esc(data['batches']['current_batch'] or 'none')}", f"- Stale units: {data['stale_units']['count']}"]
    lines.extend(f"  - {esc(reason)}: {count}" for reason, count in data["stale_units"]["by_reason"].items())
    lines.extend(f"  - `{esc(item['id'])}`: {esc(item['reason'])}, build={esc(item['scan_build'])}, context={esc(json.dumps(item['context'], ensure_ascii=False, sort_keys=True, separators=(',', ':')))}" for item in data["stale_units"]["records"])
    lines += [f"- Workflow state: `{esc(data['workflow_state'])}`", f"- Generated: present={esc(data['generation'].get('present', False))}, valid={esc(data['generation'].get('valid', False))} (`{esc(data['generation'].get('path', ''))}`)", f"- Package: present={esc(data['package'].get('present', False))}, valid={esc(data['package'].get('valid', False))}, sha256=`{esc(data['package'].get('sha256', 'unavailable'))}`, size={esc(data['package'].get('size', 'unavailable'))} (`{esc(data['package'].get('path', ''))}`)", "", "Next command:", "", f"`{esc(data['next_command'])}`", ""]
    return "\n".join(lines)


def write_report(snapshot: ReportSnapshot, json_path: Path, markdown_path: Path) -> None:
    from .path_safety import atomic_write_bytes
    if json_path.parent != markdown_path.parent or json_path.name != "status.json" or markdown_path.name != "status.md":
        raise ValueError("report pair must use canonical status paths")
    transaction = json_path.parent / ".report-transaction"
    if transaction.exists():
        raise ValueError("unfinished report transaction requires recovery")
    transaction.mkdir(mode=0o700)
    try:
        atomic_write_bytes(transaction / "status.json", render_json(snapshot).encode("utf-8"))
        atomic_write_bytes(transaction / "status.md", render_markdown(snapshot).encode("utf-8"))
        atomic_write_bytes(transaction / "manifest.json", b'{"schema_version":1}\n')
        recover_report_pair(json_path.parent)
    except Exception:
        if not (transaction / "manifest.json").exists(): shutil.rmtree(transaction, ignore_errors=True)
        raise


def recover_report_pair(reports: Path) -> None:
    """Roll a fully staged report pair forward; safe to repeat after a crash."""
    from .path_safety import atomic_write_bytes
    transaction = reports / ".report-transaction"
    if not transaction.exists(): return
    if transaction.is_symlink() or not transaction.is_dir(): raise ValueError("unsafe report transaction")
    if (transaction / "manifest.json").read_bytes() != b'{"schema_version":1}\n':
        raise ValueError("invalid report transaction")
    json_data = (transaction / "status.json").read_bytes()
    markdown_data = (transaction / "status.md").read_bytes()
    atomic_write_bytes(reports / "status.json", json_data)
    _report_commit_hook("between_files")
    atomic_write_bytes(reports / "status.md", markdown_data)
    shutil.rmtree(transaction)


def _report_commit_hook(_label: str) -> None:
    """Test seam for crash simulation."""

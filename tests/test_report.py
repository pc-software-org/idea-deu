import json
import tempfile
import unittest
from pathlib import Path

from scripts.idea_deu.models import (
    CollisionRecord, ExclusionReason, ExclusionRecord, Inventory, ProcessingStatus,
    ResourceRecord, ResourceType, TranslationContext, TranslationUnit,
)
from scripts.idea_deu.report import build_report, render_json, render_markdown
from scripts.idea_deu.validation import Finding, FindingCode, Severity


class ReportTests(unittest.TestCase):
    def test_snapshot_and_rendering_are_complete_deterministic_and_escaped(self):
        record = ResourceRecord("r" * 64, "lib/app.jar", "Bundle.properties", ResourceType.PROPERTIES, 8, "a" * 64)
        unit = TranslationUnit("u" * 64, "Hello", "b" * 64, "<script>",
            TranslationContext("Bundle", "hello", record.container, record.resource_path),
            ProcessingStatus.TRANSLATED,
            (Finding(FindingCode.EMPTY_TARGET, Severity.BLOCKING), Finding(FindingCode.LENGTH_RATIO, Severity.WARNING)))
        snapshot = build_report(
            Inventory((record,), (ExclusionRecord("x", "<bad>", ExclusionReason.NOT_JAR),),
                      (CollisionRecord("Bundle.properties", (record,), True, False),)),
            (unit,), source={"version": "1<2", "build_number": "3", "sha256": "c" * 64},
            checkpoint={"completed_sequence": 4, "current_sequence": 5, "current_batch": "translations/batches/5-x.jsonl"},
            generation={"present": True, "path": "generated/plugin"}, package={"present": False, "path": "dist/idea-deu.zip"})
        data = json.loads(render_json(snapshot))
        self.assertEqual(1, data["counts"]["resource_files"])
        self.assertEqual(1, data["counts"]["translation_units"])
        self.assertEqual({status.value: (1 if status is ProcessingStatus.TRANSLATED else 0) for status in ProcessingStatus}, data["statuses"])
        self.assertEqual({"not_jar": 1}, data["exclusions"])
        self.assertEqual({"total": 1, "unresolved": 0}, data["collisions"])
        self.assertEqual({"blocking": 1, "warning": 1}, data["findings"]["counts"])
        self.assertEqual({"empty_target": 1, "length_ratio": 1}, data["findings"]["codes"])
        self.assertEqual("python -m scripts.idea_deu import-batch translations/batches/5-x.jsonl", data["next_command"])
        self.assertTrue(render_json(snapshot).endswith("\n"))
        markdown = render_markdown(snapshot)
        self.assertNotIn("<script>", markdown)
        self.assertIn("1&lt;2", markdown)


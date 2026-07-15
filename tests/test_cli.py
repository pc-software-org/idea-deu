import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import hashlib

from scripts.idea_deu.cli import _extract_units, _stale_units, main
from scripts.idea_deu.models import (
    Inventory, ProcessingStatus, ResourceRecord, ResourceType,
    TranslationContext, TranslationUnit,
)
from scripts.idea_deu.state import write_jsonl_atomic


class _FakeProvider:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, record: ResourceRecord) -> bytes:
        return self._data


class ExtractUnitsCarryoverTest(unittest.TestCase):
    def test_recovers_translation_across_container_rename(self) -> None:
        data = b"k=Hello\n"
        source_hash = hashlib.sha256("Hello".encode("utf-8")).hexdigest()
        previous = (TranslationUnit(
            "old-id", "Hello", source_hash, "Hallo",
            TranslationContext("B", "k", "old-name.jar", "messages/B.properties"),
            ProcessingStatus.TECHNICALLY_REVIEWED, ()),)
        record = ResourceRecord(
            resource_id="r", container="new-name.jar", resource_path="messages/B.properties",
            resource_type=ResourceType.PROPERTIES, size=len(data),
            source_sha256=hashlib.sha256(data).hexdigest())
        inventory = Inventory((record,), (), ())

        units = _extract_units(inventory, _FakeProvider(data), previous)

        self.assertEqual(1, len(units))
        unit = units[0]
        self.assertEqual("Hallo", unit.target)  # recovered despite the JAR rename
        self.assertEqual(ProcessingStatus.TECHNICALLY_REVIEWED, unit.status)
        self.assertEqual("new-name.jar", unit.context.container)
        self.assertNotEqual("old-id", unit.id)  # id is container-derived, so it changed

    def test_changed_source_is_not_recovered(self) -> None:
        data = b"k=Hello changed\n"
        previous = (TranslationUnit(
            "old-id", "Hello", hashlib.sha256("Hello".encode("utf-8")).hexdigest(), "Hallo",
            TranslationContext("B", "k", "old-name.jar", "messages/B.properties"),
            ProcessingStatus.TECHNICALLY_REVIEWED, ()),)
        record = ResourceRecord(
            resource_id="r", container="new-name.jar", resource_path="messages/B.properties",
            resource_type=ResourceType.PROPERTIES, size=len(data),
            source_sha256=hashlib.sha256(data).hexdigest())

        units = _extract_units(Inventory((record,), (), ()), _FakeProvider(data), previous)

        self.assertEqual("", units[0].target)  # source changed -> must be retranslated
        self.assertEqual(ProcessingStatus.OPEN, units[0].status)

    def test_empty_source_unit_is_reviewed_not_open(self) -> None:
        data = b"k=\n"  # empty properties value -> empty source, nothing to translate
        record = ResourceRecord(
            resource_id="r", container="c.jar", resource_path="messages/B.properties",
            resource_type=ResourceType.PROPERTIES, size=len(data),
            source_sha256=hashlib.sha256(data).hexdigest())

        units = _extract_units(Inventory((record,), (), ()), _FakeProvider(data), ())

        self.assertEqual("", units[0].source)
        self.assertEqual("", units[0].target)
        self.assertEqual(ProcessingStatus.TECHNICALLY_REVIEWED, units[0].status)
        self.assertEqual((), units[0].findings)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("config", "inventory", "translations/batches", "reports"):
            (self.root / name).mkdir(parents=True)
        (self.root / "config/product.json").write_text(json.dumps({
            "archive":"source.zip", "version":"2026.1.4", "build_number":"261.26222.65", "product_code":"IU",
            "sha256":"a"*64, "since_build":"261.26222.65", "until_build":"261.26222.65",
            "plugin_id":"org.pc-software.idea-deu", "plugin_version":"2026.1.4"}))
        write_jsonl_atomic(self.root / "translations/units.jsonl", [TranslationUnit(
            "u"*64, "Hello", "b"*64, "", TranslationContext("B", "k", "c.jar", "B.properties"), ProcessingStatus.OPEN, ())])
        for name in ("resources", "exclusions", "collisions"):
            (self.root / f"inventory/{name}.jsonl").write_text("")

    def tearDown(self): self.temp.cleanup()

    def test_status_is_read_only_and_prints_counts_and_literal_next_command(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, main(["--root", str(self.root), "status"]))
        self.assertIn("Translation units: 1", output.getvalue())
        self.assertIn("python -m scripts.idea_deu next-batch --limit 100", output.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_usage_error_is_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, main(["--root", str(self.root), "next-batch", "--limit", "nope"]))

    def test_import_rejects_path_outside_canonical_batch_root_without_traceback(self):
        outside = self.root.parent / "outside.jsonl"; outside.write_text("{}\n")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertNotEqual(0, main(["--root", str(self.root), "import-batch", str(outside)]))
        self.assertNotIn("Traceback", error.getvalue())

    def test_validate_persists_findings_and_refreshes_reports(self):
        (self.root / "glossary").mkdir()
        (self.root / "glossary/de.json").write_text(json.dumps({"schema_version":1,"locale":"de",
            "style":{"primary":"neutral","fallback":"Sie"},"retained_terms":[],"preferred_terms":{},
            "forbidden_blanket_replacements":["x"]}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(0, main(["--root", str(self.root), "validate"]))
        unit = __import__("scripts.idea_deu.state", fromlist=["read_jsonl"]).read_jsonl(
            self.root / "translations/units.jsonl", TranslationUnit)[0]
        self.assertTrue(unit.findings)
        self.assertTrue((self.root / "reports/status.json").is_file())

    def test_removed_unit_is_stale_but_reappearing_unit_is_not(self):
        previous = __import__("scripts.idea_deu.state", fromlist=["read_jsonl"]).read_jsonl(
            self.root / "translations/units.jsonl", TranslationUnit)
        stale = _stale_units(previous, (), "253")
        self.assertEqual(previous[0].id, stale[0].id)
        self.assertEqual("253", stale[0].scan_build)
        self.assertEqual((), _stale_units(previous, previous, "253"))
        changed = replace(previous[0], source="Changed", source_sha256="c"*64)
        revision = _stale_units(previous, (changed,), "254")
        self.assertEqual("source_changed", revision[0].reason)
        moved = replace(previous[0], context=replace(previous[0].context, bundle="Other"))
        self.assertEqual("context_changed", _stale_units(previous, (moved,), "254")[0].reason)

    def test_malformed_canonical_state_is_domain_error_without_traceback(self):
        (self.root / "inventory/resources.jsonl").write_text("{bad\n")
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertNotEqual(0, main(["--root", str(self.root), "status"]))
        self.assertNotIn("Traceback", error.getvalue())

    def test_report_refresh_failure_keeps_successful_core_mutation_discoverable(self):
        error = io.StringIO()
        with mock.patch("scripts.idea_deu.cli._refresh_report", side_effect=OSError("report disk full")), \
             contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(0, main(["--root", str(self.root), "next-batch"]))
        self.assertTrue(any((self.root / "translations/batches").glob("*.jsonl")))
        self.assertIn("report refresh failed", error.getvalue()); self.assertNotIn("Traceback", error.getvalue())

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.idea_deu.cli import _stale_units, main
from scripts.idea_deu.models import ProcessingStatus, TranslationContext, TranslationUnit
from scripts.idea_deu.state import write_jsonl_atomic


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("config", "inventory", "translations/batches", "reports"):
            (self.root / name).mkdir(parents=True)
        (self.root / "config/product.json").write_text(json.dumps({
            "archive":"source.zip", "version":"2025.3.1.1", "build_number":"253.29346.240", "product_code":"IU",
            "sha256":"a"*64, "since_build":"253.29346.240", "until_build":"253.29346.240",
            "plugin_id":"org.pc-software.idea-deu", "plugin_version":"2025.3.1.1"}))
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

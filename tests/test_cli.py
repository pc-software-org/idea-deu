import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.idea_deu.cli import main
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

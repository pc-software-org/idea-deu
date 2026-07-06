import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from dataclasses import replace
from unittest import mock
from pathlib import Path

from scripts.idea_deu.cli import main
from scripts.idea_deu.models import ProcessingStatus, StaleTranslationUnit, TranslationUnit
from scripts.idea_deu.state import read_jsonl, write_jsonl_atomic
from tests.fixtures.scanner_factory import jar_bytes


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        repo = Path(__file__).resolve().parents[1]
        for directory in ("config", "glossary", "plugin/META-INF"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copy(repo / "config/scanner.json", self.root / "config/scanner.json")
        scanner = json.loads((self.root / "config/scanner.json").read_text())
        scanner["require_translation_reference"] = False
        scanner["resource_selections"] = []
        (self.root / "config/scanner.json").write_text(json.dumps(scanner))
        shutil.copy(repo / "glossary/de.json", self.root / "glossary/de.json")
        shutil.copy(repo / "plugin/META-INF/plugin.xml", self.root / "plugin/META-INF/plugin.xml")
        self.archive = self.root / "idea.zip"
        self._write_archive(b"hello=Hello\nother=Other\n", include_tip=True)

    def tearDown(self): self.temp.cleanup()

    def _write_archive(self, properties: bytes, *, include_tip: bool):
        entries = [("messages/Bundle.properties", properties)]
        if include_tip: entries.append(("tips/Welcome.html", b"<html>Tip</html>\n"))
        product = json.dumps({"version":"2026.1.3","buildNumber":"261.25134.95","productCode":"IU"})
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_STORED) as outer:
            outer.writestr("product-info.json", product); outer.writestr("lib/app.jar", jar_bytes(entries))
        config = {"archive":"idea.zip","version":"2026.1.3","build_number":"261.25134.95",
            "product_code":"IU","sha256":hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "since_build":"261.25134.95","until_build":"261.25134.95",
            "plugin_id":"org.pc-software.idea-deu","plugin_version":"2026.1.3"}
        (self.root / "config/product.json").write_text(json.dumps(config))

    def _run(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--root", str(self.root), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def _successful_mutation(self, previous_mtime, *args):
        time.sleep(.002)
        code, stdout, stderr = self._run(*args)
        self.assertEqual(0, code, (args, stderr)); self.assertEqual("", stderr)
        report = self.root / "reports/status.json"
        self.assertTrue(report.is_file()); self.assertGreater(report.stat().st_mtime_ns, previous_mtime)
        self.assertTrue((self.root / "reports/status.md").is_file())
        return report.stat().st_mtime_ns, stdout

    def _run_process(self, *args):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(repo))
        return subprocess.run([sys.executable, "-m", "scripts.idea_deu", "--root", str(self.root), *args],
                              cwd=repo, env=env, text=True, capture_output=True,
                              timeout=10, check=False)

    def _successful_process_mutation(self, previous_mtime, *args):
        time.sleep(.002)
        process = self._run_process(*args)
        self.assertEqual(0, process.returncode, (args, process.stderr))
        self.assertEqual("", process.stderr); self.assertNotIn("Traceback", process.stdout)
        report = self.root / "reports/status.json"
        self.assertTrue(report.is_file()); self.assertGreater(report.stat().st_mtime_ns, previous_mtime)
        self.assertTrue((self.root / "reports/status.md").is_file())
        return report.stat().st_mtime_ns

    def test_real_cli_command_sequence_reaches_complete_package(self):
        code, _stdout, stderr = self._run("validate-source")
        self.assertEqual((0, ""), (code, stderr))
        mtime, _ = self._successful_mutation(-1, "scan")
        units = read_jsonl(self.root / "translations/units.jsonl", TranslationUnit)
        self.assertEqual({"hello","other",""}, {unit.context.key for unit in units})
        mtime, _ = self._successful_mutation(mtime, "next-batch")
        batch = next((self.root / "translations/batches").glob("*.jsonl"))
        records = [json.loads(line) for line in batch.read_text().splitlines()]
        for record in records:
            record["target"] = {"hello":"Hallo","other":"Andere","":"<html>Tipp</html>\n"}[record["context"]["key"]]
        batch.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))+"\n" for record in records))
        mtime, _ = self._successful_mutation(mtime, "import-batch", str(batch.relative_to(self.root)))
        mtime, _ = self._successful_mutation(mtime, "validate")
        mtime, _ = self._successful_mutation(mtime, "generate")
        mtime, _ = self._successful_mutation(mtime, "package")
        mtime, _ = self._successful_mutation(mtime, "report")
        with mock.patch("scripts.idea_deu.generator.DistributionResourceProvider.read",
                        side_effect=AssertionError("status must not read source archive resources")):
            code, stdout, stderr = self._run("status")
        self.assertEqual(0, code); self.assertEqual("", stderr); self.assertIn("python -m scripts.idea_deu status", stdout)
        report = json.loads((self.root / "reports/status.json").read_text())
        self.assertEqual("complete", report["workflow_state"]); self.assertTrue(report["package"]["valid"])
        with zipfile.ZipFile(self.root / "dist/idea-deu.zip") as package:
            self.assertEqual(["idea-deu/lib/idea-deu.jar"], package.namelist())

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        process = subprocess.run([sys.executable, "-m", "scripts.idea_deu", "--root", str(self.root), "status"],
                                 text=True, capture_output=True, env=env, check=False)
        self.assertEqual(0, process.returncode); self.assertNotIn("Traceback", process.stderr)
        self.assertIn("complete", json.loads((self.root / "reports/status.json").read_text())["workflow_state"])

        blob = next((self.root / "inventory/source-blobs").iterdir()); blob_bytes = blob.read_bytes(); blob.unlink()
        code, stdout, stderr = self._run("status")
        self.assertEqual((0, ""), (code, stderr)); self.assertNotIn("Traceback", stdout)
        self.assertIn("python -m scripts.idea_deu generate", stdout)
        blob.write_bytes(blob_bytes)

        generated_file = next(path for path in (self.root / "generated/plugin").rglob("*") if path.is_file())
        generated_file.write_bytes(b"paired forged resource")
        (self.root / "generated/manifest.json").write_text('{"schema_version":1,"forged":true}\n')
        code, stdout, stderr = self._run("status")
        self.assertEqual((0, ""), (code, stderr)); self.assertIn("python -m scripts.idea_deu generate", stdout)
        self.assertEqual(0, self._run("generate")[0])
        artifact = self.root / "dist/idea-deu.zip"; encrypted = bytearray(artifact.read_bytes())
        local = encrypted.find(b"PK\x03\x04"); central = encrypted.find(b"PK\x01\x02")
        encrypted[local+6:local+8] = (1).to_bytes(2,"little")
        encrypted[central+8:central+10] = (1).to_bytes(2,"little"); artifact.write_bytes(encrypted)
        code, stdout, stderr = self._run("status")
        self.assertEqual((0, ""), (code, stderr)); self.assertNotIn("Traceback", stdout)
        self.assertIn("python -m scripts.idea_deu package", stdout)
        (self.root / "dist/idea-deu.zip").write_bytes(b"not a zip")
        (self.root / "dist/manifest.json").write_text('{"schema_version":1,"forged":true}\n')
        code, stdout, stderr = self._run("status")
        self.assertEqual((0, ""), (code, stderr)); self.assertIn("python -m scripts.idea_deu package", stdout)

    def test_every_cli_command_runs_across_real_process_boundaries(self):
        process = self._run_process("validate-source")
        self.assertEqual(0, process.returncode); self.assertEqual("", process.stderr)
        self.assertNotIn("Traceback", process.stdout)
        mtime = self._successful_process_mutation(-1, "scan")
        units = read_jsonl(self.root / "translations/units.jsonl", TranslationUnit)
        self.assertEqual({"hello", "other", ""}, {unit.context.key for unit in units})
        mtime = self._successful_process_mutation(mtime, "next-batch")
        batch = next((self.root / "translations/batches").glob("*.jsonl"))
        records = [json.loads(line) for line in batch.read_text().splitlines()]
        targets = {"hello":"Hallo", "other":"Andere", "":"<html>Tipp</html>\n"}
        for record in records: record["target"] = targets[record["context"]["key"]]
        batch.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":"))+"\n" for record in records))
        mtime = self._successful_process_mutation(mtime, "import-batch", str(batch.relative_to(self.root)))
        mtime = self._successful_process_mutation(mtime, "validate")
        mtime = self._successful_process_mutation(mtime, "generate")
        mtime = self._successful_process_mutation(mtime, "package")
        self.assertTrue((self.root / "dist/idea-deu.zip").is_file())
        mtime = self._successful_process_mutation(mtime, "report")
        process = self._run_process("status")
        self.assertEqual(0, process.returncode); self.assertEqual("", process.stderr)
        self.assertNotIn("Traceback", process.stdout); self.assertIn("python -m scripts.idea_deu status", process.stdout)
        report = json.loads((self.root / "reports/status.json").read_text())
        self.assertEqual("complete", report["workflow_state"])
        self.assertTrue(report["generation"]["valid"]); self.assertTrue(report["package"]["valid"])
        with zipfile.ZipFile(self.root / "dist/idea-deu.zip") as archive:
            self.assertEqual(["idea-deu/lib/idea-deu.jar"], archive.namelist())

    def test_cli_rescan_preserves_unchanged_and_records_changed_and_removed_revisions(self):
        self.assertEqual(0, self._run("scan")[0])
        units = read_jsonl(self.root / "translations/units.jsonl", TranslationUnit)
        reviewed = [replace(unit, target="Bewahrt", status=ProcessingStatus.TECHNICALLY_REVIEWED)
                    if unit.context.key == "other" else unit for unit in units]
        write_jsonl_atomic(self.root / "translations/units.jsonl", reviewed)
        self._write_archive(b"hello=Changed\nother=Other\n", include_tip=False)
        code, _stdout, stderr = self._run("scan")
        self.assertEqual((0, ""), (code, stderr))
        current = {unit.context.key: unit for unit in read_jsonl(self.root / "translations/units.jsonl", TranslationUnit)}
        self.assertEqual(("Bewahrt", ProcessingStatus.TECHNICALLY_REVIEWED),
                         (current["other"].target, current["other"].status))
        self.assertEqual(("", ProcessingStatus.OPEN), (current["hello"].target, current["hello"].status))
        stale = read_jsonl(self.root / "inventory/stale-units.jsonl", StaleTranslationUnit)
        self.assertEqual({"source_changed", "removed_from_source"}, {item.reason for item in stale})
        stale_ids = {item.id for item in stale}
        report_json = (self.root / "reports/status.json").read_text()
        report_md = (self.root / "reports/status.md").read_text()
        self.assertTrue(all(identifier in report_json and identifier in report_md for identifier in stale_ids))
        self.assertIn('"source_changed":1', report_json); self.assertIn("source_changed: 1", report_md)

    def test_process_like_generation_crash_is_read_only_in_status_and_recovers_on_generate(self):
        # Reuse the real command chain to obtain review-complete canonical inputs.
        self.assertEqual(0, self._run("scan")[0]); self.assertEqual(0, self._run("next-batch")[0])
        batch = next((self.root / "translations/batches").glob("*.jsonl"))
        records = [json.loads(line) for line in batch.read_text().splitlines()]
        for record in records: record["target"] = record["source"]
        batch.write_text("".join(json.dumps(record, sort_keys=True, separators=(",", ":"))+"\n" for record in records))
        self.assertEqual(0, self._run("import-batch", str(batch.relative_to(self.root)))[0])
        self.assertEqual(0, self._run("generate")[0])
        (self.root / "generated/plugin/junk").write_text("corrupt")
        with mock.patch("scripts.idea_deu.path_safety._tree_swap_hook", side_effect=SystemExit(99)):
            with self.assertRaises(SystemExit): self._run("generate")
        backup = self.root / "generated/.plugin.backup"
        self.assertTrue(backup.is_dir()); before = (backup.stat().st_ino, (backup / "junk").read_bytes())
        code, stdout, stderr = self._run("status")
        self.assertEqual((0, ""), (code, stderr)); self.assertIn("Recovery needed", stdout)
        self.assertEqual(before, (backup.stat().st_ino, (backup / "junk").read_bytes()))
        self.assertEqual(0, self._run("generate")[0])
        self.assertFalse(backup.exists()); self.assertFalse((self.root / "generated/plugin/junk").exists())

    def test_source_archive_symlink_is_rejected_without_traceback(self):
        outside = self.root.parent / f"{self.root.name}-outside.zip"
        shutil.copy(self.archive, outside); self.archive.unlink(); self.archive.symlink_to(outside)
        try:
            code, _stdout, stderr = self._run("validate-source")
            self.assertNotEqual(0, code); self.assertIn("symbolic", stderr); self.assertNotIn("Traceback", stderr)
        finally:
            outside.unlink(missing_ok=True)

    def test_scan_report_does_not_recompute_generation_while_units_are_open(self):
        with mock.patch(
            "scripts.idea_deu.generator.BlobResourceProvider.read",
            side_effect=AssertionError("open units cannot have a valid generation"),
        ):
            code, _stdout, stderr = self._run("scan")

        self.assertEqual((0, ""), (code, stderr))
        self.assertEqual("translate", json.loads((self.root / "reports/status.json").read_text())["workflow_state"])

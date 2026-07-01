import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.idea_deu.batches import (
    BatchError,
    export_next_batch,
    import_batch,
    load_glossary,
)
from scripts.idea_deu.models import ProcessingStatus, TranslationContext, TranslationUnit
from scripts.idea_deu.state import read_jsonl, write_jsonl_atomic


class BatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.units_path = self.root / "translations" / "units.jsonl"
        self.glossary_path = self.root / "glossary.json"
        self.glossary_path.write_text(json.dumps({
            "schema_version": 1,
            "locale": "de",
            "style": {"primary": "neutral", "fallback": "Sie"},
            "retained_terms": ["Git"],
            "preferred_terms": {},
            "forbidden_blanket_replacements": ["run"],
        }), encoding="utf-8")
        self.units = [self.unit("b" * 64, "Save {0}"), self.unit("a" * 64, "Open {0}")]
        write_jsonl_atomic(self.units_path, self.units)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def unit(self, identifier: str, source: str) -> TranslationUnit:
        import hashlib
        return TranslationUnit(
            id=identifier,
            source=source,
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
            target="",
            context=TranslationContext("messages", identifier[:4], "idea.jar", "x.properties"),
            status=ProcessingStatus.OPEN,
            findings=(),
        )

    def test_export_is_deterministic_bounded_and_idempotent(self) -> None:
        first = export_next_batch(self.root, self.units_path, limit=1)
        self.assertEqual((self.root / "translations/batches/1-aaaaaaaaaaaa.jsonl").resolve(), first)
        self.assertEqual(first, export_next_batch(self.root, self.units_path, limit=1))
        self.assertEqual(ProcessingStatus.OPEN, read_jsonl(self.units_path, TranslationUnit)[0].status)
        checkpoint = json.loads((self.root / "translations/checkpoint.json").read_text())
        self.assertEqual(1, checkpoint["current_sequence"])
        self.assertEqual("translations/batches/1-aaaaaaaaaaaa.jsonl", checkpoint["current_batch"])

    def test_export_validates_limit_and_empty_open_set(self) -> None:
        for limit in (True, 0, -1, 1001):
            with self.subTest(limit=limit), self.assertRaises(BatchError):
                export_next_batch(self.root, self.units_path, limit=limit)
        write_jsonl_atomic(self.units_path, [replace(unit, status=ProcessingStatus.TECHNICALLY_REVIEWED) for unit in self.units])
        self.assertIsNone(export_next_batch(self.root, self.units_path))

    def test_clean_import_reviews_and_blocking_import_stays_translated(self) -> None:
        batch = export_next_batch(self.root, self.units_path, limit=2)
        assert batch
        records = read_jsonl(batch, dict)
        records[0]["target"] = "Öffnen {0}"
        records[1]["target"] = "Kaputt"
        write_jsonl_atomic(batch, records)
        result = import_batch(self.root, self.units_path, batch, self.glossary_path)
        self.assertEqual(2, result.imported)
        units = {unit.id: unit for unit in read_jsonl(self.units_path, TranslationUnit)}
        self.assertEqual(ProcessingStatus.TECHNICALLY_REVIEWED, units["a" * 64].status)
        self.assertEqual(ProcessingStatus.TRANSLATED, units["b" * 64].status)
        self.assertTrue(units["b" * 64].findings)
        checkpoint = json.loads((self.root / "translations/checkpoint.json").read_text())
        self.assertEqual(1, checkpoint["completed_sequence"])
        self.assertIsNone(checkpoint["current_batch"])

    def test_import_rejects_tampering_and_is_transactional(self) -> None:
        batch = export_next_batch(self.root, self.units_path, limit=2)
        assert batch
        old_units = self.units_path.read_bytes()
        old_checkpoint = (self.root / "translations/checkpoint.json").read_bytes()
        records = read_jsonl(batch, dict)
        records[0]["source"] = "changed"
        write_jsonl_atomic(batch, records)
        with self.assertRaisesRegex(BatchError, "source"):
            import_batch(self.root, self.units_path, batch, self.glossary_path)
        self.assertEqual(old_units, self.units_path.read_bytes())
        self.assertEqual(old_checkpoint, (self.root / "translations/checkpoint.json").read_bytes())

    def test_import_is_bound_to_exact_ordered_exported_ids(self) -> None:
        third = self.unit("c" * 64, "Close {0}")
        write_jsonl_atomic(self.units_path, [*self.units, third])
        batch = export_next_batch(self.root, self.units_path, limit=2)
        assert batch
        records = read_jsonl(batch, dict)
        substitute = third.to_dict()
        substitute["target"] = "Schließen {0}"
        substitute["batch"] = records[1]["batch"]
        records[1] = substitute
        write_jsonl_atomic(batch, records)
        with self.assertRaisesRegex(BatchError, "unit IDs"):
            import_batch(self.root, self.units_path, batch, self.glossary_path)

    def test_checkpoint_manifest_has_ids_and_immutable_digest(self) -> None:
        batch = export_next_batch(self.root, self.units_path, limit=2)
        assert batch
        checkpoint = json.loads((self.root / "translations/checkpoint.json").read_text())
        self.assertEqual(["a" * 64, "b" * 64], checkpoint["unit_ids"])
        self.assertRegex(checkpoint["batch_digest"], r"^[0-9a-f]{64}$")
        records = read_jsonl(batch, dict)
        self.assertEqual(checkpoint["batch_digest"], records[0]["batch"]["digest"])

    def test_rejects_incoherent_active_checkpoint(self) -> None:
        export_next_batch(self.root, self.units_path, limit=2)
        checkpoint_path = self.root / "translations/checkpoint.json"
        checkpoint = json.loads(checkpoint_path.read_text())
        checkpoint["current_sequence"] = 3
        write_jsonl_atomic(checkpoint_path, [checkpoint])
        with self.assertRaisesRegex(BatchError, "checkpoint"):
            export_next_batch(self.root, self.units_path)

    def test_recovers_after_base_exception_between_transaction_writes(self) -> None:
        batch = export_next_batch(self.root, self.units_path, limit=2)
        assert batch
        records = read_jsonl(batch, dict)
        for record in records:
            record["target"] = record["source"]
        write_jsonl_atomic(batch, records)
        old_units = self.units_path.read_bytes()
        checkpoint_path = self.root / "translations/checkpoint.json"
        old_checkpoint = checkpoint_path.read_bytes()
        import scripts.idea_deu.batches as batches
        real_write = batches.write_jsonl_atomic
        state_writes = 0

        def interrupt(path: Path, values: object) -> None:
            nonlocal state_writes
            real_write(path, values)
            if path == self.units_path.resolve():
                state_writes += 1
                if state_writes == 1:
                    raise SystemExit("crash")

        with patch("scripts.idea_deu.batches.write_jsonl_atomic", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                import_batch(self.root, self.units_path, batch, self.glossary_path)
        self.assertNotEqual(old_units, self.units_path.read_bytes())
        self.assertEqual(batch, export_next_batch(self.root, self.units_path))
        self.assertEqual(old_units, self.units_path.read_bytes())
        self.assertEqual(old_checkpoint, checkpoint_path.read_bytes())
        self.assertEqual([], list((self.root / "translations").glob(".batch-transaction*")))

    def test_failed_recovery_retains_evidence_for_later_retry(self) -> None:
        batch = export_next_batch(self.root, self.units_path, limit=2)
        assert batch
        records = read_jsonl(batch, dict)
        for record in records:
            record["target"] = record["source"]
        write_jsonl_atomic(batch, records)
        import scripts.idea_deu.batches as batches
        real_write = batches.write_jsonl_atomic

        def crash_after_units(path: Path, values: object) -> None:
            real_write(path, values)
            if path == self.units_path.resolve():
                raise SystemExit("crash")

        with patch("scripts.idea_deu.batches.write_jsonl_atomic", side_effect=crash_after_units):
            with self.assertRaises(SystemExit):
                import_batch(self.root, self.units_path, batch, self.glossary_path)

        def fail_restore(path: Path, values: object) -> None:
            if path.resolve() == self.units_path.resolve():
                raise OSError("restore failed")
            real_write(path, values)

        with patch("scripts.idea_deu.batches.write_jsonl_atomic", side_effect=fail_restore):
            with self.assertRaisesRegex(OSError, "restore failed"):
                export_next_batch(self.root, self.units_path)
        evidence = list((self.root / "translations").glob(".batch-txn.*"))
        self.assertEqual(1, len(evidence))
        self.assertEqual(batch, export_next_batch(self.root, self.units_path))
        self.assertEqual([], list((self.root / "translations").glob(".batch-transaction*")))

    def test_transaction_directory_recovers_every_commit_boundary(self) -> None:
        import scripts.idea_deu.batches as batches
        for boundary, committed in (
            ("after_one_backup", False),
            ("before_active_rename", False),
            ("after_active_rename", False),
            ("between_state_writes", False),
            ("after_committed_rename", True),
            ("during_cleanup", True),
        ):
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    units_path = root / "translations/units.jsonl"
                    glossary_path = root / "glossary.json"
                    glossary_path.write_bytes(self.glossary_path.read_bytes())
                    write_jsonl_atomic(units_path, self.units)
                    batch = export_next_batch(root, units_path, limit=2)
                    assert batch
                    records = read_jsonl(batch, dict)
                    for record in records:
                        record["target"] = record["source"]
                    write_jsonl_atomic(batch, records)
                    old = units_path.read_bytes()

                    def stop(label: str) -> None:
                        if label == boundary:
                            raise SystemExit(boundary)

                    with patch("scripts.idea_deu.batches._transaction_hook", side_effect=stop):
                        with self.assertRaises(SystemExit):
                            import_batch(root, units_path, batch, glossary_path)
                    export_next_batch(root, units_path)
                    changed = units_path.read_bytes() != old
                    self.assertEqual(committed, changed)
                    self.assertEqual([], list((root / "translations").glob(".batch-txn.*")))

    def test_recovery_rejects_symlink_transaction_directory(self) -> None:
        state = self.root / "translations"
        target = self.root / "elsewhere"
        target.mkdir()
        (state / ".batch-txn.active").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(BatchError, "symbolic"):
            export_next_batch(self.root, self.units_path)

    def test_recovery_rejects_symlink_backup_and_manifest_destination(self) -> None:
        import hashlib
        state = self.root / "translations"
        active = state / ".batch-txn.active"
        active.mkdir()
        outside = self.root / "outside.jsonl"
        outside.write_text("untouched", encoding="utf-8")
        (active / "units.jsonl").symlink_to(outside)
        checkpoint = active / "checkpoint.json"
        checkpoint.write_text("{}\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "units_present": True,
            "checkpoint_present": True,
            "units_sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "destination": str(outside),
        }
        (active / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        with self.assertRaises(BatchError):
            export_next_batch(self.root, self.units_path)
        self.assertEqual("untouched", outside.read_text(encoding="utf-8"))

    def test_recovery_ignores_no_destination_from_crafted_manifest(self) -> None:
        import hashlib
        active = self.root / "translations/.batch-txn.active"
        active.mkdir()
        outside = self.root / "outside.jsonl"
        outside.write_text("untouched", encoding="utf-8")
        units = active / "units.jsonl"
        checkpoint = active / "checkpoint.json"
        units.write_bytes(self.units_path.read_bytes())
        checkpoint.write_text("{}\n", encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "units_present": True,
            "checkpoint_present": True,
            "units_sha256": hashlib.sha256(units.read_bytes()).hexdigest(),
            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "destination": str(outside),
        }
        (active / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BatchError, "manifest"):
            export_next_batch(self.root, self.units_path)
        self.assertEqual("untouched", outside.read_text(encoding="utf-8"))

    def test_paths_are_confined_to_root(self) -> None:
        outside = self.root.parent / "outside.jsonl"
        with self.assertRaises(BatchError):
            import_batch(self.root, self.units_path, outside, self.glossary_path)

    def test_rejects_symlinked_batch_directory(self) -> None:
        real = self.root / "real-batches"
        real.mkdir()
        (self.root / "translations").mkdir(exist_ok=True)
        (self.root / "translations/batches").symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(BatchError, "symbolic link"):
            export_next_batch(self.root, self.units_path)

    def test_wraps_malformed_batch_as_batch_error(self) -> None:
        batch = export_next_batch(self.root, self.units_path)
        assert batch
        batch.write_text('{"id":"x","id":"y"}\n', encoding="utf-8")
        with self.assertRaises(BatchError):
            import_batch(self.root, self.units_path, batch, self.glossary_path)

    def test_rejects_checkpoint_with_wrong_field_types(self) -> None:
        checkpoint = self.root / "translations/checkpoint.json"
        write_jsonl_atomic(checkpoint, [{
            "schema_version": 1,
            "completed_sequence": True,
            "current_sequence": None,
            "current_batch": None,
            "counts": {"exported": 0},
            "next_command": "export-next-batch",
        }])
        with self.assertRaisesRegex(BatchError, "checkpoint"):
            export_next_batch(self.root, self.units_path)

    def test_glossary_loader_is_strict_and_validation_adapter_retains_terms(self) -> None:
        glossary = load_glossary(self.glossary_path)
        self.assertEqual({"Git": ()}, glossary.validation_terms())
        self.glossary_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(BatchError, "duplicate"):
            load_glossary(self.glossary_path)


if __name__ == "__main__":
    unittest.main()

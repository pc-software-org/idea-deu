import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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

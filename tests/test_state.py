import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.idea_deu.models import ProcessingStatus, ResourceRecord, ResourceType
from scripts.idea_deu.state import StateError, read_jsonl, write_jsonl_atomic


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.jsonl"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_writes_canonical_sorted_utf8_jsonl_and_reads_models(self) -> None:
        records = [self._record("z", "Grüße"), self._record("a", "alpha")]

        write_jsonl_atomic(self.path, records)

        self.assertEqual(
            self.path.read_bytes(),
            (
                b'{"container":"alpha","processing_status":"open","resource_id":"a",'
                b'"resource_path":"a.properties","resource_type":"properties","size":1,'
                b'"source_sha256":"' + b"a" * 64 + b'"}\n'
                b'{"container":"Gr\xc3\xbc\xc3\x9fe","processing_status":"open","resource_id":"z",'
                b'"resource_path":"z.properties","resource_type":"properties","size":1,'
                b'"source_sha256":"' + b"a" * 64 + b'"}\n'
            ),
        )
        self.assertEqual(read_jsonl(self.path, ResourceRecord), sorted(records, key=lambda r: r.resource_id))

    def test_replaces_existing_file_and_removes_temp_file(self) -> None:
        self.path.write_text("old\n", encoding="utf-8")
        write_jsonl_atomic(self.path, [self._record("new", "container")])
        self.assertNotIn("old", self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_failure_preserves_old_file_and_cleans_temp(self) -> None:
        self.path.write_bytes(b"old\n")
        with patch("scripts.idea_deu.state.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(StateError, "replace failed"):
                write_jsonl_atomic(self.path, [self._record("new", "container")])
        self.assertEqual(self.path.read_bytes(), b"old\n")
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_serialization_failure_preserves_old_file(self) -> None:
        self.path.write_bytes(b"old\n")
        with patch("scripts.idea_deu.state.json.dumps", side_effect=TypeError("bad")):
            with self.assertRaisesRegex(StateError, "bad"):
                write_jsonl_atomic(self.path, [self._record("new", "container")])
        self.assertEqual(self.path.read_bytes(), b"old\n")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_file_fsync_failure_preserves_old_file_and_cleans_temp(self) -> None:
        self.path.write_bytes(b"old\n")
        with patch("scripts.idea_deu.state.os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaisesRegex(StateError, "fsync failed"):
                write_jsonl_atomic(self.path, [self._record("new", "container")])
        self.assertEqual(self.path.read_bytes(), b"old\n")
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_stream_write_failure_preserves_old_file_and_cleans_temp(self) -> None:
        self.path.write_bytes(b"old\n")
        with patch(
            "scripts.idea_deu.state._write_payload", side_effect=OSError("write failed")
        ):
            with self.assertRaisesRegex(StateError, "write failed"):
                write_jsonl_atomic(self.path, [self._record("new", "container")])
        self.assertEqual(self.path.read_bytes(), b"old\n")
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_directory_fsync_failure_restores_existing_file(self) -> None:
        self.path.write_bytes(b"old\n")

        with patch("scripts.idea_deu.state.os.fsync", side_effect=self._fail_second_fsync):
            with self.assertRaisesRegex(StateError, "directory fsync failed"):
                write_jsonl_atomic(self.path, [self._record("new", "container")])

        self.assertEqual(self.path.read_bytes(), b"old\n")
        self.assertEqual(self._state_artifacts(), [])

    def test_directory_fsync_failure_removes_new_file(self) -> None:
        with patch("scripts.idea_deu.state.os.fsync", side_effect=self._fail_second_fsync):
            with self.assertRaisesRegex(StateError, "directory fsync failed"):
                write_jsonl_atomic(self.path, [self._record("new", "container")])

        self.assertFalse(self.path.exists())
        self.assertEqual(self._state_artifacts(), [])

    def test_failed_rollback_retains_recovery_backup(self) -> None:
        self.path.write_bytes(b"old\n")
        real_replace = os.replace
        replace_calls = 0

        def fail_rollback(source: Path, destination: Path) -> None:
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("rollback replace failed")
            real_replace(source, destination)

        with (
            patch("scripts.idea_deu.state.os.fsync", side_effect=self._fail_second_fsync),
            patch("scripts.idea_deu.state.os.replace", side_effect=fail_rollback),
        ):
            with self.assertRaisesRegex(
                StateError, "rollback failed.*backup retained"
            ):
                write_jsonl_atomic(self.path, [self._record("new", "container")])

        backups = list(self.path.parent.glob(f".{self.path.name}.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"old\n")

    def test_rejects_duplicate_ids_for_write_and_read(self) -> None:
        records = [self._record("same", "one"), self._record("same", "two")]
        with self.assertRaisesRegex(StateError, "duplicate.*same"):
            write_jsonl_atomic(self.path, records)
        self.path.write_text('{"resource_id":"same"}\n{"resource_id":"same"}\n', encoding="utf-8")
        with self.assertRaisesRegex(StateError, "duplicate.*same"):
            read_jsonl(self.path, dict)

    def test_rejects_malformed_non_object_and_duplicate_json_keys(self) -> None:
        for content, message in (
            ("not-json\n", "line 1"),
            ("[]\n", "object"),
            ('{"resource_id":"a","resource_id":"b"}\n', "duplicate JSON key"),
        ):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(StateError, message):
                    read_jsonl(self.path, dict)

    def test_rejects_symlink_destination(self) -> None:
        target = self.path.parent / "target"
        target.write_bytes(b"old")
        os.symlink(target, self.path)
        with self.assertRaisesRegex(StateError, "symbolic link"):
            write_jsonl_atomic(self.path, [self._record("new", "container")])
        self.assertEqual(target.read_bytes(), b"old")

    @staticmethod
    def _record(resource_id: str, container: str) -> ResourceRecord:
        return ResourceRecord(
            resource_id=resource_id,
            container=container,
            resource_path=f"{resource_id}.properties",
            resource_type=ResourceType.PROPERTIES,
            size=1,
            source_sha256="a" * 64,
            processing_status=ProcessingStatus.OPEN,
        )

    def _fail_second_fsync(self, _descriptor: int) -> None:
        calls = getattr(self, "_fsync_calls", 0) + 1
        self._fsync_calls = calls
        if calls == 2:
            raise OSError("directory fsync failed")

    def _state_artifacts(self) -> list[Path]:
        return sorted(self.path.parent.glob(f".{self.path.name}.*"))


if __name__ == "__main__":
    unittest.main()

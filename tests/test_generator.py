import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.idea_deu.generator import GenerationError, MappingResourceProvider, generate_resources
from scripts.idea_deu.models import (CollisionRecord, Inventory, ProcessingStatus,
    ResourceRecord, ResourceType, TranslationContext, TranslationUnit)
from scripts.idea_deu.validation import Finding, FindingCode, Severity


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "resources"
        self.data = b"# keep\r\nhello = Hello\r\nother: untouched\r\n"
        self.record = self.resource("lib/app.jar", "messages/Bundle.properties", self.data)

    def tearDown(self): self.tmp.cleanup()

    def resource(self, container, path, data, kind=ResourceType.PROPERTIES):
        return ResourceRecord(hashlib.sha256(f"{container}\0{path}".encode()).hexdigest(), container,
            path, kind, len(data), hashlib.sha256(data).hexdigest())

    def unit(self, record, key, source, target, status=ProcessingStatus.TECHNICALLY_REVIEWED):
        return TranslationUnit(hashlib.sha256((record.resource_id+key).encode()).hexdigest(), source,
            hashlib.sha256(source.encode()).hexdigest(), target,
            TranslationContext("Bundle", key, record.container, record.resource_path), status, ())

    def test_properties_replaces_value_and_preserves_physical_content(self):
        inventory = Inventory((self.record,), (), ())
        generate_resources(inventory, (self.unit(self.record, "hello", "Hello", "Grüße"),),
            MappingResourceProvider({(self.record.container, self.record.resource_path): self.data}), self.out)
        self.assertEqual(b"# keep\r\nhello = Gr\xc3\xbc\xc3\x9fe\r\nother: untouched\r\n",
            (self.out / "messages/Bundle_de.properties").read_bytes())

    def test_whole_file_is_utf8_at_exact_path(self):
        data = b"<html>Hello</html>\n"
        record = self.resource("lib/app.jar", "tips/Welcome.html", data, ResourceType.TIP)
        unit = self.unit(record, "", "<html>Hello</html>\n", "<html>Grüße</html>\n")
        generate_resources(Inventory((record,), (), ()), (unit,),
            MappingResourceProvider({(record.container, record.resource_path): data}), self.out)
        self.assertEqual(unit.target.encode(), (self.out / record.resource_path).read_bytes())

    def test_rejects_incomplete_statuses_and_blockers_with_all_ids(self):
        units = [self.unit(self.record, str(i), "Hello", "Hallo", status) for i, status in enumerate(
            (ProcessingStatus.OPEN, ProcessingStatus.TRANSLATED))]
        units.append(replace(self.unit(self.record, "x", "Hello", "Hallo"), findings=(
            Finding(FindingCode.EMPTY_TARGET, Severity.BLOCKING),)))
        with self.assertRaises(GenerationError) as caught:
            generate_resources(Inventory((self.record,), (), ()), units,
                MappingResourceProvider({(self.record.container, self.record.resource_path): self.data}), self.out)
        for unit in units: self.assertIn(unit.id, str(caught.exception))

    def test_missing_unit_and_hash_mismatch_fail(self):
        provider = MappingResourceProvider({(self.record.container, self.record.resource_path): b"changed"})
        with self.assertRaisesRegex(GenerationError, "missing translation units"):
            generate_resources(Inventory((self.record,), (), ()), (), provider, self.out)
        with self.assertRaisesRegex(GenerationError, "SHA-256"):
            generate_resources(Inventory((self.record,), (), ()), (self.unit(self.record,"hello","Hello","Hallo"),), provider, self.out)

    def test_unresolved_collision_lists_every_container(self):
        other = replace(self.record, resource_id="f"*64, container="plugins/x.jar")
        collision = CollisionRecord(self.record.resource_path, (self.record, other), False)
        with self.assertRaises(GenerationError) as caught:
            generate_resources(Inventory((self.record, other), (), (collision,)), (), MappingResourceProvider({}), self.out)
        self.assertIn("lib/app.jar", str(caught.exception)); self.assertIn("plugins/x.jar", str(caught.exception))

    def test_identical_collision_requires_explicit_dedupe_policy(self):
        other = replace(self.record, resource_id="f"*64, container="plugins/x.jar")
        collision = CollisionRecord(self.record.resource_path, (self.record, other), True)
        units = (self.unit(self.record,"hello","Hello","Hallo"), self.unit(other,"hello","Hello","Hallo"))
        provider = MappingResourceProvider({(r.container,r.resource_path): self.data for r in (self.record,other)})
        with self.assertRaisesRegex(GenerationError, "collision"):
            generate_resources(Inventory((self.record,other),(),(collision,)), units, provider, self.out)
        generate_resources(Inventory((self.record,other),(),(collision,)), units, provider, self.out, dedupe_identical=True)

    def test_blocks_unsafe_and_case_fold_colliding_output_paths(self):
        unsafe = replace(self.record, resource_path="../escape.properties")
        with self.assertRaisesRegex(GenerationError, "unsafe"):
            generate_resources(Inventory((unsafe,),(),()), (), MappingResourceProvider({}), self.out)
        upper = replace(self.record, resource_id="e"*64, resource_path="messages/BUNDLE.properties")
        with self.assertRaisesRegex(GenerationError, "case-fold"):
            generate_resources(Inventory((self.record,upper),(),()), (), MappingResourceProvider({}), self.out)

    def test_duplicate_inventory_path_requires_collision_classification(self):
        other = replace(self.record, resource_id="d"*64, container="plugins/x.jar")
        with self.assertRaisesRegex(GenerationError, "missing collision classification"):
            generate_resources(Inventory((self.record, other), (), ()), (), MappingResourceProvider({}), self.out)

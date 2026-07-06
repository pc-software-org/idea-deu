import hashlib
import io
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.idea_deu.generator import (DistributionResourceProvider, GenerationError, GenerationResult,
    MappingResourceProvider, generate_resources)
from tests.fixtures.scanner_factory import jar_bytes
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

    def test_distribution_provider_reuses_open_container_for_adjacent_resources(self):
        class CountingStream(io.BytesIO):
            zero_seeks = 0
            def seek(self, offset, whence=0):
                if offset == 0 and whence == 0: self.zero_seeks += 1
                return super().seek(offset, whence)
        nested = jar_bytes([("a.properties", b"a=1"), ("b.properties", b"b=2")])
        archive = CountingStream()
        with zipfile.ZipFile(archive, "w") as outer: outer.writestr("lib/app.jar", nested)
        provider = DistributionResourceProvider(archive)
        first = self.resource("lib/app.jar", "a.properties", b"a=1")
        second = self.resource("lib/app.jar", "b.properties", b"b=2")

        self.assertEqual(b"a=1", provider.read(first))
        seeks_after_first = archive.zero_seeks
        self.assertEqual(b"b=2", provider.read(second))

        self.assertEqual(seeks_after_first, archive.zero_seeks)

    def test_properties_replaces_value_and_preserves_physical_content(self):
        inventory = Inventory((self.record,), (), ())
        result = generate_resources(inventory, (self.unit(self.record, "hello", "Hello", "Grüße"),
            self.unit(self.record, "other", "untouched", "unberührt")),
            MappingResourceProvider({(self.record.container, self.record.resource_path): self.data}), self.out)
        self.assertIsInstance(result, GenerationResult)
        self.assertEqual(b"# keep\r\nhello = Gr\xc3\xbc\xc3\x9fe\r\nother: unber\xc3\xbchrt\r\n",
            (self.out / "messages/Bundle_de.properties").read_bytes())

    def test_regeneration_atomically_repairs_nonempty_corrupt_tree(self):
        self.out.mkdir(); (self.out / "junk").write_text("stale")
        generate_resources(Inventory((self.record,),(),()), (
            self.unit(self.record,"hello","Hello","Hallo"),
            self.unit(self.record,"other","untouched","x")),
            MappingResourceProvider({(self.record.container,self.record.resource_path):self.data}), self.out)
        self.assertFalse((self.out / "junk").exists())
        self.assertTrue((self.out / "messages/Bundle_de.properties").is_file())

    def test_regeneration_failure_rolls_back_old_tree(self):
        self.out.mkdir(); old = self.out / "old"; old.write_text("preserve")
        with mock.patch("scripts.idea_deu.path_safety._tree_swap_hook", side_effect=OSError("crash")):
            with self.assertRaises(GenerationError):
                generate_resources(Inventory((self.record,),(),()), (
                    self.unit(self.record,"hello","Hello","Hallo"),
                    self.unit(self.record,"other","untouched","x")),
                    MappingResourceProvider({(self.record.container,self.record.resource_path):self.data}), self.out)
        self.assertEqual("preserve", old.read_text())
        self.assertEqual(["old"], [path.name for path in self.out.iterdir()])

    def test_properties_requires_exactly_one_unit_for_every_key(self):
        inventory = Inventory((self.record,), (), ())
        provider = MappingResourceProvider({(self.record.container, self.record.resource_path): self.data})
        hello = self.unit(self.record, "hello", "Hello", "Hallo")
        with self.assertRaises(GenerationError) as missing:
            generate_resources(inventory, (hello,), provider, self.out)
        self.assertIn("messages/Bundle.properties:other", str(missing.exception))
        extra = self.unit(self.record, "absent", "x", "y")
        with self.assertRaises(GenerationError) as surplus:
            generate_resources(inventory, (hello, self.unit(self.record,"other","untouched","x"), extra), provider, self.out)
        self.assertIn("messages/Bundle.properties:absent", str(surplus.exception))

    def test_postfix_template_html_description_is_supported(self):
        data = b"<html>Wrap</html>\n"
        record = self.resource("lib/app.jar", "postfixTemplates/X/description.html",
                               data, ResourceType.POSTFIX_TEMPLATE)
        unit = self.unit(record, "", "<html>Wrap</html>\n", "<html>Umschließen</html>\n")
        generate_resources(Inventory((record,), (), ()), (unit,),
            MappingResourceProvider({(record.container, record.resource_path): data}), self.out)
        self.assertEqual(unit.target.encode(), (self.out / record.resource_path).read_bytes())

    def test_empty_properties_bundle_without_units_is_allowed(self):
        record = self.resource("lib/app.jar", "messages/Empty.properties", b"")
        generate_resources(Inventory((record,), (), ()), (),
            MappingResourceProvider({(record.container, record.resource_path): b""}), self.out)
        self.assertEqual(b"", (self.out / "messages/Empty_de.properties").read_bytes())

    def test_whole_file_is_utf8_at_exact_path(self):
        data = b"<html>Hello</html>\n"
        record = self.resource("lib/app.jar", "tips/Welcome.html", data, ResourceType.TIP)
        unit = self.unit(record, "", "<html>Hello</html>\n", "<html>Grüße</html>\n")
        generate_resources(Inventory((record,), (), ()), (unit,),
            MappingResourceProvider({(record.container, record.resource_path): data}), self.out)
        self.assertEqual(unit.target.encode(), (self.out / record.resource_path).read_bytes())

    def test_whole_file_preserves_bom_newline_style_and_final_newline(self):
        data = b"\xef\xbb\xbf<html>\r\nHello\r\n</html>"
        record = self.resource("lib/app.jar", "tips/Welcome.html", data, ResourceType.TIP)
        unit = self.unit(record, "", "<html>\r\nHello\r\n</html>", "<html>\nGrüße\n</html>\n")
        generate_resources(Inventory((record,),(),()), (unit,), MappingResourceProvider({
            (record.container,record.resource_path):data}), self.out)
        self.assertEqual(b"\xef\xbb\xbf<html>\r\nGr\xc3\xbc\xc3\x9fe\r\n</html>",
            (self.out/record.resource_path).read_bytes())

    def test_whole_file_rejects_ambiguous_mixed_source_newlines(self):
        data=b"<html>\r\nHello\n</html>"
        record=self.resource("lib/app.jar","tips/Welcome.html",data,ResourceType.TIP)
        with self.assertRaisesRegex(GenerationError,"mixed newline"):
            generate_resources(Inventory((record,),(),()),(self.unit(record,"",data.decode(),"Hallo"),),
                MappingResourceProvider({(record.container,record.resource_path):data}),self.out)

    def test_rejects_unsupported_executable_resource(self):
        record=self.resource("lib/app.jar","X.class",b"bytecode",ResourceType.TIP)
        with self.assertRaisesRegex(GenerationError,"unsupported resource"):
            generate_resources(Inventory((record,),(),()),(),MappingResourceProvider({}),self.out)

    def test_rejects_symlinked_generated_parent_without_outside_write(self):
        outside=Path(self.tmp.name)/"outside"; outside.mkdir()
        parent=Path(self.tmp.name)/"linked"; parent.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(GenerationError,"symbolic|unsafe"):
            generate_resources(Inventory((self.record,),(),()),(
                self.unit(self.record,"hello","Hello","Hallo"), self.unit(self.record,"other","untouched","x")),
                MappingResourceProvider({(self.record.container,self.record.resource_path):self.data}),parent/"generated")
        self.assertEqual([],list(outside.iterdir()))

    def test_rejects_symlink_above_existing_generated_parent_without_outside_write(self):
        outside=Path(self.tmp.name)/"outside"; existing=outside/"existing"; existing.mkdir(parents=True)
        linked=Path(self.tmp.name)/"linked"; linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(GenerationError,"symbolic|unsafe"):
            generate_resources(Inventory((self.record,),(),()),(
                self.unit(self.record,"hello","Hello","Hallo"), self.unit(self.record,"other","untouched","x")),
                MappingResourceProvider({(self.record.container,self.record.resource_path):self.data}),
                linked/"existing"/"generated")
        self.assertEqual([],list(existing.iterdir()))

    def test_parent_swap_after_fd_open_cannot_redirect_generation(self):
        control=Path(self.tmp.name)/"control"; control.mkdir()
        detached=Path(self.tmp.name)/"detached"; outside=Path(self.tmp.name)/"outside"; outside.mkdir()
        def swap(_path, _fd):
            control.rename(detached)
            control.symlink_to(outside,target_is_directory=True)
        with mock.patch("scripts.idea_deu.path_safety._after_parent_open",side_effect=swap,create=True):
            with self.assertRaises(GenerationError):
                generate_resources(Inventory((self.record,),(),()),(
                    self.unit(self.record,"hello","Hello","Hallo"),self.unit(self.record,"other","untouched","x")),
                    MappingResourceProvider({(self.record.container,self.record.resource_path):self.data}),
                    control/"generated")
        self.assertEqual([],list(outside.iterdir()))

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
        # A whole-file (non-properties) resource must carry its one unit; a
        # keyless properties bundle is exempt (see the empty-bundle test).
        whole = self.resource("lib/app.jar", "tips/Welcome.html", b"<html>Hi</html>\n", ResourceType.TIP)
        with self.assertRaisesRegex(GenerationError, "missing translation units"):
            generate_resources(Inventory((whole,), (), ()), (),
                MappingResourceProvider({(whole.container, whole.resource_path): b"<html>Hi</html>\n"}), self.out)
        # A non-empty properties bundle with a missing key unit is still caught.
        with self.assertRaisesRegex(GenerationError, "incomplete properties units"):
            generate_resources(Inventory((self.record,), (), ()), (),
                MappingResourceProvider({(self.record.container, self.record.resource_path): self.data}), self.out)
        provider = MappingResourceProvider({(self.record.container, self.record.resource_path): b"changed"})
        with self.assertRaisesRegex(GenerationError, "SHA-256"):
            generate_resources(Inventory((self.record,), (), ()), (self.unit(self.record,"hello","Hello","Hallo"),), provider, self.out)

    def test_rejects_resource_size_and_translation_source_hash_mismatches(self):
        units=(self.unit(self.record,"hello","Hello","Hallo"),self.unit(self.record,"other","untouched","x"))
        provider=MappingResourceProvider({(self.record.container,self.record.resource_path):self.data})
        with self.assertRaisesRegex(GenerationError,"size"):
            generate_resources(Inventory((replace(self.record,size=len(self.data)+1),),(),()),units,provider,self.out)
        bad_unit=replace(units[0],source_sha256="0"*64)
        with self.assertRaisesRegex(GenerationError,"source SHA-256"):
            generate_resources(Inventory((self.record,),(),()),(bad_unit,units[1]),provider,self.out)

    def test_unresolved_collision_lists_every_container(self):
        other = replace(self.record, resource_id="f"*64, container="plugins/x.jar", source_sha256="0"*64)
        collision = CollisionRecord(self.record.resource_path, (self.record, other), False)
        with self.assertRaises(GenerationError) as caught:
            generate_resources(Inventory((self.record, other), (), (collision,)), (), MappingResourceProvider({}), self.out)
        self.assertIn("lib/app.jar", str(caught.exception)); self.assertIn("plugins/x.jar", str(caught.exception))

    def test_identical_collision_requires_explicit_dedupe_policy(self):
        other = replace(self.record, resource_id="f"*64, container="plugins/x.jar")
        collision = CollisionRecord(self.record.resource_path, (self.record, other), True)
        units = (self.unit(self.record,"hello","Hello","Hallo"),self.unit(self.record,"other","untouched","x"),
            self.unit(other,"hello","Hello","Hallo"),self.unit(other,"other","untouched","x"))
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

    def test_collision_claim_must_exactly_match_derived_group_and_hash_identity(self):
        other=replace(self.record,resource_id="f"*64,container="plugins/x.jar")
        forged_member=replace(other,source_sha256="0"*64)
        forged=CollisionRecord(self.record.resource_path,(self.record,forged_member),True,False)
        with self.assertRaisesRegex(GenerationError,"collision classification"):
            generate_resources(Inventory((self.record,other),(),(forged,)),(),MappingResourceProvider({}),self.out)

    def test_rejects_extra_collision_claim_for_unique_resource(self):
        forged=CollisionRecord(self.record.resource_path,(self.record,),True,False)
        with self.assertRaisesRegex(GenerationError,"collision classification"):
            generate_resources(Inventory((self.record,),(),(forged,)),(),MappingResourceProvider({}),self.out)

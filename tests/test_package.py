import hashlib
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from scripts.idea_deu.generator import GenerationResult, MappingResourceProvider, generate_resources
from scripts.idea_deu.models import Inventory, ProcessingStatus, ResourceRecord, ResourceType, TranslationContext, TranslationUnit
from scripts.idea_deu.package import PackageError, build_plugin_package, verify_plugin_package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.resources = self.root / "resources"
        self.descriptor = Path("plugin/META-INF/plugin.xml")
        data=b"hello=Hello\n"; container="lib/app.jar"; path="Bundle.properties"
        record=ResourceRecord("r"*64,container,path,ResourceType.PROPERTIES,len(data),hashlib.sha256(data).hexdigest())
        unit=TranslationUnit("u"*64,"Hello",hashlib.sha256(b"Hello").hexdigest(),"Grüße",
            TranslationContext("Bundle","hello",container,path),ProcessingStatus.TECHNICALLY_REVIEWED,())
        self.inventory=Inventory((record,),(),())
        self.units=(unit,)
        self.provider=MappingResourceProvider({(container,path):data})
        self.result=generate_resources(self.inventory,self.units,self.provider,self.resources)
    def tearDown(self): self.tmp.cleanup()

    VERSION = "2026.1.3.1"
    SINCE = "261"
    UNTIL = "261.*"
    CHANGE_NOTES = "<ul><li>Erster Punkt</li><li>Zweiter Punkt</li></ul>"

    def build(self,result,descriptor,destination,*,inventory=None,units=None,provider=None,
              change_notes=None):
        return build_plugin_package(
            result,
            self.inventory if inventory is None else inventory,
            self.units if units is None else units,
            self.provider if provider is None else provider,
            descriptor,
            destination,
            version=self.VERSION, since_build=self.SINCE, until_build=self.UNTIL,
            change_notes=self.CHANGE_NOTES if change_notes is None else change_notes,
        )

    def verify(self, path, result=None, descriptor=None, *, change_notes=None):
        return verify_plugin_package(
            path, self.result if result is None else result,
            self.descriptor if descriptor is None else descriptor,
            version=self.VERSION, since_build=self.SINCE, until_build=self.UNTIL,
            change_notes=self.CHANGE_NOTES if change_notes is None else change_notes,
        )

    def rendered_descriptor_root(self):
        from scripts.idea_deu.package import render_descriptor
        data = render_descriptor(self.descriptor.read_bytes(),
                                 version=self.VERSION, since_build=self.SINCE, until_build=self.UNTIL,
                                 change_notes=self.CHANGE_NOTES)
        return ElementTree.fromstring(data)

    def template_root(self):
        # The raw template (placeholders intact) so a mutated copy still renders
        # and is then rejected by descriptor structure validation, not by render.
        return ElementTree.fromstring(self.descriptor.read_bytes())

    def test_descriptor_has_exact_identity_compatibility_and_language_bundle(self):
        root = self.rendered_descriptor_root()
        self.assertEqual("org.pc-software.idea-deu", root.findtext("id"))
        idea = root.find("idea-version"); self.assertEqual("261", idea.attrib["since-build"])
        self.assertEqual("261.*", idea.attrib["until-build"])
        self.assertEqual("de", root.find("./extensions/languageBundle").attrib["locale"])

    def test_descriptor_carries_change_notes_text(self):
        root = self.rendered_descriptor_root()
        self.assertEqual(self.CHANGE_NOTES, (root.findtext("change-notes") or "").strip())

    def test_change_notes_cdata_terminator_does_not_break_descriptor(self):
        from scripts.idea_deu.package import render_descriptor
        notes = "<ul><li>danger ]]> here</li></ul>"
        data = render_descriptor(self.descriptor.read_bytes(), version=self.VERSION,
                                 since_build=self.SINCE, until_build=self.UNTIL, change_notes=notes)
        root = ElementTree.fromstring(data)  # parses only because ]]> was CDATA-escaped
        self.assertEqual(notes, (root.findtext("change-notes") or "").strip())

    def test_build_verify_roundtrip_rejects_change_notes_mismatch(self):
        built = self.root / "cn.zip"; self.build(self.result, self.descriptor, built)
        self.assertTrue(self.verify(built))
        self.assertFalse(self.verify(built, change_notes="<ul><li>Andere Notiz</li></ul>"))

    def test_empty_change_notes_rejected(self):
        with self.assertRaisesRegex(PackageError, "change-notes"):
            self.build(self.result, self.descriptor, self.root / "empty.zip", change_notes="   ")

    def test_build_is_byte_deterministic_sorted_and_has_fixed_metadata(self):
        one = self.root / "one.zip"; two = self.root / "two.zip"
        self.build(self.result, self.descriptor, one)
        self.build(self.result, self.descriptor, two)
        self.assertEqual(hashlib.sha256(one.read_bytes()).digest(), hashlib.sha256(two.read_bytes()).digest())
        with zipfile.ZipFile(one) as archive:
            self.assertEqual(sorted(archive.namelist()), archive.namelist())
            self.assertIn("idea-deu/lib/idea-deu.jar", archive.namelist())
            self.assertNotIn("idea-deu/META-INF/plugin.xml", archive.namelist())
            self.assertTrue(all(info.date_time == (1980,1,1,0,0,0) for info in archive.infolist()))
            self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in archive.infolist()))
            self.assertTrue(all((info.external_attr >> 16) == 0o100644 for info in archive.infolist()))
            with zipfile.ZipFile(archive.open("idea-deu/lib/idea-deu.jar")) as jar:
                self.assertEqual(["Bundle_de.properties", "META-INF/plugin.xml"], jar.namelist())
                self.assertTrue(all(info.date_time == (1980,1,1,0,0,0) for info in jar.infolist()))
                self.assertTrue(all(info.compress_type == zipfile.ZIP_DEFLATED for info in jar.infolist()))
                self.assertTrue(all((info.external_attr >> 16) == 0o100644 for info in jar.infolist()))
                root=ElementTree.fromstring(jar.read("META-INF/plugin.xml"))
                self.assertEqual("org.pc-software.idea-deu",root.findtext("id"))

    def test_strict_verifier_rejects_noncanonical_boundaries_comments_flags_and_compression(self):
        canonical = self.root / "canonical.zip"; self.build(self.result, self.descriptor, canonical)
        self.assertTrue(self.verify(canonical))
        for label, payload in (("prepended", b"JUNK" + canonical.read_bytes()),
                               ("appended", canonical.read_bytes() + b"JUNK")):
            path = self.root / f"{label}.zip"; path.write_bytes(payload)
            self.assertFalse(self.verify(path))
        commented = self.root / "commented.zip"; commented.write_bytes(canonical.read_bytes())
        with zipfile.ZipFile(commented, "a") as archive: archive.comment = b"comment"
        self.assertFalse(self.verify(commented))
        for label, local_offset, central_offset, value in (
            ("encrypted", 6, 8, 1), ("unsupported", 8, 10, 99)):
            content = bytearray(canonical.read_bytes())
            local = content.find(b"PK\x03\x04"); central = content.find(b"PK\x01\x02")
            offset = local_offset if label == "encrypted" else local_offset
            content[local + offset:local + offset + 2] = value.to_bytes(2,"little")
            content[central + central_offset:central + central_offset + 2] = value.to_bytes(2,"little")
            path = self.root / f"{label}.zip"; path.write_bytes(content)
            self.assertFalse(self.verify(path))

    def test_strict_verifier_rejects_local_header_mutations_and_level_one_deflate(self):
        canonical = self.root / "canonical.zip"; self.build(self.result, self.descriptor, canonical)
        original = canonical.read_bytes(); local = original.find(b"PK\x03\x04")
        mutations = {"version":(4,b"\x0a\x00"), "time":(10,b"\x01\x00"),
            "date":(12,b"\x22\x00"), "crc":(14,b"\0\0\0\0"),
            "compressed_size":(18,b"\0\0\0\0"), "size":(22,b"\0\0\0\0"),
            "name":(30,b"X")}
        for label, (offset, replacement) in mutations.items():
            content = bytearray(original); content[local+offset:local+offset+len(replacement)] = replacement
            path = self.root / f"local-{label}.zip"; path.write_bytes(content)
            self.assertFalse(self.verify(path), label)
        with zipfile.ZipFile(canonical) as archive:
            inner = archive.read("idea-deu/lib/idea-deu.jar")
        level_one = self.root / "level-one.zip"
        info=zipfile.ZipInfo("idea-deu/lib/idea-deu.jar",(1980,1,1,0,0,0)); info.compress_type=zipfile.ZIP_DEFLATED
        info.create_system=3; info.external_attr=(0o100644)<<16
        with zipfile.ZipFile(level_one,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
            archive.writestr(info,inner,compress_type=zipfile.ZIP_DEFLATED,compresslevel=1)
        self.assertFalse(self.verify(level_one))

    def test_ignores_symlink_in_materialized_resource_tree(self):
        (self.resources / "link").symlink_to(self.descriptor.resolve())
        self.build(self.result, self.descriptor, self.root/"from-evidence.zip")

    def test_rejects_invalid_or_wrong_descriptor(self):
        bad = self.root / "plugin.xml"; bad.write_text("<idea-plugin><id>x&amp;y</id></idea-plugin>")
        with self.assertRaises(PackageError): self.build(self.result, bad, self.root/"bad.zip")

    def test_rejects_descriptor_with_additional_elements_or_attributes(self):
        root = self.template_root()
        ElementTree.SubElement(root, "depends").text = "com.intellij.modules.java"
        root.find("./extensions/languageBundle").set("implementation", "Injected")
        bad = self.root / "plugin.xml"
        ElementTree.ElementTree(root).write(bad, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(PackageError, "descriptor"):
            self.build(self.result, bad, self.root/"bad.zip")

    def test_rejects_bypass_without_verified_generation_result(self):
        with self.assertRaisesRegex(PackageError,"GenerationResult"):
            self.build(self.resources,self.descriptor,self.root/"bad.zip")

    def test_rejects_modified_copy_of_generation_result(self):
        forged=replace(self.result,root=self.root,files=())
        with self.assertRaisesRegex(PackageError,"result evidence"):
            self.build(forged,self.descriptor,self.root/"bad.zip")

    def test_rejects_arbitrary_directly_constructed_result(self):
        arbitrary=self.root/"arbitrary"; arbitrary.mkdir(); (arbitrary/"evil.xml").write_bytes(b"<evil/>")
        forged=GenerationResult(arbitrary,Inventory((),(),()),(),(),(("evil.xml",b"<evil/>"),),False)
        with self.assertRaises(PackageError):
            self.build(forged,self.descriptor,self.root/"bad.zip")

    def test_rejects_forged_empty_generation_result(self):
        forged=GenerationResult(self.root,Inventory((),(),()),(),(),(),False)
        with self.assertRaises(PackageError):
            self.build(forged,self.descriptor,self.root/"bad.zip")

    def test_rejects_internally_consistent_fake_result_against_trusted_inputs(self):
        data=b"evil=Evil\n"; container="lib/evil.jar"; path="Evil.properties"
        record=ResourceRecord("e"*64,container,path,ResourceType.PROPERTIES,len(data),hashlib.sha256(data).hexdigest())
        unit=TranslationUnit("v"*64,"Evil",hashlib.sha256(b"Evil").hexdigest(),"Böse",
            TranslationContext("Evil","evil",container,path),ProcessingStatus.TECHNICALLY_REVIEWED,())
        fake=generate_resources(Inventory((record,),(),()),(unit,),MappingResourceProvider({(container,path):data}),
            self.root/"evil-resources")
        with self.assertRaisesRegex(PackageError,"trusted|evidence"):
            self.build(fake,self.descriptor,self.root/"bad.zip")

    def test_packaging_uses_evidence_not_mutable_generated_tree(self):
        (self.resources/"Bundle_de.properties").write_text("tampered")
        (self.resources/"X.class").write_bytes(b"x")
        built=self.root/"evidence.zip"
        self.build(self.result,self.descriptor,built)
        with zipfile.ZipFile(built) as archive, zipfile.ZipFile(archive.open("idea-deu/lib/idea-deu.jar")) as jar:
            self.assertEqual(b"hello=Gr\xc3\xbc\xc3\x9fe\n",jar.read("Bundle_de.properties"))
            self.assertNotIn("X.class",jar.namelist())

    def test_ignores_changed_and_executable_materialized_content(self):
        (self.resources/"Bundle_de.properties").write_text("tampered")
        (self.resources/"X.class").write_bytes(b"x")
        self.build(self.result,self.descriptor,self.root/"from-evidence.zip")

    def test_rejects_symlinked_dist_parent_without_outside_write(self):
        outside=self.root/"outside"; outside.mkdir(); linked=self.root/"linked"; linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(PackageError,"symbolic|unsafe"):
            self.build(self.result,self.descriptor,linked/"dist"/"idea-deu.zip")
        self.assertEqual([],list(outside.iterdir()))

    def test_rejects_symlink_above_existing_dist_parent_without_outside_write(self):
        outside=self.root/"outside"; existing=outside/"existing"; existing.mkdir(parents=True)
        linked=self.root/"linked"; linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(PackageError,"symbolic|unsafe"):
            self.build(self.result,self.descriptor,linked/"existing"/"dist"/"idea-deu.zip")
        self.assertEqual([],list(existing.iterdir()))

    def test_parent_swap_after_fd_open_cannot_redirect_package(self):
        control=self.root/"control"; control.mkdir(); detached=self.root/"detached"
        outside=self.root/"outside"; outside.mkdir()
        def swap(_path, _fd):
            control.rename(detached)
            control.symlink_to(outside,target_is_directory=True)
        with mock.patch("scripts.idea_deu.path_safety._after_parent_open",side_effect=swap,create=True):
            with self.assertRaises(PackageError):
                self.build(self.result,self.descriptor,control/"idea-deu.zip")
        self.assertEqual([],list(outside.iterdir()))

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
from scripts.idea_deu.package import PackageError, build_plugin_package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.resources = self.root / "resources"
        self.descriptor = Path("plugin/META-INF/plugin.xml")
        data=b"hello=Hello\n"; container="lib/app.jar"; path="Bundle.properties"
        record=ResourceRecord("r"*64,container,path,ResourceType.PROPERTIES,len(data),hashlib.sha256(data).hexdigest())
        unit=TranslationUnit("u"*64,"Hello",hashlib.sha256(b"Hello").hexdigest(),"Grüße",
            TranslationContext("Bundle","hello",container,path),ProcessingStatus.TECHNICALLY_REVIEWED,())
        self.result=generate_resources(Inventory((record,),(),()),(unit,),MappingResourceProvider({(container,path):data}),self.resources)
    def tearDown(self): self.tmp.cleanup()

    def test_descriptor_has_exact_identity_compatibility_and_language_bundle(self):
        root = ElementTree.parse(self.descriptor).getroot()
        self.assertEqual("org.pc-software.idea-deu", root.findtext("id"))
        idea = root.find("idea-version"); self.assertEqual("253.29346.240", idea.attrib["since-build"])
        self.assertEqual("253.29346.240", idea.attrib["until-build"])
        self.assertEqual("de", root.find("./extensions/languageBundle").attrib["locale"])

    def test_build_is_byte_deterministic_sorted_and_has_fixed_metadata(self):
        one = self.root / "one.zip"; two = self.root / "two.zip"
        build_plugin_package(self.result, self.descriptor, one)
        build_plugin_package(self.result, self.descriptor, two)
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

    def test_ignores_symlink_in_materialized_resource_tree(self):
        (self.resources / "link").symlink_to(self.descriptor.resolve())
        build_plugin_package(self.result, self.descriptor, self.root/"from-evidence.zip")

    def test_rejects_invalid_or_wrong_descriptor(self):
        bad = self.root / "plugin.xml"; bad.write_text("<idea-plugin><id>x&amp;y</id></idea-plugin>")
        with self.assertRaises(PackageError): build_plugin_package(self.result, bad, self.root/"bad.zip")

    def test_rejects_descriptor_with_additional_elements_or_attributes(self):
        root = ElementTree.parse(self.descriptor).getroot()
        ElementTree.SubElement(root, "depends").text = "com.intellij.modules.java"
        root.find("./extensions/languageBundle").set("implementation", "Injected")
        bad = self.root / "plugin.xml"
        ElementTree.ElementTree(root).write(bad, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(PackageError, "descriptor"):
            build_plugin_package(self.result, bad, self.root/"bad.zip")

    def test_rejects_bypass_without_verified_generation_result(self):
        with self.assertRaisesRegex(PackageError,"GenerationResult"):
            build_plugin_package(self.resources,self.descriptor,self.root/"bad.zip")

    def test_rejects_modified_copy_of_generation_result(self):
        forged=replace(self.result,root=self.root,files=())
        with self.assertRaisesRegex(PackageError,"generation evidence"):
            build_plugin_package(forged,self.descriptor,self.root/"bad.zip")

    def test_rejects_arbitrary_directly_constructed_result(self):
        arbitrary=self.root/"arbitrary"; arbitrary.mkdir(); (arbitrary/"evil.xml").write_bytes(b"<evil/>")
        forged=GenerationResult(arbitrary,Inventory((),(),()),(),(),(("evil.xml",b"<evil/>"),),False)
        with self.assertRaises(PackageError):
            build_plugin_package(forged,self.descriptor,self.root/"bad.zip")

    def test_rejects_forged_empty_generation_result(self):
        forged=GenerationResult(self.root,Inventory((),(),()),(),(),(),False)
        with self.assertRaises(PackageError):
            build_plugin_package(forged,self.descriptor,self.root/"bad.zip")

    def test_packaging_uses_evidence_not_mutable_generated_tree(self):
        (self.resources/"Bundle_de.properties").write_text("tampered")
        (self.resources/"X.class").write_bytes(b"x")
        built=self.root/"evidence.zip"
        build_plugin_package(self.result,self.descriptor,built)
        with zipfile.ZipFile(built) as archive, zipfile.ZipFile(archive.open("idea-deu/lib/idea-deu.jar")) as jar:
            self.assertEqual(b"hello=Gr\xc3\xbc\xc3\x9fe\n",jar.read("Bundle_de.properties"))
            self.assertNotIn("X.class",jar.namelist())

    def test_ignores_changed_and_executable_materialized_content(self):
        (self.resources/"Bundle_de.properties").write_text("tampered")
        (self.resources/"X.class").write_bytes(b"x")
        build_plugin_package(self.result,self.descriptor,self.root/"from-evidence.zip")

    def test_rejects_symlinked_dist_parent_without_outside_write(self):
        outside=self.root/"outside"; outside.mkdir(); linked=self.root/"linked"; linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(PackageError,"symbolic|unsafe"):
            build_plugin_package(self.result,self.descriptor,linked/"dist"/"idea-deu.zip")
        self.assertEqual([],list(outside.iterdir()))

    def test_rejects_symlink_above_existing_dist_parent_without_outside_write(self):
        outside=self.root/"outside"; existing=outside/"existing"; existing.mkdir(parents=True)
        linked=self.root/"linked"; linked.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(PackageError,"symbolic|unsafe"):
            build_plugin_package(self.result,self.descriptor,linked/"existing"/"dist"/"idea-deu.zip")
        self.assertEqual([],list(existing.iterdir()))

    def test_parent_swap_after_fd_open_cannot_redirect_package(self):
        control=self.root/"control"; control.mkdir(); detached=self.root/"detached"
        outside=self.root/"outside"; outside.mkdir()
        def swap(_path, _fd):
            control.rename(detached)
            control.symlink_to(outside,target_is_directory=True)
        with mock.patch("scripts.idea_deu.path_safety._after_parent_open",side_effect=swap,create=True):
            with self.assertRaises(PackageError):
                build_plugin_package(self.result,self.descriptor,control/"idea-deu.zip")
        self.assertEqual([],list(outside.iterdir()))

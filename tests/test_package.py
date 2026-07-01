import hashlib
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

from scripts.idea_deu.generator import MappingResourceProvider, generate_resources
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

    def test_rejects_symlink_and_unsafe_resource_tree(self):
        (self.resources / "link").symlink_to(self.descriptor.resolve())
        with self.assertRaisesRegex(PackageError, "symbolic"):
            build_plugin_package(self.result, self.descriptor, self.root/"bad.zip")

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
        with self.assertRaisesRegex(PackageError,"verified GenerationResult"):
            build_plugin_package(forged,self.descriptor,self.root/"bad.zip")

    def test_revalidates_generated_files_and_rejects_executable_content(self):
        (self.resources/"Bundle_de.properties").write_text("tampered")
        with self.assertRaisesRegex(PackageError,"changed"):
            build_plugin_package(self.result,self.descriptor,self.root/"bad.zip")
        (self.resources/"Bundle_de.properties").write_text("hello=Grüße\n")
        (self.resources/"X.class").write_bytes(b"x")
        with self.assertRaisesRegex(PackageError,"unsupported"):
            build_plugin_package(self.result,self.descriptor,self.root/"bad.zip")

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

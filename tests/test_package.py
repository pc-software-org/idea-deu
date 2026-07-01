import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from scripts.idea_deu.package import PackageError, build_plugin_package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.resources = self.root / "resources"; self.resources.mkdir()
        (self.resources / "Bundle_de.properties").write_text("hello=Grüße\n", encoding="utf-8")
        self.descriptor = Path("plugin/META-INF/plugin.xml")
    def tearDown(self): self.tmp.cleanup()

    def test_descriptor_has_exact_identity_compatibility_and_language_bundle(self):
        root = ElementTree.parse(self.descriptor).getroot()
        self.assertEqual("org.pc-software.idea-deu", root.findtext("id"))
        idea = root.find("idea-version"); self.assertEqual("253.29346.240", idea.attrib["since-build"])
        self.assertEqual("253.29346.240", idea.attrib["until-build"])
        self.assertEqual("de", root.find("./extensions/languageBundle").attrib["locale"])

    def test_build_is_byte_deterministic_sorted_and_has_fixed_metadata(self):
        one = self.root / "one.zip"; two = self.root / "two.zip"
        build_plugin_package(self.resources, self.descriptor, one)
        build_plugin_package(self.resources, self.descriptor, two)
        self.assertEqual(hashlib.sha256(one.read_bytes()).digest(), hashlib.sha256(two.read_bytes()).digest())
        with zipfile.ZipFile(one) as archive:
            self.assertEqual(sorted(archive.namelist()), archive.namelist())
            self.assertIn("idea-deu/lib/idea-deu.jar", archive.namelist())
            self.assertIn("idea-deu/META-INF/plugin.xml", archive.namelist())
            self.assertTrue(all(info.date_time == (1980,1,1,0,0,0) for info in archive.infolist()))
            with zipfile.ZipFile(archive.open("idea-deu/lib/idea-deu.jar")) as jar:
                self.assertEqual(["Bundle_de.properties"], jar.namelist())

    def test_rejects_symlink_and_unsafe_resource_tree(self):
        (self.resources / "link").symlink_to(self.descriptor.resolve())
        with self.assertRaisesRegex(PackageError, "symbolic"):
            build_plugin_package(self.resources, self.descriptor, self.root/"bad.zip")

    def test_rejects_invalid_or_wrong_descriptor(self):
        bad = self.root / "plugin.xml"; bad.write_text("<idea-plugin><id>x&amp;y</id></idea-plugin>")
        with self.assertRaises(PackageError): build_plugin_package(self.resources, bad, self.root/"bad.zip")

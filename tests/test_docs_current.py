"""Keep the user-facing docs in step with the source binding in config/product.json.

A rebind that forgets the README or docs fails here instead of going stale.
"""
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRODUCT = json.loads((ROOT / "config" / "product.json").read_text(encoding="utf-8"))


class DocsMatchProductConfigTests(unittest.TestCase):
    def assert_mentions(self, doc, *values):
        text = (ROOT / doc).read_text(encoding="utf-8")
        for value in values:
            self.assertIn(value, text, f"{doc} does not mention {value!r} from config/product.json")

    def test_readme_matches_binding(self):
        self.assert_mentions(
            "README.md",
            f"IntelliJ IDEA {PRODUCT['version']}",
            PRODUCT["build_number"],
            PRODUCT["plugin_version"],
            PRODUCT["archive"],
            PRODUCT["sha256"],
            f"`since-build = {PRODUCT['since_build']}`, `until-build = {PRODUCT['until_build']}`",
        )

    def test_acceptance_checklist_matches_binding(self):
        self.assert_mentions(
            "docs/acceptance-checklist.md", PRODUCT["version"], PRODUCT["build_number"]
        )

    def test_plugin_verification_matches_binding(self):
        self.assert_mentions(
            "docs/plugin-verification.md", PRODUCT["version"], PRODUCT["build_number"]
        )


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace

from scripts.idea_deu.config import ProductConfig
from scripts.idea_deu.models import (
    CollisionRecord,
    ExclusionReason,
    ExclusionRecord,
    Inventory,
    ResourceRecord,
    ResourceType,
)
from scripts.idea_deu.summary import build_summary


PATTERNS = ("*.properties", "tips/**/*.html")

PRODUCT = ProductConfig(
    archive="idea-2026.1.4.win.zip",
    version="2026.1.4",
    build_number="261.26222.65",
    product_code="IU",
    sha256="a" * 64,
    since_build="261",
    until_build="261.*",
    plugin_id="org.pc-software.idea-deu",
    plugin_version="2026.1.4.1",
)


def _resource(path, rtype, size, sha):
    return ResourceRecord("i" * 64, "c.jar", path, rtype, size, sha)


class BuildSummaryTest(unittest.TestCase):
    def setUp(self):
        self.resources = (
            _resource("a/Bundle.properties", ResourceType.PROPERTIES, 100, "aa"),
            _resource("b/Other.properties", ResourceType.PROPERTIES, 200, "bb"),
            _resource("tips/T.html", ResourceType.TIP, 50, "cc"),
        )
        self.exclusions = (
            ExclusionRecord("c.jar", "c/Excluded.properties", ExclusionReason.NOT_IN_TRANSLATION_REFERENCE),
            ExclusionRecord("c.jar", "d/Big.properties", ExclusionReason.RESOURCE_TOO_LARGE, "999"),
            ExclusionRecord("c.jar", "e/notmatch.txt", ExclusionReason.UNSUPPORTED_RESOURCE),
            ExclusionRecord("c.jar", "", ExclusionReason.DIRECTORY),
        )
        self.collisions = (
            CollisionRecord("p/Same.properties", (), content_identical=True, unresolved=False),
            CollisionRecord("p/Diff.properties", (), content_identical=False, unresolved=True),
        )
        self.reference = frozenset({
            "a/Bundle.properties",    # candidate (kept)        -> in reference
            "c/Excluded.properties",  # candidate (excluded)    -> in reference
            "d/Big.properties",       # candidate (tech-dropped) -> in reference + suspicious
            "z/Missing.properties",   # absent in IDE           -> not present (properties)
            "tips/Z.html",            # absent in IDE           -> not present (tip)
        })
        self.inventory = Inventory(self.resources, self.exclusions, self.collisions)

    def _summary(self):
        return build_summary(
            self.inventory, PRODUCT,
            SimpleNamespace(resource_patterns=PATTERNS),
            self.reference, translation_unit_count=7,
        )

    def test_counts_blobs_and_types(self):
        s = self._summary()
        self.assertEqual(s["counts"], {
            "collisions": 2, "exclusions": 4, "resources": 3,
            "source_blob_bytes": 350, "source_blobs": 3,
            "translation_units": 7, "unresolved_collisions": 1,
        })
        self.assertEqual(s["resource_types"], {"properties": 2, "tip": 1})
        self.assertEqual(s["exclusion_reasons"], {
            "directory": 1, "not_in_translation_reference": 1,
            "resource_too_large": 1, "unsupported_resource": 1,
        })
        self.assertEqual(s["collision_content"], {"distinct": 1, "identical": 1})

    def test_reference_coverage(self):
        s = self._summary()
        self.assertEqual(s["reference_coverage"], {
            "candidate_paths": 5,
            "candidate_paths_in_reference": 3,
            "candidate_paths_not_in_reference": 2,
            "not_present_by_type": {"properties": 1, "tip": 1},
            "reference_paths": 5,
            "reference_paths_not_present_in_idea": 2,
            "suspicious_missing_candidate_paths": 1,
        })

    def test_source_matches_product_and_schema(self):
        s = self._summary()
        self.assertEqual(s["schema_version"], 1)
        self.assertEqual(s["source"], {
            "archive": "idea-2026.1.4.win.zip", "build_number": "261.26222.65",
            "product_code": "IU", "sha256": "a" * 64,
            "since_build": "261", "until_build": "261.*", "version": "2026.1.4",
        })

    def test_no_reference_yields_zero_coverage(self):
        s = build_summary(self.inventory, PRODUCT,
                          SimpleNamespace(resource_patterns=PATTERNS), None, 7)
        cov = s["reference_coverage"]
        self.assertEqual(cov["reference_paths"], 0)
        self.assertEqual(cov["candidate_paths_in_reference"], 0)
        self.assertEqual(cov["candidate_paths"], 5)
        self.assertEqual(cov["suspicious_missing_candidate_paths"], 0)


if __name__ == "__main__":
    unittest.main()

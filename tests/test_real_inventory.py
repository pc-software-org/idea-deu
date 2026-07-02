import hashlib
import gzip
import json
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def records(name: str):
    path = ROOT / name
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


class RealInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = json.loads((ROOT / "inventory/summary.json").read_text(encoding="utf-8"))
        cls.resources = records("inventory/resources.jsonl")
        cls.collisions = records("inventory/collisions.jsonl")
        cls.units = records("translations/units.jsonl")
        cls.report = json.loads((ROOT / "reports/status.json").read_text(encoding="utf-8"))

    def test_source_binding_and_observed_counts_are_exact(self):
        config = json.loads((ROOT / "config/product.json").read_text(encoding="utf-8"))
        self.assertEqual(self.summary["source"], {key: config[key] for key in self.summary["source"]})
        self.assertEqual(self.summary["counts"]["resources"], len(self.resources))
        self.assertEqual(self.summary["counts"]["translation_units"], len(self.units))
        self.assertEqual(self.summary["counts"]["collisions"], len(self.collisions))
        self.assertEqual(self.summary["resource_types"], dict(sorted(Counter(r["resource_type"] for r in self.resources).items())))
        self.assertTrue(all(self.summary["resource_types"].values()))

    def test_ids_are_unique_stable_and_sorted(self):
        resource_ids = [r["resource_id"] for r in self.resources]
        self.assertEqual(len(resource_ids), len(set(resource_ids)))
        self.assertEqual(resource_ids, sorted(resource_ids))
        for resource in self.resources:
            expected = hashlib.sha256(f'{resource["container"]}\0{resource["resource_path"]}'.encode()).hexdigest()
            self.assertEqual(expected, resource["resource_id"])
        unit_ids = [unit["id"] for unit in self.units]
        self.assertEqual(unit_ids, sorted(unit_ids))
        self.assertEqual(len(unit_ids), len(set(unit_ids)))

    def test_units_are_open_and_have_properties_or_whole_file_shape(self):
        kinds = {(r["container"], r["resource_path"]): r["resource_type"] for r in self.resources}
        self.assertTrue(all(unit["status"] == "open" and unit["target"] == "" for unit in self.units))
        for unit in self.units:
            kind = kinds[(unit["context"]["container"], unit["context"]["path"])]
            self.assertEqual(kind != "properties", unit["context"]["key"] == "")

    def test_reports_jsonl_totals_and_exclusions_match_summary(self):
        exclusions = records("inventory/exclusions.jsonl.gz")
        self.assertEqual(self.summary["counts"]["exclusions"], len(exclusions))
        self.assertEqual(self.summary["exclusion_reasons"], dict(sorted(Counter(x["reason"] for x in exclusions).items())))
        self.assertEqual(self.report["counts"], {"resource_files": len(self.resources), "translation_units": len(self.units)})
        self.assertEqual(self.report["exclusions"], self.summary["exclusion_reasons"])
        self.assertEqual(self.report["collisions"], {
            "total": len(self.collisions),
            "unresolved": sum(c["unresolved"] for c in self.collisions),
        })

    def test_source_blobs_match_manifest_hash_and_size(self):
        manifest = records("inventory/source-manifest.jsonl")
        self.assertEqual(len(manifest), len(self.resources))
        unique = {item["sha256"]: item["size"] for item in manifest}
        self.assertEqual(self.summary["counts"]["source_blobs"], len(unique))
        self.assertEqual(self.summary["counts"]["source_blob_bytes"], sum(unique.values()))
        for digest, size in unique.items():
            data = (ROOT / "inventory/source-blobs" / digest).read_bytes()
            self.assertEqual(size, len(data))
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())

    @unittest.skipUnless((ROOT / "idea-2025.3.1.1.win.zip").is_file(), "real source archive absent")
    def test_real_archive_hash_when_available(self):
        digest = hashlib.sha256()
        with (ROOT / "idea-2025.3.1.1.win.zip").open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        self.assertEqual(self.summary["source"]["sha256"], digest.hexdigest())


if __name__ == "__main__":
    unittest.main()

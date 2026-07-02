from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from scripts.idea_deu.models import ExclusionReason
from scripts.idea_deu.scanner import ScannerError, load_scanner_config, scan_archive
from tests.fixtures.scanner_factory import (
    jar_bytes,
    with_unsupported_compression,
    write_outer_archive,
)


ROOT = Path(__file__).resolve().parents[1]


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.config = replace(load_scanner_config(ROOT / "config" / "scanner.json"),
                              require_translation_reference=False,
                              resource_selections=())

    def reference_config(self, *containers: str):
        return replace(self.config, translation_reference_containers=containers,
                       require_translation_reference=True)

    def test_translation_reference_filters_operational_properties_and_keeps_ui(self) -> None:
        reference = "plugins/localization-ja/lib/localization-ja.jar"
        source = write_outer_archive(self.directory / "idea.zip", [
            (reference, jar_bytes([("messages/App.properties", b"name=localized")])),
            ("lib/app.jar", jar_bytes([
                ("messages/App.properties", b"name=English"),
                ("META-INF/maven/x/pom.properties", b"version=1"),
            ])),
        ])

        inventory = scan_archive(source, self.reference_config(reference))

        self.assertEqual(["messages/App.properties"], [item.resource_path for item in inventory.resources])
        self.assertIn("not_in_translation_reference", [item.reason.value for item in inventory.exclusions])

    def test_translation_reference_does_not_suppress_structural_resources(self) -> None:
        reference = "plugins/localization-ja/lib/localization-ja.jar"
        source = write_outer_archive(self.directory / "idea.zip", [
            (reference, jar_bytes([("messages/App.properties", b"name=localized")])),
            ("lib/app.jar", jar_bytes([
                ("tips/Welcome.html", b"<html>Tip</html>"),
                ("postfixTemplates/ForEach/description.html", b"<html>For each</html>"),
            ])),
        ])

        inventory = scan_archive(source, self.reference_config(reference))

        self.assertEqual(
            [("postfixTemplates/ForEach/description.html", "postfix_template"),
             ("tips/Welcome.html", "tip")],
            [(item.resource_path, item.resource_type.value) for item in inventory.resources],
        )

    def test_translation_reference_uses_union_of_all_reference_containers(self) -> None:
        ja = "plugins/localization-ja/lib/localization-ja.jar"
        ko = "plugins/localization-ko/lib/localization-ko.jar"
        source = write_outer_archive(self.directory / "idea.zip", [
            (ja, jar_bytes([("messages/One.properties", b"x=1")])),
            (ko, jar_bytes([("messages/Two.properties", b"x=2")])),
            ("lib/app.jar", jar_bytes([("messages/One.properties", b"x=one"),
                                        ("messages/Two.properties", b"x=two")])),
        ])

        inventory = scan_archive(source, self.reference_config(ja, ko))

        self.assertEqual(["messages/One.properties", "messages/Two.properties"],
                         [item.resource_path for item in inventory.resources])

    def test_required_translation_reference_missing_or_corrupt_fails(self) -> None:
        reference = "plugins/localization-ja/lib/localization-ja.jar"
        for label, entries in (("missing", [("lib/app.jar", jar_bytes([]))]),
                               ("corrupt", [(reference, b"not a jar")])):
            with self.subTest(label=label):
                source = write_outer_archive(self.directory / f"{label}.zip", entries)
                with self.assertRaisesRegex(ScannerError, "translation reference"):
                    scan_archive(source, self.reference_config(reference))

    def test_inventory_classifies_resources_and_records_every_exclusion(self) -> None:
        core = jar_bytes(
            [
                ("messages/App.properties", b"hello=Hello"),
                ("inspectionDescriptions/Unused.html", b"<html>Unused</html>"),
                ("intentionDescriptions/Flip/description.html", b"<html>Flip</html>"),
                ("fileTemplates/internal/Class.html", b"class ${NAME}"),
                ("postfixTemplates/if.xml", b"<template />"),
                ("tips/Welcome.html", b"<html>Welcome</html>"),
                ("messages/App_ja.properties", b"hello=\xe3\x81\x93\xe3\x82\x93\xe3\x81\xab\xe3\x81\xa1\xe3\x81\xaf"),
                ("messages/App_en_US.properties", b"hello=Hello"),
                ("localization/fr/messages/App.properties", b"hello=Bonjour"),
                ("classes/App.class", b"\xca\xfe\xba\xbe"),
                ("docs/readme.html", b"not supported"),
                ("lib/vendor.jar", b"not recursively scanned"),
            ]
        )
        plugin = jar_bytes(
            [
                ("messages/PluginBundle.properties", b"name=Plugin"),
                ("tips/PluginTip.html", b"<html>Plugin</html>"),
            ]
        )
        source = write_outer_archive(
            self.directory / "idea.zip",
            [("IDE/lib/app.jar", core), ("IDE/plugins/demo/lib/demo.jar", plugin), ("IDE/bin/idea.sh", b"#!/bin/sh")],
        )

        inventory = scan_archive(source, self.config)

        actual = [(item.container, item.resource_path) for item in inventory.resources]
        self.assertEqual(
            actual,
            [
                ("IDE/lib/app.jar", "fileTemplates/internal/Class.html"),
                ("IDE/lib/app.jar", "inspectionDescriptions/Unused.html"),
                ("IDE/lib/app.jar", "intentionDescriptions/Flip/description.html"),
                ("IDE/lib/app.jar", "messages/App.properties"),
                ("IDE/lib/app.jar", "postfixTemplates/if.xml"),
                ("IDE/lib/app.jar", "tips/Welcome.html"),
                ("IDE/plugins/demo/lib/demo.jar", "messages/PluginBundle.properties"),
                ("IDE/plugins/demo/lib/demo.jar", "tips/PluginTip.html"),
            ],
        )
        expected_id = hashlib.sha256(b"IDE/lib/app.jar\0messages/App.properties").hexdigest()
        self.assertEqual(inventory.resources[3].resource_id, expected_id)
        self.assertEqual(
            inventory.resources[3].to_dict(),
            {
                "resource_id": expected_id,
                "container": "IDE/lib/app.jar",
                "resource_path": "messages/App.properties",
                "resource_type": "properties",
                "size": 11,
                "source_sha256": hashlib.sha256(b"hello=Hello").hexdigest(),
                "processing_status": "open",
            },
        )
        exclusions = {(item.container, item.resource_path): item.reason for item in inventory.exclusions}
        self.assertEqual(exclusions[("IDE/lib/app.jar", "messages/App_ja.properties")], ExclusionReason.LOCALIZED)
        self.assertEqual(exclusions[("IDE/lib/app.jar", "messages/App_en_US.properties")], ExclusionReason.LOCALIZED)
        self.assertEqual(exclusions[("IDE/lib/app.jar", "localization/fr/messages/App.properties")], ExclusionReason.LOCALIZED)
        self.assertEqual(exclusions[("IDE/lib/app.jar", "classes/App.class")], ExclusionReason.UNSUPPORTED_RESOURCE)
        self.assertEqual(exclusions[("IDE/lib/app.jar", "docs/readme.html")], ExclusionReason.UNSUPPORTED_RESOURCE)
        self.assertEqual(exclusions[("IDE/lib/app.jar", "lib/vendor.jar")], ExclusionReason.NESTED_ARCHIVE)
        self.assertEqual(exclusions[("<outer>", "IDE/bin/idea.sh")], ExclusionReason.NOT_JAR)

    def test_reports_collisions_and_duplicate_members_deterministically(self) -> None:
        first = jar_bytes([("messages/Same.properties", b"a=1"), ("messages/Dup.properties", b"a=1"), ("messages/Dup.properties", b"a=2")])
        second = jar_bytes([("messages/Same.properties", b"a=2")])
        source = write_outer_archive(self.directory / "idea.zip", [("z.jar", second), ("a.jar", first)])

        inventory = scan_archive(source, self.config)

        self.assertEqual([item.container for item in inventory.collisions[0].resources], ["a.jar", "z.jar"])
        self.assertEqual(inventory.collisions[0].resource_path, "messages/Same.properties")
        self.assertTrue(inventory.collisions[0].unresolved)
        self.assertFalse(inventory.collisions[0].content_identical)
        self.assertEqual(
            {item.source_sha256 for item in inventory.collisions[0].resources},
            {hashlib.sha256(b"a=1").hexdigest(), hashlib.sha256(b"a=2").hexdigest()},
        )
        self.assertIn(ExclusionReason.DUPLICATE_MEMBER, [item.reason for item in inventory.exclusions])

    def test_corrupt_oversized_and_unsupported_nested_jars_become_exclusions(self) -> None:
        small_limits = self.config.with_limits(max_nested_jar_bytes=200, max_resource_bytes=10)
        incompressible = b"".join(hashlib.sha256(str(index).encode()).digest() for index in range(20))
        oversized = jar_bytes([("messages/Big.properties", incompressible)])
        unsupported = with_unsupported_compression(jar_bytes([("messages/X.properties", b"x=1")]))
        resource_too_large = jar_bytes([("messages/Large.properties", b"01234567890")])
        source = write_outer_archive(
            self.directory / "idea.zip",
            [("bad.jar", b"not a zip"), ("huge.jar", oversized), ("unsupported.jar", unsupported), ("resource.jar", resource_too_large)],
        )

        inventory = scan_archive(source, small_limits)

        reasons = {item.container: item.reason for item in inventory.exclusions}
        self.assertEqual(reasons["bad.jar"], ExclusionReason.CORRUPT_ARCHIVE)
        self.assertEqual(reasons["huge.jar"], ExclusionReason.NESTED_JAR_TOO_LARGE)
        self.assertEqual(reasons["unsupported.jar"], ExclusionReason.UNSUPPORTED_COMPRESSION)
        self.assertEqual(reasons["resource.jar"], ExclusionReason.RESOURCE_TOO_LARGE)

    def test_traversal_names_are_excluded_and_config_is_explicit(self) -> None:
        source = write_outer_archive(self.directory / "idea.zip", [("../evil.jar", jar_bytes([("../messages/Evil.properties", b"x=y")]))])
        inventory = scan_archive(source, self.config)
        self.assertEqual([item.reason for item in inventory.exclusions], [ExclusionReason.UNSAFE_PATH])

        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(raw["limits"]["max_nested_jar_bytes"], 300_000_000)
        self.assertEqual(raw["resource_patterns"], list(self.config.resource_patterns))

    def test_resource_patterns_control_classification(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [("app.jar", jar_bytes([("messages/App.properties", b"x=y"), ("tips/Welcome.html", b"tip")]))],
        )
        tips_only = replace(self.config, resource_patterns=("tips/**/*.html",))

        inventory = scan_archive(source, tips_only)

        self.assertEqual([item.resource_path for item in inventory.resources], ["tips/Welcome.html"])

    def test_config_rejects_resource_pattern_without_resource_type(self) -> None:
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        raw["resource_patterns"] = ["docs/**/*.html"]
        path = self.directory / "unknown-pattern.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(ScannerError, r"unsupported resource pattern: docs/\*\*/\*\.html"):
            load_scanner_config(path)

    def test_direct_config_replacement_cannot_leak_resource_type_key_error(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [("app.jar", jar_bytes([("docs/Readme.html", b"<html>Read me</html>")]))],
        )
        invalid = replace(self.config, resource_patterns=("docs/**/*.html",))

        with self.assertRaisesRegex(ScannerError, r"unsupported resource pattern: docs/\*\*/\*\.html"):
            scan_archive(source, invalid)

    def test_direct_config_replacement_rejects_empty_and_malformed_patterns(self) -> None:
        source = write_outer_archive(self.directory / "idea.zip", [])

        for label, patterns in (
            ("empty", ()),
            ("none", None),
            ("unhashable element", ("*.properties", [])),
            ("string is not a pattern sequence", "*.properties"),
        ):
            with self.subTest(label=label):
                invalid = replace(self.config, resource_patterns=patterns)
                with self.assertRaisesRegex(
                    ScannerError,
                    "resource_patterns must be a non-empty sequence of non-blank strings",
                ):
                    scan_archive(source, invalid)

    def test_explicit_third_party_container_rule_excludes_before_nested_scan(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [
                ("IDE/lib/jna.jar", jar_bytes([("messages/Native.properties", b"name=Native")])),
                (
                    "IDE/plugins/demo/lib/demo.jar",
                    jar_bytes([("messages/Plugin.properties", b"name=Plugin")]),
                ),
            ],
        )

        inventory = scan_archive(source, self.config)

        self.assertEqual(
            [(item.container, item.resource_path) for item in inventory.resources],
            [("IDE/plugins/demo/lib/demo.jar", "messages/Plugin.properties")],
        )
        self.assertEqual(
            [(item.container, item.reason.value) for item in inventory.exclusions],
            [("IDE/lib/jna.jar", "third_party_container")],
        )
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        self.assertIn(
            {"glob": "**/lib/jna.jar", "reason": "third_party_container"},
            raw["container_exclusions"],
        )

    def test_explicit_non_translatable_resource_rule_excludes_only_exact_member(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [("lib/vendor.jar", jar_bytes([
                ("vendor/Binary.properties", b"\x00\xff"),
                ("messages/Ui.properties", b"name=Visible"),
            ]))],
        )
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        raw["require_translation_reference"] = False
        raw["resource_selections"] = []
        raw["resource_exclusions"] = [{
            "container": "lib/vendor.jar",
            "resource": "vendor/Binary.properties",
            "reason": "non_translatable_resource",
        }]
        path = self.directory / "scanner.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        inventory = scan_archive(source, load_scanner_config(path))

        self.assertEqual([item.resource_path for item in inventory.resources], ["messages/Ui.properties"])
        self.assertIn(
            ("vendor/Binary.properties", "non_translatable_resource"),
            [(item.resource_path, item.reason.value) for item in inventory.exclusions],
        )

    def test_explicit_already_localized_container_is_excluded(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [("plugins/localization-ja/lib/localization-ja.jar",
              jar_bytes([("messages/App.properties", "name=名前".encode())]))],
        )
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        raw["require_translation_reference"] = False
        raw["resource_selections"] = []
        raw["container_exclusions"].append({
            "glob": "plugins/localization-ja/lib/localization-ja.jar",
            "reason": "already_localized",
        })
        path = self.directory / "scanner.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        inventory = scan_archive(source, load_scanner_config(path))

        self.assertEqual((), inventory.resources)
        self.assertEqual("already_localized", inventory.exclusions[0].reason.value)

    def test_explicit_resource_selection_keeps_only_named_collision_member(self) -> None:
        source = write_outer_archive(self.directory / "idea.zip", [
            ("a.jar", jar_bytes([("messages/Same.properties", b"name=A")])),
            ("b.jar", jar_bytes([("messages/Same.properties", b"name=B")])),
        ])
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        raw["require_translation_reference"] = False
        raw["resource_selections"] = [{
            "resource": "messages/Same.properties", "container": "b.jar",
            "reason": "official_localization_selection",
        }]
        path = self.directory / "scanner.json"; path.write_text(json.dumps(raw), encoding="utf-8")

        inventory = scan_archive(source, load_scanner_config(path))

        self.assertEqual(["b.jar"], [item.container for item in inventory.resources])
        self.assertEqual((), inventory.collisions)
        self.assertIn("collision_not_selected", [item.reason.value for item in inventory.exclusions])

    def test_config_declares_cumulative_scan_budgets(self) -> None:
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))

        self.assertEqual(
            set(raw["limits"]),
            {
                "max_containers",
                "max_members",
                "max_nested_jar_bytes",
                "max_outer_members",
                "max_resource_bytes",
                "max_total_nested_jar_bytes",
                "max_total_resource_bytes",
                "spool_memory_bytes",
            },
        )

    def test_config_rejects_bool_empty_values_and_inconsistent_limits(self) -> None:
        valid = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        invalid_configs: list[tuple[str, dict[str, object]]] = []

        for label, mutate in (
            ("boolean limit", lambda data: data["limits"].__setitem__("max_members", True)),
            ("empty patterns", lambda data: data.__setitem__("resource_patterns", [])),
            ("blank pattern", lambda data: data.__setitem__("resource_patterns", [""])),
            ("empty directories", lambda data: data.__setitem__("localization_directories", [])),
            ("blank directory", lambda data: data.__setitem__("localization_directories", [" "])),
            (
                "blank container glob",
                lambda data: data.__setitem__(
                    "container_exclusions",
                    [{"glob": "", "reason": "third_party_container"}],
                ),
            ),
            (
                "more containers than outer members",
                lambda data: data["limits"].__setitem__(
                    "max_containers", data["limits"]["max_outer_members"] + 1
                ),
            ),
            (
                "single jar exceeds total",
                lambda data: data["limits"].__setitem__(
                    "max_nested_jar_bytes", data["limits"]["max_total_nested_jar_bytes"] + 1
                ),
            ),
            (
                "single resource exceeds total",
                lambda data: data["limits"].__setitem__(
                    "max_resource_bytes", data["limits"]["max_total_resource_bytes"] + 1
                ),
            ),
            (
                "spool exceeds jar",
                lambda data: data["limits"].__setitem__(
                    "spool_memory_bytes", data["limits"]["max_nested_jar_bytes"] + 1
                ),
            ),
        ):
            candidate = deepcopy(valid)
            mutate(candidate)
            invalid_configs.append((label, candidate))

        for index, (label, candidate) in enumerate(invalid_configs):
            with self.subTest(label=label):
                path = self.directory / f"invalid-{index}.json"
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(ScannerError):
                    load_scanner_config(path)

    def test_config_wraps_json_and_file_errors(self) -> None:
        malformed = self.directory / "malformed.json"
        malformed.write_text("{not-json", encoding="utf-8")

        for path in (malformed, self.directory / "missing.json"):
            with self.subTest(path=path.name):
                with self.assertRaises(ScannerError) as raised:
                    load_scanner_config(path)
                self.assertIsNotNone(raised.exception.__cause__)

    def test_container_and_total_nested_jar_budgets_skip_each_excess_container(self) -> None:
        jars = {
            name: jar_bytes([(f"messages/{name.upper()}.properties", b"x=y")])
            for name in ("a", "b", "c", "d")
        }
        source = write_outer_archive(
            self.directory / "idea.zip",
            [(f"{name}.jar", content) for name, content in reversed(jars.items())],
        )
        limits = replace(
            self.config,
            max_containers=3,
            max_total_nested_jar_bytes=len(jars["a"]) + len(jars["b"]),
        )

        inventory = scan_archive(source, limits)

        self.assertEqual([item.container for item in inventory.resources], ["a.jar", "b.jar"])
        reasons = {(item.container, item.resource_path): item.reason.value for item in inventory.exclusions}
        self.assertEqual(reasons[("c.jar", "")], "total_nested_jar_bytes_exceeded")
        self.assertEqual(reasons[("d.jar", "")], "container_budget_exceeded")

    def test_outer_member_budget_aborts_before_sorting_or_exclusion_records(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [
                ("z.txt", b"z"),
                ("directory/", b""),
                ("c.txt", b"c"),
                ("b.txt", b"b"),
                ("a.txt", b"a"),
            ],
        )
        limits = replace(self.config, max_outer_members=4)

        with self.assertRaisesRegex(ScannerError, "outer archive member budget exceeded: 5 > 4"):
            scan_archive(source, limits)

    def test_member_budget_skips_whole_container_and_can_continue(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [
                ("b.jar", jar_bytes([("messages/B1.properties", b"x=1"), ("messages/B2.properties", b"x=2")])),
                ("c.jar", jar_bytes([("messages/C.properties", b"x=3")])),
                ("a.jar", jar_bytes([("messages/A1.properties", b"x=1"), ("messages/A2.properties", b"x=2")])),
            ],
        )
        limits = replace(self.config, max_members=3)

        inventory = scan_archive(source, limits)

        self.assertEqual(
            [(item.container, item.resource_path) for item in inventory.resources],
            [
                ("a.jar", "messages/A1.properties"),
                ("a.jar", "messages/A2.properties"),
                ("c.jar", "messages/C.properties"),
            ],
        )
        self.assertEqual(
            [(item.container, item.resource_path, item.reason.value) for item in inventory.exclusions],
            [("b.jar", "", "member_budget_exceeded")],
        )

    def test_total_resource_budget_excludes_each_candidate_that_does_not_fit(self) -> None:
        source = write_outer_archive(
            self.directory / "idea.zip",
            [
                (
                    "app.jar",
                    jar_bytes(
                        [
                            ("messages/C.properties", b"cccc"),
                            ("messages/A.properties", b"aaaa"),
                            ("messages/B.properties", b"bbbb"),
                        ]
                    ),
                )
            ],
        )
        limits = replace(self.config, max_total_resource_bytes=8)

        inventory = scan_archive(source, limits)

        self.assertEqual(
            [item.resource_path for item in inventory.resources],
            ["messages/A.properties", "messages/B.properties"],
        )
        self.assertEqual(
            [(item.resource_path, item.reason.value) for item in inventory.exclusions],
            [("messages/C.properties", "total_resource_bytes_exceeded")],
        )

    def test_resource_selection_for_missing_resource_fails_closed(self) -> None:
        source = write_outer_archive(self.directory / "idea.zip", [
            ("a.jar", jar_bytes([("messages/Real.properties", b"x=1")])),
        ])
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        raw["require_translation_reference"] = False
        raw["resource_selections"] = [{
            "resource": "messages/DoesNotExist.properties", "container": "a.jar",
            "reason": "official_localization_selection",
        }]
        path = self.directory / "scanner.json"; path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(ScannerError, "resource selection resource not present"):
            scan_archive(source, load_scanner_config(path))

    def test_config_requires_schema_version_field(self) -> None:
        raw = json.loads((ROOT / "config" / "scanner.json").read_text(encoding="utf-8"))
        self.assertEqual(raw.get("schema_version"), 1)
        del raw["schema_version"]
        path = self.directory / "no-schema.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ScannerError):
            load_scanner_config(path)

        raw["schema_version"] = 2
        path = self.directory / "wrong-schema.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ScannerError):
            load_scanner_config(path)

    def test_total_resource_budget_reserves_candidate_bytes_before_decompression(self) -> None:
        nested = with_unsupported_compression(
            jar_bytes(
                [
                    ("messages/A.properties", b"aaaa"),
                    ("messages/B.properties", b"bbbb"),
                ]
            )
        )
        source = write_outer_archive(self.directory / "idea.zip", [("app.jar", nested)])
        limits = replace(self.config, max_total_resource_bytes=4)

        inventory = scan_archive(source, limits)

        self.assertEqual(inventory.resources, ())
        self.assertEqual(
            [(item.resource_path, item.reason.value) for item in inventory.exclusions],
            [
                ("messages/A.properties", "unsupported_compression"),
                ("messages/B.properties", "total_resource_bytes_exceeded"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

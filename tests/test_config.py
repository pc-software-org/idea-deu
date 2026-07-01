import json
import re
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from scripts.idea_deu.config import ProductConfig, load_product_config


class ProductConfigTest(unittest.TestCase):
    VALID_CONFIG = {
        "archive": "idea-2025.3.1.1.win.zip",
        "version": "2025.3.1.1",
        "build_number": "253.29346.240",
        "product_code": "IU",
        "sha256": "755b9549eb41ddec86ea111e4aba94b4fd6e39a60b1de7945c92652c70f80026",
        "since_build": "253.29346.240",
        "until_build": "253.29346.240",
        "plugin_id": "org.pc-software.idea-deu",
        "plugin_version": "2025.3.1.1",
    }

    def test_loads_exact_product_binding(self) -> None:
        config = load_product_config(Path("config/product.json"))

        self.assertEqual(config.version, "2025.3.1.1")
        self.assertEqual(config.build_number, "253.29346.240")
        self.assertEqual(config.product_code, "IU")
        self.assertRegex(config.sha256, re.compile(r"^[0-9a-f]{64}$"))

    def test_product_config_is_frozen(self) -> None:
        config = ProductConfig(**self.VALID_CONFIG)

        with self.assertRaises(FrozenInstanceError):
            config.version = "changed"  # type: ignore[misc]

    def test_rejects_missing_key(self) -> None:
        invalid = self.VALID_CONFIG | {}
        del invalid["archive"]

        with self.assertRaises(ValueError):
            self._load(invalid)

    def test_rejects_extra_key(self) -> None:
        with self.assertRaises(ValueError):
            self._load(self.VALID_CONFIG | {"unexpected": "value"})

    def test_rejects_invalid_sha256(self) -> None:
        for sha256 in ("a" * 63, "A" * 64, "g" * 64):
            with self.subTest(sha256=sha256), self.assertRaises(ValueError):
                self._load(self.VALID_CONFIG | {"sha256": sha256})

    def test_rejects_inexact_compatibility_bounds(self) -> None:
        for field in ("since_build", "until_build"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self._load(self.VALID_CONFIG | {field: "253.*"})

    def _load(self, data: dict[str, str]) -> ProductConfig:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "product.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return load_product_config(path)


if __name__ == "__main__":
    unittest.main()

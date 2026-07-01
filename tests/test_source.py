import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from scripts.idea_deu.config import ProductConfig
from scripts.idea_deu.source import SourceValidationError, validate_source
from tests.fixtures.source_factory import make_source_archive


class SourceValidationTest(unittest.TestCase):
    def test_accepts_matching_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(Path(directory))

            source = validate_source(self._config(archive, sha256))

            self.assertEqual(source.archive, archive)
            self.assertEqual(source.sha256, sha256)
            self.assertEqual(source.version, "2025.3.1.1")
            self.assertEqual(source.build_number, "253.29346.240")
            self.assertEqual(source.product_code, "IU")
            with self.assertRaises(FrozenInstanceError):
                source.version = "changed"  # type: ignore[misc]

    def test_rejects_wrong_hash_with_expected_and_actual_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, actual_sha256 = make_source_archive(Path(directory))
            expected_sha256 = "0" * 64

            with self.assertRaisesRegex(
                SourceValidationError,
                f"expected {expected_sha256}.*actual {actual_sha256}",
            ):
                validate_source(self._config(archive, expected_sha256))

    def test_rejects_wrong_build_with_expected_and_actual_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(
                Path(directory), build_number="253.29346.241"
            )

            with self.assertRaisesRegex(
                SourceValidationError,
                r"buildNumber.*expected 253\.29346\.240.*actual 253\.29346\.241",
            ):
                validate_source(self._config(archive, sha256))

    def test_rejects_archive_without_root_product_info(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(
                Path(directory), include_product_info=False
            )

            with self.assertRaisesRegex(
                SourceValidationError, r"missing.*product-info\.json"
            ):
                validate_source(self._config(archive, sha256))

    def test_rejects_corrupt_zip_with_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "broken.zip"
            archive.write_bytes(b"not a zip")
            sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()

            with self.assertRaisesRegex(SourceValidationError, "valid ZIP archive"):
                validate_source(self._config(archive, sha256))

    def test_rejects_malformed_product_info_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(
                Path(directory), product_info="{broken"
            )

            with self.assertRaisesRegex(
                SourceValidationError, r"product-info\.json.*valid JSON"
            ):
                validate_source(self._config(archive, sha256))

    def test_rejects_duplicate_product_info_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(
                Path(directory),
                product_info=(
                    '{"version":"old","version":"2025.3.1.1",'
                    '"buildNumber":"253.29346.240","productCode":"IU"}'
                ),
            )

            with self.assertRaisesRegex(
                SourceValidationError, r"duplicate.*version.*product-info\.json"
            ):
                validate_source(self._config(archive, sha256))

    def test_rejects_non_object_product_info(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(Path(directory), product_info="[]")

            with self.assertRaisesRegex(
                SourceValidationError, r"product-info\.json.*JSON object"
            ):
                validate_source(self._config(archive, sha256))

    def test_rejects_missing_identity_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive, sha256 = make_source_archive(
                Path(directory),
                product_info=(
                    '{"version":"2025.3.1.1",'
                    '"buildNumber":"253.29346.240"}'
                ),
            )

            with self.assertRaisesRegex(
                SourceValidationError, r"product-info\.json.*missing.*productCode"
            ):
                validate_source(self._config(archive, sha256))

    def test_rejects_wrong_version_and_product_code(self) -> None:
        cases = (
            ("version", "2025.3.1", "2025.3.1.1"),
            ("productCode", "IC", "IU"),
        )
        for field, actual, expected in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                kwargs = (
                    {"version": actual}
                    if field == "version"
                    else {"product_code": actual}
                )
                archive, sha256 = make_source_archive(Path(directory), **kwargs)

                with self.assertRaisesRegex(
                    SourceValidationError,
                    f"{field}.*expected {expected}.*actual {actual}",
                ):
                    validate_source(self._config(archive, sha256))

    def test_rejects_missing_archive_with_path(self) -> None:
        archive = Path("does-not-exist.zip")

        with self.assertRaisesRegex(SourceValidationError, str(archive)):
            validate_source(self._config(archive, "0" * 64))

    @staticmethod
    def _config(archive: Path, sha256: str) -> ProductConfig:
        return ProductConfig(
            archive=str(archive),
            version="2025.3.1.1",
            build_number="253.29346.240",
            product_code="IU",
            sha256=sha256,
            since_build="253.29346.240",
            until_build="253.29346.240",
            plugin_id="org.pc-software.idea-deu",
            plugin_version="2025.3.1.1",
        )


if __name__ == "__main__":
    unittest.main()

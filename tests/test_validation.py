import unittest

from scripts.idea_deu.validation import (
    FindingCode,
    Severity,
    validate_translation,
)


class TranslationValidationTests(unittest.TestCase):
    def assert_code(
        self,
        source: str,
        target: str,
        code: FindingCode,
        **kwargs: object,
    ) -> None:
        result = validate_translation(source, target, **kwargs)
        self.assertIn(code, {finding.code for finding in result.findings})

    def assert_clean(self, text: str) -> None:
        result = validate_translation(text, text)
        self.assertEqual((), result.findings)
        self.assertFalse(result.is_blocking)

    def test_result_models_are_typed_and_canonically_serializable(self) -> None:
        result = validate_translation("Hello {0}", "Hallo")
        self.assertTrue(result.is_blocking)
        finding = result.findings[0]
        self.assertEqual(Severity.BLOCKING, finding.severity)
        self.assertEqual(FindingCode.PLACEHOLDER_MISMATCH, finding.code)
        self.assertEqual(
            {"findings": [{"code": "placeholder_mismatch", "severity": "blocking"}]},
            result.to_dict(),
        )
        with self.assertRaises(AttributeError):
            finding.code = FindingCode.EMPTY_TARGET  # type: ignore[misc]

    def test_empty_or_whitespace_target_is_blocking(self) -> None:
        for target in ("", "  \t\n"):
            with self.subTest(target=target):
                self.assert_code("Text", target, FindingCode.EMPTY_TARGET)

    def test_message_format_placeholders_preserve_multiset_and_context(self) -> None:
        cases = (
            ("Open {0} from {1}", "Öffne {0}"),
            ("{0} then {0}", "{0}"),
            ("Total {1,number}", "Gesamt {1}"),
            ("At {0,date,short}", "Um {0,time,short}"),
            ("{0,choice,0#none|1#{1,number}}", "{0,choice,0#keine|1#{1}}"),
        )
        for source, target in cases:
            with self.subTest(source=source, target=target):
                self.assert_code(source, target, FindingCode.PLACEHOLDER_MISMATCH)

    def test_valid_message_format_and_quoting_is_clean(self) -> None:
        for text in (
            "Open {0} at {1,time,short}",
            "'{0}' and ''{1,number}''",
            "{0,choice,0#'{'none'}'|1#{1,date,long}}",
        ):
            with self.subTest(text=text):
                self.assert_clean(text)

    def test_malformed_target_message_format_is_blocking(self) -> None:
        for target in ("Hallo {0", "Hallo }", "Hallo '{0}"):
            with self.subTest(target=target):
                self.assert_code("Hello {0}", target, FindingCode.MESSAGE_FORMAT_INVALID)

    def test_printf_placeholders_are_preserved(self) -> None:
        cases = (
            ("Use %s", "Nutze %d"),
            ("%1$d / %2$08.2f", "%2$08.2f / %1$s"),
            ("Progress: %s%%", "Fortschritt: %s%"),
        )
        for source, target in cases:
            with self.subTest(source=source, target=target):
                self.assert_code(source, target, FindingCode.PLACEHOLDER_MISMATCH)
        self.assert_clean("Value %1$-+#08.2f and %%")

    def test_template_placeholders_and_mnemonics_are_preserved(self) -> None:
        cases = (
            ("Hello ${name}", "Hallo ${user}"),
            ("Set $NAME$", "Setze $USER$"),
            ("&Open", "Öffnen"),
            ("_Run", "Starten"),
            ("Save && Close", "Speichern & Schließen"),
            ("Use __name__", "Nutze _name_"),
        )
        for source, target in cases:
            with self.subTest(source=source, target=target):
                self.assert_code(source, target, FindingCode.PLACEHOLDER_MISMATCH)

    def test_markup_structure_links_and_attribute_placeholders_are_preserved(self) -> None:
        cases = (
            (
                "<b>Bold <i>now</i></b>",
                "<b>Fett</b><i>jetzt</i>",
                FindingCode.MARKUP_STRUCTURE_CHANGED,
            ),
            (
                "<a href='https://example.test'>Go</a>",
                "<a href='https://evil.test'>Los</a>",
                FindingCode.LINK_CHANGED,
            ),
            (
                "<img src='icon.png'/>",
                "<img src='other.png'/>",
                FindingCode.LINK_CHANGED,
            ),
            (
                "<a href='/{0}'>Go</a>",
                "<a href='/fixed'>Los</a>",
                FindingCode.PLACEHOLDER_MISMATCH,
            ),
        )
        for source, target, code in cases:
            with self.subTest(source=source, target=target):
                self.assert_code(source, target, code)
        self.assert_clean("Before <b title='x'>bold</b><br/>after")

    def test_markup_structural_attribute_names_are_preserved(self) -> None:
        self.assert_code(
            "<td colspan='2'>Value</td>",
            "<td rowspan='2'>Wert</td>",
            FindingCode.MARKUP_STRUCTURE_CHANGED,
        )

    def test_attribute_quote_style_does_not_change_placeholder_count(self) -> None:
        result = validate_translation(
            "<a href='/{0}'>Go</a>",
            '<a href="/{0}">Los</a>',
        )
        self.assertEqual((), result.findings)

    def test_malformed_markup_and_external_entities_are_blocked_without_resolution(self) -> None:
        self.assert_code("<b>bold</b>", "<b>fett", FindingCode.MARKUP_STRUCTURE_CHANGED)
        self.assert_code(
            "<b>bold</b>",
            '<!DOCTYPE x [<!ENTITY ext SYSTEM "file:///etc/passwd">]><b>&ext;</b>',
            FindingCode.MARKUP_STRUCTURE_CHANGED,
        )

    def test_plain_punctuation_is_not_mistaken_for_syntax(self) -> None:
        for text in ("a < b & c > d", "100% complete", "set {x, y}", "R&D"):
            with self.subTest(text=text):
                self.assert_clean(text)

    def test_length_ratio_is_warning_and_not_blocking(self) -> None:
        result = validate_translation("Short", "Sehr viel längerer Text als die Quelle")
        self.assertEqual([FindingCode.LENGTH_RATIO], [finding.code for finding in result.findings])
        self.assertEqual(Severity.WARNING, result.findings[0].severity)
        self.assertFalse(result.is_blocking)

    def test_glossary_uses_case_insensitive_word_boundaries(self) -> None:
        glossary = {"Projekt": ("Project", "Vorhaben")}
        for target in ("Das Project öffnen", "Das VORHABEN öffnen"):
            with self.subTest(target=target):
                result = validate_translation("Open project", target, glossary=glossary)
                self.assertIn(FindingCode.GLOSSARY_MISMATCH, {f.code for f in result.findings})
                self.assertFalse(result.is_blocking)
        result = validate_translation("Projector", "Projektor", glossary=glossary)
        self.assertNotIn(FindingCode.GLOSSARY_MISMATCH, {f.code for f in result.findings})

    def test_context_is_accepted_for_future_policy_selection(self) -> None:
        result = validate_translation(
            "Text", "Text", context={"key": "action.open"}
        )
        self.assertEqual((), result.findings)


if __name__ == "__main__":
    unittest.main()

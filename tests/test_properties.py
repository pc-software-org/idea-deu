import unittest

from scripts.idea_deu.properties import PropertiesError, parse_properties, render_properties


class PropertiesTest(unittest.TestCase):
    def test_parses_java_syntax_and_round_trips_losslessly(self) -> None:
        data = (
            b"# heading\r\n! second\r\n\r\n"
            b"equals=value\r\ncolon:value\r\nspace value\r\n"
            b"continued=first\\\r\n  second\\\r\n\tthird\r\n"
            b"escaped\\ key\\=part\\:x = escaped\\ value\\:\\=\\\\\r\n"
            b"unicode=Gr\\u00FC\\u00DFe\r\nempty=\r\n"
            b"\\#key=\\!value\r\n"
        )

        document = parse_properties(data)

        self.assertEqual(
            document.values,
            {
                "equals": "value",
                "colon": "value",
                "space": "value",
                "continued": "firstsecondthird",
                "escaped key=part:x": "escaped value:=\\",
                "unicode": "Grüße",
                "empty": "",
                "#key": "!value",
            },
        )
        self.assertEqual(render_properties(document, {}), data)
        self.assertEqual(render_properties(document, document.values), data)

    def test_preserves_lf_and_missing_final_newline(self) -> None:
        data = b"# comment\nkey:value"
        document = parse_properties(data)
        self.assertEqual(render_properties(document, {}), data)

    def test_replaces_only_known_values_and_escapes_them(self) -> None:
        data = b"first = old\ncontinued=old\\\n  value\nlast:keep\n"
        document = parse_properties(data)

        rendered = render_properties(
            document, {"first": " leading:=\\\nnext", "continued": "neu"}
        )

        self.assertEqual(
            rendered,
            b"first = \\ leading\\:\\=\\\\\\nnext\ncontinued=neu\nlast:keep\n",
        )
        self.assertEqual(parse_properties(rendered).values["first"], " leading:=\\\nnext")

    def test_rejects_unknown_translation_key(self) -> None:
        document = parse_properties(b"known=value\n")
        with self.assertRaisesRegex(PropertiesError, "unknown.*missing"):
            render_properties(document, {"missing": "value"})

    def test_partial_translation_mapping_is_allowed(self) -> None:
        document = parse_properties(b"one=1\ntwo=2\n")
        self.assertEqual(render_properties(document, {"one": "eins"}), b"one=eins\ntwo=2\n")

    def test_rejects_duplicate_logical_keys(self) -> None:
        with self.assertRaisesRegex(PropertiesError, "duplicate.*same key"):
            parse_properties(b"same\\ key=one\nsame\\ key:two\n")

    def test_rejects_malformed_input(self) -> None:
        cases = {
            "unicode escape": b"key=bad\\u12xz\n",
            "continuation": b"key=value\\",
            "UTF-8": b"key=\xff\n",
        }
        for message, data in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(
                PropertiesError, message
            ):
                parse_properties(data)


if __name__ == "__main__":
    unittest.main()

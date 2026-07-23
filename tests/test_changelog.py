import unittest

from scripts.idea_deu.changelog import ChangelogError, render_change_notes


CHANGELOG = """# Changelog

## 2026.1.4.1
- Erster Punkt
- Zweiter Punkt

## 2026.1.4
- Erstveröffentlichung
"""


class RenderChangeNotesTests(unittest.TestCase):
    def test_extracts_requested_version_section(self):
        self.assertEqual(
            render_change_notes(CHANGELOG, "2026.1.4.1"),
            "<ul><li>Erster Punkt</li><li>Zweiter Punkt</li></ul>",
        )

    def test_selects_correct_section_among_many(self):
        self.assertEqual(
            render_change_notes(CHANGELOG, "2026.1.4"),
            "<ul><li>Erstveröffentlichung</li></ul>",
        )

    def test_accepts_asterisk_bullets(self):
        text = "## 1.0\n* Eins\n* Zwei\n"
        self.assertEqual(render_change_notes(text, "1.0"),
                         "<ul><li>Eins</li><li>Zwei</li></ul>")

    def test_html_special_characters_are_escaped(self):
        text = "## 1.0\n- Menü & <b> Co\n"
        self.assertEqual(render_change_notes(text, "1.0"),
                         "<ul><li>Menü &amp; &lt;b&gt; Co</li></ul>")

    def test_missing_version_raises(self):
        with self.assertRaises(ChangelogError):
            render_change_notes(CHANGELOG, "9.9.9")

    def test_empty_section_raises(self):
        text = "## 1.0\n\n## 2.0\n- Punkt\n"
        with self.assertRaises(ChangelogError):
            render_change_notes(text, "1.0")

    def test_non_bullet_content_raises(self):
        text = "## 1.0\nFreitext ohne Aufzählung\n"
        with self.assertRaises(ChangelogError):
            render_change_notes(text, "1.0")

    def test_cdata_terminator_survives_verbatim(self):
        # render_change_notes does not touch ]]>; package._cdata_safe handles it.
        text = "## 1.0\n- danger ]]> here\n"
        self.assertEqual(render_change_notes(text, "1.0"),
                         "<ul><li>danger ]]&gt; here</li></ul>")


if __name__ == "__main__":
    unittest.main()

"""Render a version's CHANGELOG.md section into plugin change-notes HTML.

The Marketplace "What's new" text is taken from the <change-notes> element in
the plugin descriptor at upload time and cannot be edited afterwards, so the
notes must be present in every packaged build. CHANGELOG.md is the single,
reviewable source; this module extracts the section for the version being built
and renders it as a minimal HTML bullet list.

Markdown is deliberately minimal: only ``## <version>`` section headers and
``-``/``*`` bullet lines. Anything else in a section is an error rather than
silently dropped."""
from __future__ import annotations

import html


class ChangelogError(ValueError):
    """Raised when CHANGELOG.md lacks a usable section for the built version."""


def render_change_notes(changelog_text: str, version: str) -> str:
    """Return ``<ul>…</ul>`` change-notes HTML for *version* from *changelog_text*.

    Fails loud: a missing section, an empty section, or any non-bullet content
    is a ``ChangelogError`` — a release without notes should not build."""
    lines = changelog_text.splitlines()
    section: list[str] | None = None
    for line in lines:
        header = _section_version(line)
        if header is not None:
            if header == version:
                section = []
            elif section is not None:
                break  # next version header ends the wanted section
            continue
        if section is not None:
            section.append(line)

    if section is None:
        raise ChangelogError(f"CHANGELOG.md has no section for version {version}")

    items: list[str] = []
    for raw in section:
        stripped = raw.strip()
        if not stripped:
            continue
        bullet = _bullet_content(stripped)
        if bullet is None:
            raise ChangelogError(
                f"non-bullet line in CHANGELOG.md section {version}: {stripped!r}")
        items.append(f"<li>{html.escape(bullet, quote=False)}</li>")

    if not items:
        raise ChangelogError(f"CHANGELOG.md section for version {version} has no entries")
    return "<ul>" + "".join(items) + "</ul>"


def _section_version(line: str) -> str | None:
    """Return the version of a ``## <version>`` header line, else None."""
    stripped = line.strip()
    if stripped.startswith("## "):
        return stripped[3:].strip()
    return None


def _bullet_content(stripped_line: str) -> str | None:
    """Return the text of a ``-``/``*`` bullet line, else None."""
    if stripped_line[:2] in ("- ", "* "):
        return stripped_line[2:].strip()
    return None

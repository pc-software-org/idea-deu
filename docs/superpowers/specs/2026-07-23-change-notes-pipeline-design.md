# Design: „What's new" (change-notes) über die Pipeline befüllen

**Datum:** 2026-07-23
**Status:** Genehmigt (Design)

## Problem

Beim Upload neuer Versionen zur JetBrains Marketplace ist die „What's new"-Sektion
jeder Version leer und lässt sich nachträglich nicht mehr ändern. Ursache: der
Marketplace zieht den „What's new"-Text pro Version aus dem `<change-notes>`-
Element in `plugin/META-INF/plugin.xml`. Das Template enthält dieses Element
nicht, also wird jede Version ohne Change-Notes hochgeladen.

Zwei Stellen blockieren die Behebung:

1. Das Template (`plugin/META-INF/plugin.xml`) hat kein `<change-notes>`.
2. `_validate_descriptor` in `scripts/idea_deu/package.py` hat eine strikte
   Allowlist der erlaubten Kind-Elemente. Ein zusätzliches Element würde den
   Build mit `"plugin descriptor identity or compatibility mismatch"` abbrechen.

## Quelle des Textes: `CHANGELOG.md`

Neue `CHANGELOG.md` im Repo-Root. Pro Version ein `## <version>`-Abschnitt,
darunter eine Bullet-Liste. Bewusst minimal gehaltenes Markdown — **nur
Bullet-Listen**, kein Fett/Links/Überschriften/verschachtelte Listen.

```markdown
# Changelog

## 2026.1.4.1
- Menüeintrag "Refactor" korrigiert
- Tooltip-Übersetzungen ergänzt

## 2026.1.4
- Erstveröffentlichung für 2026.1.4
```

## Architektur

### Neues Modul `scripts/idea_deu/changelog.py` (stdlib only)

- `class ChangelogError(ValueError)`
- `render_change_notes(changelog_text: str, version: str) -> str`
  - Parst `## <version>`-Abschnitte; Zeilen der gesuchten Version bis zum
    nächsten `##`.
  - Bullet-Zeilen (`- ` oder `* `) → `<li>…</li>`, Inhalt HTML-escaped
    (`&`, `<`, `>`). Ergebnis in `<ul>…</ul>` gewickelt, als eine Zeile
    ohne führenden/abschließenden Whitespace.
  - Nicht-Bullet-Zeilen innerhalb des Abschnitts (außer Leerzeilen) sind ein
    Fehler → `ChangelogError` (Markdown bleibt einfach, keine stille Toleranz).
  - **Fail-loud**: fehlt der Abschnitt für `version` oder enthält er keine
    Bullets → `ChangelogError`. Begründung: ohne inhaltliche Änderung gibt es
    keinen Grund für ein Release, also darf ein Release ohne Notes nicht bauen.

### `plugin/META-INF/plugin.xml`

Nach `<description>` eine Zeile ergänzen:

```xml
<change-notes><![CDATA[@CHANGE_NOTES@]]></change-notes>
```

CDATA ist erforderlich: Pythons ElementTree liest den CDATA-Inhalt als reinen
`.text` ohne XML-Kindelemente. Damit greift der strikte Validator sauber und
die HTML-Tags werden nicht als XML-Struktur missverstanden.

### `scripts/idea_deu/package.py`

- `render_descriptor(template, *, version, since_build, until_build, change_notes)`
  - Ersetzt zusätzlich `@CHANGE_NOTES@`. Schützt gegen `]]>` im Inhalt
    (CDATA-safe Split), damit die CDATA-Sektion nicht vorzeitig endet.
- `_validate_descriptor(data, *, version, since_build, until_build, change_notes)`
  - `("change-notes", {}, change_notes, ())` an die passende Position der
    `expected`-Tuple (direkt nach `description`). Strikte Identitätsprüfung
    bleibt bestehen; Vergleich gegen den erwarteten HTML-Text.
- `change_notes` durchreichen durch `plugin_package_bytes`,
  `build_plugin_package` und `verify_plugin_package`.

### `scripts/idea_deu/cli.py`

- Im `package`-Kommando und im `verify`-Pfad `CHANGELOG.md` aus dem Repo-Root
  lesen, `render_change_notes(text, config.plugin_version)` aufrufen und als
  `change_notes=` an `build_plugin_package` / `verify_plugin_package` übergeben.

## Datenfluss

```
CHANGELOG.md ──(render_change_notes, plugin_version)──▶ change_notes (HTML)
                                                             │
plugin.xml @CHANGE_NOTES@ ──(render_descriptor)──▶ Deskriptor mit <change-notes>
                                                             │
                                                    ──▶ idea-deu.jar / idea-deu.zip
                                                             │
                                        CI-Upload ──▶ Marketplace „What's new"
```

## Determinismus

`CHANGELOG.md` ist eingecheckt → Rebuild ergibt identischen SHA-256. Der
`Verify deterministic rebuild`-Schritt in CI bleibt grün.

## Fehlerbehandlung

- Fehlende/leere Version im CHANGELOG → `ChangelogError`, Build bricht ab.
- Nicht-Bullet-Inhalt im Abschnitt → `ChangelogError`.
- `]]>` im Text → CDATA-sicher zerlegt.
- Deskriptor-Struktur weicht ab → bestehende `PackageError`-Identitätsprüfung.

## Tests

- **Neu `tests/test_changelog.py`**: Version-Extraktion, HTML-Escaping,
  fehlende Version (Fehler), Nicht-Bullet-Inhalt (Fehler), `]]>`-Guard,
  Auswahl des korrekten Abschnitts bei mehreren Versionen.
- **`tests/test_package.py`**: `render_descriptor`/`_validate_descriptor` mit
  `change_notes`-Parameter; Assertion, dass `<change-notes>` im Deskriptor
  steht und den erwarteten HTML-Text enthält; Round-Trip build→verify.
- **`tests/test_cli_e2e.py`**: CHANGELOG.md im Fixture vorhanden; gepacktes
  Artefakt enthält die change-notes; fehlender CHANGELOG-Abschnitt lässt
  `package` fehlschlagen.

## Doku

- `CLAUDE.md` (Projekt), Abschnitt „Release": ergänzen, dass vor dem Tag ein
  `## <plugin_version>`-Abschnitt in `CHANGELOG.md` angelegt werden muss.
  Hinweis: der Text wird verbatim zu Marketplace „What's new" und ist danach
  nicht mehr änderbar.

## Bewusst ausgeschlossen (YAGNI)

- Voller Markdown-Parser (nur Bullet-Listen).
- change-notes-Feld in `config/product.json` (strikte Key-Allowlist im Loader;
  CHANGELOG ist besser lesbar/reviewbar).
- Editieren bereits hochgeladener Versionen (Marketplace-seitig fixiert).

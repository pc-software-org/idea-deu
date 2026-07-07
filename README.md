# idea-deu — German Language Pack for IntelliJ IDEA 2026.1.3

An offline-installable German (`de`) language pack for **IntelliJ IDEA
2026.1.3**, build **261.25134.95** (product code `IU`, Ultimate). The pack is a
code-free IntelliJ plugin that overlays translated resource bundles and
description files onto the IDE via a `languageBundle` extension.

The translation corpus and pipeline are reproducible: a dependency-light Python
package fingerprints the exact source distribution, inventories every
translatable resource in the nested IDE/plugin JARs, stores source and
translation state as deterministic JSONL, validates every translation, and
packages a byte-deterministic plugin ZIP.

## Prerequisites

- Python 3.12+ (standard library only; no third-party runtime dependencies).
- The exact source archive `idea-2026.1.3.win.zip` placed in the repository
  root. It is a build input, not repository content (git-ignored). Verify it:
  ```
  shasum -a 256 idea-2026.1.3.win.zip
  # 71b0e287a2fec5fe3428dda95ad8e947e4c35cd35e7dd3e5cad1fc19dc92fb3e
  ```
- JDK 21 only if you want to run the optional Gradle Plugin Verifier (below).

## Repository layout

- `config/product.json` — the exact source binding (version, build, SHA-256).
- `config/scanner.json` — supported resource patterns, exclusions, and
  collision selections.
- `scripts/idea_deu/` — the pipeline package (`python -m scripts.idea_deu`).
- `inventory/` — committed scan evidence (resources, exclusions, collisions,
  source blobs, `summary.json`).
- `translations/units.jsonl` — the resumable translation state (one unit per
  bundle key / description file).
- `glossary/de.json` — terminology and style rules.
- `plugin/META-INF/plugin.xml` — the plugin descriptor template.
- `reports/` — machine- and human-readable status.
- `generated/`, `dist/` — rebuildable output (git-ignored).

## Build the pack

```bash
python3 -m scripts.idea_deu validate-source   # verify the archive matches config
python3 -m scripts.idea_deu scan               # inventory + carry over translations
python3 -m scripts.idea_deu validate           # revalidate the whole corpus
python3 -m scripts.idea_deu generate           # write generated/plugin/
python3 -m scripts.idea_deu package            # write dist/idea-deu.zip
shasum -a 256 dist/idea-deu.zip                 # matches dist/idea-deu.zip.sha256
```

The build is deterministic: repeating `generate` + `package` produces a
byte-identical `dist/idea-deu.zip` (fixed entry order, fixed timestamps).

## Status and continuation

```bash
python3 -m scripts.idea_deu status   # counts + the exact next command
```

The translation loop is resumable from repository state alone. After an
interruption, `status` prints the next command. To translate remaining open
units:

```bash
python3 -m scripts.idea_deu next-batch --limit 200   # export one bounded batch
#   fill the "target" field of each line in the printed batch file, then:
python3 -m scripts.idea_deu import-batch translations/batches/<file>.jsonl
python3 -m scripts.idea_deu status
```

`import-batch` validates every target; clean units become
`technically_reviewed`, units with a blocking finding stay `translated` with
the finding recorded, and are re-picked by re-exporting after correction.

## Offline installation on Windows (air-gapped)

1. Start an unmodified **IntelliJ IDEA 2026.1.3 (261.25134.95)**.
2. Disconnect Marketplace / network access.
3. **Settings → Plugins → ⚙ → Install Plugin from Disk…** and choose
   `dist/idea-deu.zip`.
4. **Settings → Appearance & Behavior → System Settings → Language and Region**
   → set **Language** to *Deutsch*.
5. Restart the IDE when prompted.

The pack is built from exactly build 261.25134.95 but declares compatibility
with the whole 2026.1 line (`since-build = 261`, `until-build = 261.*`), so it
also loads on 2026.1.x patch releases. Untranslated or changed keys fall back to
English. Rebuild against a newer distribution to follow a later release line.

### Rollback / uninstall

**Settings → Plugins → German Language Pack → Uninstall**, or set the language
back to *English* and restart. Removing the plugin fully reverts the UI.

## Optional: JetBrains Plugin Verifier

The generated plugin is descriptor-verified by the pipeline. To additionally run
JetBrains' Plugin Verifier (requires JDK 21 and network for first download), use
the Gradle project in this repo:

```bash
./gradlew verifyPluginProjectConfiguration
./gradlew verifyPlugin
```

See `docs/acceptance-checklist.md` for the full manual acceptance procedure.

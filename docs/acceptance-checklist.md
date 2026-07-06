# Acceptance Checklist — German Language Pack 2026.1.3

Target: IntelliJ IDEA **2026.1.3**, build **261.25134.95** (`IU`).
Artifact: `dist/idea-deu.zip` (SHA-256 in `dist/idea-deu.zip.sha256`).

Mark each item **PASS/FAIL** with a screenshot or a one-line observation.
Any crash, an unresolved English string that has no recorded exclusion, a
missing dependency, or a broken placeholder is a **release blocker**.

## Environment

| Field | Value |
|---|---|
| IDE build | `261.25134.95` (confirm via Help → About) |
| OS | Windows ____ (record version) |
| Plugin SHA-256 | ____ (must equal `dist/idea-deu.zip.sha256`) |
| Marketplace access | disconnected |
| License mode | run the checklist **twice**: licensed and unlicensed/trial |

## Installation

- [ ] Install Plugin from Disk accepts `idea-deu.zip` without error.
- [ ] Language and Region lists *Deutsch*; selecting it prompts a restart.
- [ ] After restart the UI is German.
- [ ] Help → About still reports build 261.25134.95 (pack did not alter the IDE).

## Functional areas (spot-check German + correct placeholders)

- [ ] **Project**: New Project wizard, project structure, module settings.
- [ ] **Editor**: context menu, intention actions (Alt+Enter), inspections’
      descriptions render as German HTML with intact `<code>`/links.
- [ ] **Search**: Search Everywhere, Find in Files, Find Usages counts
      (e.g. “{0} Verwendungen”) show correct numbers.
- [ ] **Settings**: Appearance, Keymap, Editor, Plugins pages are German;
      no truncated/overflowing labels on key dialogs.
- [ ] **Build**: build/rebuild messages, problems view.
- [ ] **Run/Debug**: run configurations dialog, debugger tool window,
      breakpoints, evaluate expression.
- [ ] **Git**: Commit tool window, Push, Branches popup, Log; retained terms
      (Git, Commit, Branch, Cherry-Pick) are unchanged.
- [ ] **Premium/paid features** (licensed run): profiler, database tools,
      remote dev entry points — labels are German, no crashes.

## Integrity checks

- [ ] Placeholders (`{0}`, `%s`, `${...}`) render with values, never literally.
- [ ] Mnemonics (underlined access keys) still work in menus/dialogs.
- [ ] No mixed English/German within a single dialog beyond intended retained
      terms and recorded exclusions.
- [ ] Notifications and error dialogs are German.

## Recorded acceptances

- **Warnings**: 333 `length_ratio` warnings (German rendering longer than 2.5×
  the source on short labels) are accepted as inherent German verbosity; verify
  none cause visible truncation in the areas above.
- **Retained English**: product/tool names and pseudo-tag sentinels
  (`<Unknown>`, `<No Group>`, `<empty name>`) intentionally stay English.

## Sign-off

- [ ] Licensed run: all areas PASS.
- [ ] Unlicensed/trial run: all areas PASS.
- [ ] Result and evidence recorded in `reports/release-2026.1.3.md`.

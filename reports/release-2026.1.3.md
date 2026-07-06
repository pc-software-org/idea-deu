# Release report — German Language Pack 2026.1.3

## Source binding

| Field | Value |
|---|---|
| Product | IntelliJ IDEA Ultimate (`IU`) |
| Version | 2026.1.3 |
| Build | 261.25134.95 (`since-build` == `until-build`) |
| Source archive | `idea-2026.1.3.win.zip` |
| Source SHA-256 | `71b0e287a2fec5fe3428dda95ad8e947e4c35cd35e7dd3e5cad1fc19dc92fb3e` |

## Artifact

| Field | Value |
|---|---|
| File | `dist/idea-deu.zip` |
| SHA-256 | `2849ca540634e2870f80ab7761b71d16fb7ddce9dc4a20d0f5e1f6a896b715f3` |
| Size | 2 782 669 bytes |
| Determinism | rebuilt twice → byte-identical SHA-256 |
| Descriptor | id `org.pc-software.idea-deu`, `<languageBundle locale="de"/>`, since/until-build 261.25134.95 |

## Inventory

- Resource files: **4190**
- Translation units: **71452**
- Unresolved collisions: **0** (2 content-identical collisions resolved by
  selection; 20 further collision copies excluded)
- Exclusions by reason:

  | Reason | Count |
  |---|---:|
  | already_localized | 3 |
  | collision_not_selected | 22 |
  | directory | 2306 |
  | localized | 1273 |
  | nested_archive | 40 |
  | not_in_translation_reference | 945 |
  | not_jar | 1942 |
  | unsupported_resource | 587662 |

## Translation

- `technically_reviewed`: **71452** (100%)
- `open`: **0**
- Blocking findings: **0**
- Accepted warnings: **333** `length_ratio` — German rendering exceeds 2.5× the
  (short) source length; inherent to German verbosity, no visible truncation
  expected. No other warning categories.

## How 2026.1.3 was produced (migration from 2025.3.1.1)

The corpus was migrated from the prior target (2025.3.1.1, build 253.29346.240):

- Re-scanning the 2026.1.3 distribution carried over every unchanged
  translation. Because a unit id includes its JAR container and JetBrains
  renamed/repackaged many JARs in 261, an additional container-independent
  carry-over by `(path, key, source hash)` recovered **~28 900** translations
  that id-only matching would have dropped.
- Carried over: **~66 000** reviewed translations.
- Genuine delta translated for 261: **5 675** units, in 29 committed batches.
- Blocked-unit rescue: **353** units repaired (placeholder/markup/message-format),
  including a validator refinement so a literal percentage after a placeholder
  ("{0}% classes") is not mistaken for a printf conversion.
- Three latent pipeline defects, surfaced by the first real generate/package
  run, were fixed: `.html` postfix-template descriptions, empty (keyless)
  `.properties` bundles, and the two content-identical collisions.

## Verification status

- Unit test suite: **211 tests, all green**.
- `python -m scripts.idea_deu validate`: exit 0, 0 blocking.
- Deterministic re-scan and re-package confirmed.
- JetBrains Plugin Verifier: **not run in the authoring environment** (no JDK 21
  / no network). Procedure documented in `docs/plugin-verification.md`.
- Windows air-gap acceptance: **pending** — to be executed on Windows with
  IntelliJ IDEA 2026.1.3 per `docs/acceptance-checklist.md` (licensed and
  unlicensed modes). This is a release gate.

## Open items before shipping

1. Run the JetBrains Plugin Verifier against `dist/idea-deu.zip` + IU-261.25134.95.
2. Complete `docs/acceptance-checklist.md` on Windows in both license modes.
3. Decide whether to widen `until-build` beyond the exact 261.25134.95 pin so
   the pack survives 2026.1.x patch releases (currently exact by design).

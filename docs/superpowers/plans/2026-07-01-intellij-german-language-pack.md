# IntelliJ German Language Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resumable, verified pipeline that translates IntelliJ IDEA 2025.3.1.1 build 253.29346.240 into an offline-installable German language-pack ZIP.

**Architecture:** A dependency-light Python package fingerprints the source distribution, scans nested IDE/plugin JARs, persists source and translation units as deterministic JSONL, validates translations, generates locale resources, and packages a code-free IntelliJ plugin. Translation proceeds in bounded committed batches, so a later session can continue from repository state and reports alone.

**Tech Stack:** Python 3.12 standard library, `unittest`, JSON/JSONL, ZIP/JAR, XML/HTML parsers, Gradle Wrapper with IntelliJ Platform Gradle Plugin 2.x for project/plugin verification, IntelliJ IDEA 2025.3.1.1.

---

## File map

- `pyproject.toml`: package metadata and test configuration.
- `config/product.json`: exact source archive, version, build, hash, compatibility, and plugin identity.
- `config/scanner.json`: supported paths, localization exclusions, and explicit archive exclusions.
- `scripts/idea_deu/models.py`: immutable inventory and translation records.
- `scripts/idea_deu/config.py`: configuration loading and validation.
- `scripts/idea_deu/source.py`: ZIP fingerprint and `product-info.json` validation.
- `scripts/idea_deu/scanner.py`: nested JAR discovery and supported-resource inventory.
- `scripts/idea_deu/properties.py`: loss-aware Java Properties parsing and rendering.
- `scripts/idea_deu/state.py`: deterministic JSONL and atomic checkpoint persistence.
- `scripts/idea_deu/validation.py`: placeholder, MessageFormat, markup, links, and length checks.
- `scripts/idea_deu/batches.py`: bounded translation export/import and status transitions.
- `scripts/idea_deu/generator.py`: translated resource-tree generation and collision enforcement.
- `scripts/idea_deu/package.py`: plugin descriptor and deterministic ZIP construction.
- `scripts/idea_deu/report.py`: machine- and human-readable progress reports.
- `scripts/idea_deu/cli.py`: stable commands composing the modules.
- `glossary/de.json`: terminology and neutral/“Sie” style rules.
- `plugin/META-INF/plugin.xml`: source descriptor template for locale `de`.
- `tests/fixtures/`: tiny synthetic IDE ZIP/JAR inputs; never use the multi-gigabyte product ZIP in unit tests.
- `tests/test_*.py`: focused tests matching each module.
- `inventory/`, `translations/`, `reports/`: committed resumable state and evidence.
- `generated/`, `dist/`: ignored rebuildable output.
- `README.md`: setup, continuation, build, verification, and offline installation.

### Task 1: Project skeleton and exact product binding

**Files:**
- Create: `pyproject.toml`
- Create: `config/product.json`
- Create: `scripts/__init__.py`
- Create: `scripts/idea_deu/__init__.py`
- Create: `scripts/idea_deu/config.py`
- Create: `tests/test_config.py`
- Modify: `.gitignore`

- [ ] **Step 1: Calculate the immutable source hash**

Run: `shasum -a 256 idea-2025.3.1.1.win.zip`

Expected: one 64-character hash followed by `idea-2025.3.1.1.win.zip`. Record that exact hash in `config/product.json`; do not substitute a sample value.

- [ ] **Step 2: Write the failing configuration test**

```python
class ConfigTest(unittest.TestCase):
    def test_loads_exact_target(self):
        config = load_product_config(Path("config/product.json"))
        self.assertEqual(config.version, "2025.3.1.1")
        self.assertEqual(config.build_number, "253.29346.240")
        self.assertEqual(config.product_code, "IU")
        self.assertRegex(config.sha256, r"^[0-9a-f]{64}$")
```

- [ ] **Step 3: Run the test and confirm the missing module failure**

Run: `python3 -m unittest tests.test_config -v`

Expected: FAIL because `scripts.idea_deu.config` does not exist.

- [ ] **Step 4: Implement `ProductConfig` and strict JSON loading**

Implement a frozen dataclass with `archive`, `version`, `build_number`, `product_code`, `sha256`, `since_build`, `until_build`, `plugin_id`, and `plugin_version`. Reject missing keys, non-hex hashes, and compatibility values other than exact `253.29346.240`.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.test_config -v`

Expected: PASS.

```bash
git add .gitignore pyproject.toml config/product.json scripts tests/test_config.py
git commit -m "build: bind pipeline to IntelliJ 2025.3.1.1"
```

### Task 2: Source archive validation

**Files:**
- Create: `scripts/idea_deu/source.py`
- Create: `tests/test_source.py`
- Create: `tests/fixtures/source_factory.py`

- [ ] **Step 1: Write failing tests for a matching archive and three rejection paths**

```python
class SourceTest(unittest.TestCase):
    def make_config(self, archive: Path, **changes: str) -> ProductConfig:
        values = {
            "archive": archive,
            "version": "2025.3.1.1",
            "build_number": "253.29346.240",
            "product_code": "IU",
            "sha256": sha256_file(archive),
            "since_build": "253.29346.240",
            "until_build": "253.29346.240",
            "plugin_id": "org.pc-software.idea-deu",
            "plugin_version": "2025.3.1.1",
        }
        values.update(changes)
        return ProductConfig(**values)

    def test_validate_source_accepts_matching_archive(self):
        archive = create_ide_zip("2025.3.1.1", "253.29346.240", "IU")
        self.assertEqual(validate_source(self.make_config(archive)).build_number,
                         "253.29346.240")

    def test_validate_source_rejects_wrong_hash(self):
        archive = create_ide_zip("2025.3.1.1", "253.29346.240", "IU")
        with self.assertRaisesRegex(SourceValidationError, "SHA-256"):
            validate_source(self.make_config(archive, sha256="0" * 64))

    def test_validate_source_rejects_wrong_build(self):
        archive = create_ide_zip("2025.3.1.1", "253.1", "IU")
        with self.assertRaisesRegex(SourceValidationError, "253.29346.240"):
            validate_source(self.make_config(archive))

    def test_validate_source_rejects_missing_product_info(self):
        archive = create_ide_zip(None, None, None)
        with self.assertRaisesRegex(SourceValidationError, "product-info.json"):
            validate_source(self.make_config(archive))
```

The fixture factory must create small ZIPs containing a root `product-info.json` with `version`, `buildNumber`, and `productCode`.

- [ ] **Step 2: Verify failures**

Run: `python3 -m unittest tests.test_source -v`

Expected: four failures caused by missing `validate_source`.

- [ ] **Step 3: Implement streamed SHA-256 and product metadata validation**

Expose `validate_source(config: ProductConfig) -> SourceInfo`. Read the archive in 1 MiB chunks, then parse `product-info.json` directly from the outer ZIP. Raise a domain-specific `SourceValidationError` containing the expected and actual value.

- [ ] **Step 4: Verify and commit**

Run: `python3 -m unittest tests.test_source -v`

Expected: four PASS.

```bash
git add scripts/idea_deu/source.py tests/test_source.py tests/fixtures/source_factory.py
git commit -m "feat: validate IntelliJ source distribution"
```

### Task 3: Nested JAR inventory and resource classification

**Files:**
- Create: `config/scanner.json`
- Create: `scripts/idea_deu/models.py`
- Create: `scripts/idea_deu/scanner.py`
- Create: `tests/test_scanner.py`

- [ ] **Step 1: Write a synthetic outer ZIP with core and bundled-plugin JARs**

Include English `.properties`, supported HTML/XML paths, an already localized `_ja.properties`, a class file, and a third-party JAR. Assert exact inclusion/exclusion reasons and stable IDs of the form `sha256(container + "\0" + resource_path)`.

- [ ] **Step 2: Verify the scanner tests fail**

Run: `python3 -m unittest tests.test_scanner -v`

Expected: FAIL because `scan_distribution` is missing.

- [ ] **Step 3: Implement bounded nested-archive scanning**

Use `ZipFile.open()` plus `SpooledTemporaryFile` to avoid extracting the IDE. Classify only:

```text
*.properties
inspectionDescriptions/**/*.html
intentionDescriptions/**/*.html
fileTemplates/**/*.html
postfixTemplates/**/*.xml
tips/**/*.html
```

Never infer exclusions silently: emit an `ExclusionRecord` with `container`, `path`, and enumerated `reason`.

- [ ] **Step 4: Add collision detection and deterministic ordering**

Group included resources by relative resource path. Mark paths with more than one distinct source container as unresolved collisions. Sort all output by `(container, resource_path)`.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m unittest tests.test_scanner -v`

Expected: PASS.

```bash
git add config/scanner.json scripts/idea_deu/models.py scripts/idea_deu/scanner.py tests/test_scanner.py
git commit -m "feat: inventory translatable IntelliJ resources"
```

### Task 4: Properties parsing and resumable JSONL state

**Files:**
- Create: `scripts/idea_deu/properties.py`
- Create: `scripts/idea_deu/state.py`
- Create: `tests/test_properties.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write parser round-trip tests**

Cover comments, blank lines, `=`, `:`, whitespace separators, continuations, escaped separators, `\uXXXX`, duplicate keys, and CRLF. Assert parsed logical values and byte-stable output when no translation changes.

- [ ] **Step 2: Write atomic-state tests**

Assert canonical JSON key order, stable record order, newline termination, replacement of an existing file, and preservation of the old file when serialization raises before `os.replace`.

- [ ] **Step 3: Run both suites and verify failures**

Run: `python3 -m unittest tests.test_properties tests.test_state -v`

Expected: FAIL for missing parser and state functions.

- [ ] **Step 4: Implement focused APIs**

```python
parse_properties(data: bytes) -> PropertiesDocument
render_properties(document: PropertiesDocument, translations: Mapping[str, str]) -> bytes
read_jsonl(path: Path, record_type: type[T]) -> list[T]
write_jsonl_atomic(path: Path, records: Iterable[JsonRecord]) -> None
```

Duplicate properties keys must be a blocking parse error, not last-one-wins behavior.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m unittest tests.test_properties tests.test_state -v`

Expected: PASS.

```bash
git add scripts/idea_deu/properties.py scripts/idea_deu/state.py tests/test_properties.py tests/test_state.py
git commit -m "feat: persist loss-aware translation state"
```

### Task 5: Translation validation engine

**Files:**
- Create: `scripts/idea_deu/validation.py`
- Create: `tests/test_validation.py`

- [ ] **Step 1: Write table-driven blocking validation tests**

Test preservation of `{0}`, `{1,number}`, `%s`, `%1$d`, `${name}`, `$NAME$`, mnemonic markers, balanced MessageFormat apostrophes, HTML/XML tag structure, and `href` targets. Also test empty translations.

- [ ] **Step 2: Write warning tests**

Assert warnings for a translation more than 2.5 times the source length and for glossary violations. Warnings must not make `ValidationResult.is_blocking` true.

- [ ] **Step 3: Verify failures**

Run: `python3 -m unittest tests.test_validation -v`

Expected: FAIL because `validate_translation` is missing.

- [ ] **Step 4: Implement typed findings**

Return findings with stable codes such as `PLACEHOLDER_MISMATCH`, `MESSAGE_FORMAT_INVALID`, `MARKUP_STRUCTURE_CHANGED`, `LINK_CHANGED`, `EMPTY_TARGET`, `LENGTH_RATIO`, and `GLOSSARY_MISMATCH`. Parse markup with standard-library parsers; do not validate tags using regex alone.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m unittest tests.test_validation -v`

Expected: PASS.

```bash
git add scripts/idea_deu/validation.py tests/test_validation.py
git commit -m "feat: validate German translation integrity"
```

### Task 6: Glossary and bounded translation batches

**Files:**
- Create: `glossary/de.json`
- Create: `scripts/idea_deu/batches.py`
- Create: `tests/test_batches.py`

- [ ] **Step 1: Add explicit initial terminology**

Store rules retaining `Git`, `Commit`, `Branch`, `Debugger`, `Breakpoint`, `API`, `URL`, and `IntelliJ IDEA`; encode “neutral first, Sie fallback” as style metadata. Include forbidden blanket replacements such as translating every `run` identically.

- [ ] **Step 2: Write batch lifecycle tests**

Assert that `export_next_batch(limit=100)` selects only `offen`, never exceeds the limit, and is deterministic. Assert import rejects unknown IDs and changed source hashes, validates every target, and moves clean records to `technisch_geprüft` while retaining blocking records as `übersetzt` with findings.

- [ ] **Step 3: Verify failures**

Run: `python3 -m unittest tests.test_batches -v`

Expected: FAIL because batch APIs are missing.

- [ ] **Step 4: Implement export/import and checkpoint metadata**

Write each batch to `translations/batches/{sequence}-{first_id_prefix}.jsonl`; update `translations/checkpoint.json` atomically with completed sequence, counts, current batch path, and next command.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m unittest tests.test_batches -v`

Expected: PASS.

```bash
git add glossary/de.json scripts/idea_deu/batches.py tests/test_batches.py
git commit -m "feat: add resumable translation batches"
```

### Task 7: Resource generation and collision-safe packaging

**Files:**
- Create: `plugin/META-INF/plugin.xml`
- Create: `scripts/idea_deu/generator.py`
- Create: `scripts/idea_deu/package.py`
- Create: `tests/test_generator.py`
- Create: `tests/test_package.py`

- [ ] **Step 1: Write generation tests**

Assert Properties values are replaced without changing keys/comments, HTML/XML bodies are emitted at exact original paths, untranslated units fail generation, and unresolved path collisions fail with both source containers in the message.

- [ ] **Step 2: Write package tests**

Assert `plugin.xml` contains plugin ID `org.pc-software.idea-deu`, exact build compatibility, and:

```xml
<extensions defaultExtensionNs="com.intellij">
  <languageBundle locale="de"/>
</extensions>
```

Build twice and assert equal SHA-256 output. ZIP entries must be sorted and use a fixed timestamp.

- [ ] **Step 3: Verify failures**

Run: `python3 -m unittest tests.test_generator tests.test_package -v`

Expected: FAIL for missing generator/package functions.

- [ ] **Step 4: Implement generation and deterministic ZIP creation**

Generate into `generated/plugin/`; package to `dist/idea-deu-2025.3.1.1.zip`. Refuse packaging when any included unit is `offen`, has a blocking finding, or belongs to an unresolved collision.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m unittest tests.test_generator tests.test_package -v`

Expected: PASS.

```bash
git add plugin scripts/idea_deu/generator.py scripts/idea_deu/package.py tests/test_generator.py tests/test_package.py
git commit -m "feat: build deterministic German language pack"
```

### Task 8: Reports and command-line workflow

**Files:**
- Create: `scripts/idea_deu/report.py`
- Create: `scripts/idea_deu/cli.py`
- Create: `scripts/idea_deu/__main__.py`
- Create: `tests/test_report.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write report tests**

Assert JSON and Markdown reports contain total files/units, each status count, exclusions by reason, collisions, blocking/warning counts, last completed batch, and a literal next command.

- [ ] **Step 2: Write CLI workflow tests**

Exercise `validate-source`, `scan`, `next-batch --limit 100`, `import-batch PATH`, `validate`, `generate`, `package`, `report`, and `status`. Assert nonzero exit codes for blockers.

- [ ] **Step 3: Verify failures**

Run: `python3 -m unittest tests.test_report tests.test_cli -v`

Expected: FAIL because report and CLI modules are missing.

- [ ] **Step 4: Implement orchestration without duplicating domain logic**

Every mutating command must finish by atomically refreshing `reports/status.json` and `reports/status.md`. `status` must be read-only and print the next resumable command.

- [ ] **Step 5: Run the full unit suite and commit**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests PASS.

```bash
git add scripts/idea_deu tests/test_report.py tests/test_cli.py
git commit -m "feat: expose resumable language-pack workflow"
```

### Task 9: Scan the real 2025.3.1.1 distribution

**Files:**
- Modify: `config/product.json`
- Create: `inventory/resources.jsonl`
- Create: `inventory/exclusions.jsonl`
- Create: `inventory/collisions.jsonl`
- Create: `translations/units.jsonl`
- Create: `reports/status.json`
- Create: `reports/status.md`
- Test: `tests/test_real_inventory.py`

- [ ] **Step 1: Validate the real archive**

Run: `python3 -m scripts.idea_deu validate-source`

Expected: `OK IntelliJ IDEA 2025.3.1.1 (IU-253.29346.240)`.

- [ ] **Step 2: Scan and persist the real inventory**

Run: `python3 -m scripts.idea_deu scan`

Expected: exit 0 and nonempty inventory, exclusions, translation units, and reports.

- [ ] **Step 3: Add invariant tests from observed facts**

The test must assert the recorded archive hash/build, nonzero counts for every supported resource category actually present, unique stable IDs, sorted JSONL, and exact equality between the report totals and inventory files. Do not hard-code guessed counts; record the observed counts in `inventory/summary.json` and compare against it.

- [ ] **Step 4: Resolve every path collision empirically**

For each entry in `inventory/collisions.jsonl`, inspect both source containers and classify it in `config/scanner.json` as identical-deduplicated, container-excluded with reason, or package-layout-separated. Re-run `scan` until `unresolved_collisions` is zero.

- [ ] **Step 5: Verify determinism and commit**

Run `scan` twice, then `git diff --exit-code inventory translations reports config` after the second run.

Expected: no diff after the second scan.

```bash
git add config inventory translations reports tests/test_real_inventory.py
git commit -m "data: inventory IntelliJ 2025.3.1.1 resources"
```

### Task 10: Complete translations in resumable batches

**Files:**
- Modify: `translations/units.jsonl`
- Create/Modify: `translations/batches/*.jsonl`
- Modify: `translations/checkpoint.json`
- Modify: `glossary/de.json`
- Modify: `reports/status.json`
- Modify: `reports/status.md`

- [ ] **Step 1: Export the next bounded batch**

Run: `python3 -m scripts.idea_deu next-batch --limit 100`

Expected: a path to one JSONL file and at most 100 `offen` units. If it reports no open units, continue to Step 5.

- [ ] **Step 2: Translate every target in that one batch**

Use the English value, key, bundle, and neighboring units as context. Preserve all protected tokens. Prefer neutral German; use “Sie” only when direct address cannot be avoided. Do not change IDs, source values, hashes, or statuses manually.

- [ ] **Step 3: Import and inspect findings**

Run:

```bash
BATCH=$(python3 -c 'import json; print(json.load(open("translations/checkpoint.json"))["current_batch"])')
python3 -m scripts.idea_deu import-batch "$BATCH"
```

Expected: zero blocking findings. Correct the same batch and repeat import until this is true.

- [ ] **Step 4: Commit the completed batch and repeat**

```bash
git add translations glossary reports
SEQUENCE=$(python3 -c 'import json; print(json.load(open("translations/checkpoint.json"))["completed_sequence"])')
git commit -m "i18n: translate IntelliJ batch $SEQUENCE"
python3 -m scripts.idea_deu status
```

Repeat Steps 1–4 until status reports `offen: 0` and `blocking: 0`. The sequence number comes from `translations/checkpoint.json`; never invent it.

- [ ] **Step 5: Run global technical and terminology validation**

Run: `python3 -m scripts.idea_deu validate`

Expected: exit 0, `offen: 0`, `blocking: 0`. Review all warnings in `reports/status.md`; either correct them or record a specific accepted-warning reason in the affected unit.

- [ ] **Step 6: Mark the reviewed corpus and commit**

Run: `python3 -m scripts.idea_deu report`

Expected: every included unit is at least `technisch_geprüft`; the report contains no unexplained exclusions or warnings.

```bash
git add translations glossary reports
git commit -m "i18n: complete German IntelliJ translation corpus"
```

### Task 11: Plugin verification and documentation

**Files:**
- Create: `settings.gradle.kts`
- Create: `build.gradle.kts`
- Create: `gradle.properties`
- Create: `gradle/wrapper/gradle-wrapper.properties`
- Create: `gradlew`
- Create: `gradlew.bat`
- Create: `README.md`
- Create: `docs/acceptance-checklist.md`

- [ ] **Step 1: Add a minimal Gradle verification project**

Use IntelliJ Platform Gradle Plugin 2.x, Java 21, `intellijIdea("2025.3.1.1")`, `sinceBuild = "253.29346.240"`, and exact upper compatibility in the generated descriptor. Configure plugin verification against the locally unpacked target IDE when available, avoiding a second product download.

- [ ] **Step 2: Verify project configuration**

Run: `./gradlew verifyPluginProjectConfiguration`

Expected: BUILD SUCCESSFUL with no compatibility error.

- [ ] **Step 3: Build and verify the generated plugin**

Run:

```bash
python3 -m scripts.idea_deu generate
python3 -m scripts.idea_deu package
./gradlew verifyPlugin
```

Expected: `dist/idea-deu-2025.3.1.1.zip`, no pipeline blockers, and Plugin Verifier success.

- [ ] **Step 4: Document exact continuation and offline installation**

`README.md` must include prerequisites, source ZIP placement, every CLI command, status recovery after interruption, build verification, artifact checksum, Windows “Install Plugin from Disk”, language selection, restart, and rollback/uninstall. `docs/acceptance-checklist.md` must cover licensed and unlicensed modes plus Project, Editor, Search, Settings, Build, Run/Debug, Git, and premium-feature samples.

- [ ] **Step 5: Run final static verification and commit**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m scripts.idea_deu validate
git diff --check
```

Expected: all tests PASS, validation exits 0, and no whitespace errors.

```bash
git add settings.gradle.kts build.gradle.kts gradle.properties gradle gradlew gradlew.bat README.md docs/acceptance-checklist.md
git commit -m "docs: add offline build and acceptance workflow"
```

### Task 12: Windows air-gap acceptance and release evidence

**Files:**
- Modify: `docs/acceptance-checklist.md`
- Create: `reports/release-2025.3.1.1.md`
- Create: `dist/idea-deu-2025.3.1.1.zip.sha256`

- [ ] **Step 1: Produce final artifact and checksum**

Run:

```bash
python3 -m scripts.idea_deu package
shasum -a 256 dist/idea-deu-2025.3.1.1.zip > dist/idea-deu-2025.3.1.1.zip.sha256
```

Expected: artifact and matching checksum file.

- [ ] **Step 2: Perform clean Windows installation without network access**

Start an unmodified IntelliJ IDEA 2025.3.1.1 build 253.29346.240, disconnect Marketplace access, install the ZIP from disk, select German under Language and Region, and restart. Record IDE build, Windows version, plugin hash, and result in the checklist.

- [ ] **Step 3: Execute all acceptance checks in both license modes**

Mark each checklist item PASS/FAIL with a screenshot or concise observation. Any crash, unresolved English source without an exclusion, missing dependency, or broken placeholder is a release blocker and returns to the responsible earlier task.

- [ ] **Step 4: Write the release report**

The report must state source/build hashes, inventory and translation counts, exclusion counts by reason, zero unresolved collisions, zero blocking findings, remaining accepted warnings with reasons, Plugin Verifier result, and Windows acceptance results.

- [ ] **Step 5: Final verification and release commit**

Run: `git status --short --ignored`

Expected: only the source IntelliJ ZIP, `generated/`, and `dist/` are ignored; no uncommitted source, translation, report, or documentation changes remain after staging.

```bash
git add docs/acceptance-checklist.md reports/release-2025.3.1.1.md
git commit -m "release: verify German language pack 2025.3.1.1"
```

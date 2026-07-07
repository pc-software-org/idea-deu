# CLAUDE.md — idea-deu

Unofficial German (`de`) language pack for **IntelliJ IDEA Ultimate 2026.1.3**
(build `261.25134.95`). Dependency-light Python pipeline
(`python -m scripts.idea_deu`, stdlib only) → code-free `languageBundle` plugin.
JetBrains Marketplace **plugin id 32785**. Apache-2.0 (+ NOTICE). Remote:
`pc-software-org/idea-deu`, branch `main`.

## Non-obvious facts (read before changing things)

- **The ZIP builds without the 1.5 GB IDE archive.** `generate`/`package` use the
  committed `inventory/source-blobs/`. Only `validate-source`/`scan` need
  `idea-2026.1.3.win.zip` (git-ignored) in the repo root.
- **Version = single source of truth: `config/product.json` → `plugin_version`.**
  `plugin/META-INF/plugin.xml` is a template with `@PLUGIN_VERSION@`,
  `@SINCE_BUILD@`, `@UNTIL_BUILD@`, rendered by `package.py`. Don't hardcode the
  version elsewhere. Scheme `<ide-version>.<patch>` — a translation-only fix
  bumps the 4th segment (`2026.1.3.1` → `2026.1.3.2`).
- Build is deterministic (rebuild → identical SHA-256). The strict translation
  validator (`validation.py`) blocks on mnemonic (`&`/`_`) count, MessageFormat,
  markup/tag structure, placeholders. Prose `%` after a digit/`}` is not printf.
- Pseudo-tag sentinels (`<No Group>`, `<empty name>`) must stay verbatim; empty
  `.properties` bundles legitimately have zero units.

## Common tasks

- Test: `python -m unittest discover -s tests -q` (also `compileall`, `git diff --check`).
- Build: `python -m scripts.idea_deu generate && python -m scripts.idea_deu package`.
- Translate open units: `next-batch --limit 200` → fill each JSONL line's
  `target` → `import-batch <path>` → commit. Dispatch a subagent per batch.
- Release: bump `plugin_version`, commit to `main`, then
  `git tag v<plugin_version> && git push origin main v<plugin_version>`.
  CI (`.github/workflows/build.yml`) guards tag==version, builds, **signs**,
  GitHub-releases, and uploads to Marketplace.

## Signing (CRITICAL)

Release ZIPs are author-signed with `marketplace-zip-signer`. The **private key
lives only at `signing/private.pem` (git-ignored) and as secret `SIGN_KEY` —
sole copy; do not lose it.** Public cert `signing/chain.crt` (committed), secret
`SIGN_CERT`. Repo secrets set: `MARKETPLACE_TOKEN`, `SIGN_KEY`, `SIGN_CERT`.

## Guardrails

- Don't reintroduce a Gradle project (removed; it triggered GitHub's failing
  auto dependency submission). Verify with the standalone JetBrains Plugin
  Verifier — see `docs/plugin-verification.md`.
- Don't commit `signing/private.pem` or the IDE archive.
- The translations derive from JetBrains' proprietary strings — keep the
  "unofficial, not affiliated" framing (see `NOTICE`).

# Publishing to JetBrains Marketplace

The plugin is registered on JetBrains Marketplace as
**id 32785** — <https://plugins.jetbrains.com/plugin/32785-german-language-pack>.

## One-time setup

1. Sign in to the Marketplace with the account that owns plugin 32785.
2. Create a **permanent token**: Marketplace profile → **My Tokens** → generate.
3. In the GitHub repo: **Settings → Secrets and variables → Actions → New
   repository secret**, name it `MARKETPLACE_TOKEN`, paste the token.

Without the secret the release job still builds and publishes a GitHub Release;
it just logs a notice and skips the Marketplace upload.

## Versioning

The plugin version lives **only** in `config/product.json` → `plugin_version`
(`plugin.xml` is a template filled from it at build time). Scheme:
`<ide-version>.<patch>` — a translation-only fix bumps the 4th segment.

| Change | plugin_version |
|---|---|
| First release for 2026.1.3 | `2026.1.3.1` |
| Translation fix (same IDE) | `2026.1.3.2`, `2026.1.3.3`, … |
| Re-scan onto a new IDE (e.g. 2026.2) | `2026.2.0.1` |

A local build (`generate`/`package`) reads the version from config; **no git
tag is involved**, so offline builds are unaffected. The tag is only a release
marker, and CI fails the release if the tag does not equal `plugin_version`.

## Release a new version

```bash
# 1. bump the version (translation fix example)
#    edit config/product.json: "plugin_version": "2026.1.3.2"
git commit -am "release 2026.1.3.2"

# 2. tag it v<plugin_version> and push. build.yml verifies tag == plugin_version,
#    builds, creates a GitHub Release, and uploads to Marketplace (channel stable).
git tag v2026.1.3.2
git push origin main v2026.1.3.2
```

The upload uses the documented Marketplace API:

```
POST https://plugins.jetbrains.com/api/updates/upload
Authorization: Bearer <token>
-F pluginId=32785 -F file=@dist/idea-deu.zip -F channel=stable
```

After upload the version goes through JetBrains review before it appears on the
public page. Keep `pluginVersion` in `config/product.json` in step with the tag.

## Manual upload (fallback)

```bash
python3 -m scripts.idea_deu generate && python3 -m scripts.idea_deu package
curl -sS --fail-with-body -X POST \
  --header "Authorization: Bearer $MARKETPLACE_TOKEN" \
  -F pluginId=32785 -F file=@dist/idea-deu.zip -F channel=stable \
  https://plugins.jetbrains.com/api/updates/upload
```

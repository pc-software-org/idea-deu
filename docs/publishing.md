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

## Release a new version

```bash
# build.yml runs on the tag: builds, verifies, creates a GitHub Release, and
# uploads the ZIP to Marketplace (channel: stable).
git tag v2026.1.3
git push origin v2026.1.3
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

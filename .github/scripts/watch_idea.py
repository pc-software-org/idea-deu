#!/usr/bin/env python3
"""Open a GitHub issue when JetBrains ships an IDEA release newer than the bound one.

Compares the latest IntelliJ IDEA Ultimate release (JetBrains release API) with
`config/product.json` -> `version`. On a difference it files one issue carrying
the new version/build/download URL/checksum so the language pack can be
retargeted. De-duplicates on the exact issue title so a daily schedule never
piles up duplicates. Stdlib only; talks to GitHub via the `gh` CLI, passing all
untrusted API strings as subprocess list arguments (never a shell string)."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

API = "https://data.services.jetbrains.com/products/releases?code=IIU&latest=true&type=release"


def main() -> int:
    with urllib.request.urlopen(API, timeout=30) as response:  # noqa: S310 (trusted host)
        releases = json.load(response)["IIU"]
    if not releases:
        print("release API returned no IIU entries", file=sys.stderr)
        return 1
    release = releases[0]
    latest = release["version"]
    build = release.get("build", "")
    windows = release.get("downloads", {}).get("windowsZip", {})
    link = windows.get("link", "")
    checksum = windows.get("checksumLink", "")

    current = json.loads(Path("config/product.json").read_text())["version"]
    print(f"latest={latest} build={build} bound={current}")
    if latest == current:
        print("up to date — no issue")
        return 0

    title = f"IntelliJ IDEA {latest} verfügbar (gebunden: {current})"
    if title in _open_issue_titles():
        print("issue already open — nothing to do")
        return 0

    body = (
        f"Eine neue IntelliJ IDEA Ultimate-Version ist verfügbar.\n\n"
        f"| | |\n|---|---|\n"
        f"| Neue Version | `{latest}` |\n"
        f"| Build | `{build}` |\n"
        f"| Aktuell gebunden | `{current}` |\n\n"
        f"- Windows-ZIP: {link}\n"
        f"- SHA-256: {checksum}\n\n"
        f"**Sprachpaket aktualisieren:** `config/product.json` + `config.py "
        f"_EXACT_BUILD` neu binden, `validate-source`, `scan`, Delta übersetzen, "
        f"`CHANGELOG.md`-Abschnitt für die neue `plugin_version` anlegen, dann "
        f"`v<plugin_version>` taggen.\n\n"
        f"_Automatisch erstellt von `.github/workflows/watch-idea.yml`._\n"
    )
    Path("issue_body.md").write_text(body)
    subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body-file", "issue_body.md",
         "--label", "idea-update"],
        check=True,
    )
    print("issue created")
    return 0


def _open_issue_titles() -> set[str]:
    """Titles of currently open issues; ensures the `idea-update` label exists."""
    # Idempotent label create — ignore "already exists" so --label never fails.
    subprocess.run(
        ["gh", "label", "create", "idea-update", "--color", "1D76DB",
         "--description", "New IntelliJ IDEA release to translate"],
        capture_output=True, text=True,
    )
    listed = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--limit", "200",
         "--json", "title", "--jq", ".[].title"],
        capture_output=True, text=True, check=True,
    )
    return set(listed.stdout.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())

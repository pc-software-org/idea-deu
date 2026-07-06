# Plugin verification

The language pack is a **code-free resource plugin** built by the Python
pipeline into `dist/idea-deu.zip`, not by Gradle. Two independent checks apply.

## 1. Pipeline verification (always run, offline)

The pipeline itself enforces the release invariants and refuses to package
otherwise:

- every included unit is `technically_reviewed` with no blocking finding,
- no unresolved path collisions,
- exact descriptor identity and `since/until-build = 261.25134.95`,
- a byte-deterministic ZIP (repeat `generate` + `package` → identical SHA-256).

```bash
python3 -m scripts.idea_deu validate
python3 -m scripts.idea_deu generate
python3 -m scripts.idea_deu package
shasum -a 256 dist/idea-deu.zip   # compare to dist/idea-deu.zip.sha256
```

## 2. JetBrains Plugin Verifier (recommended for the built artifact)

Because the artifact is built outside Gradle, the most direct external check is
the **standalone** JetBrains Plugin Verifier run against `dist/idea-deu.zip` and
the unpacked target IDE. Requires JDK 21 and (once) network to fetch the
verifier and, if not already present, the IDE.

```bash
# verifier-cli-<ver>-all.jar from https://github.com/JetBrains/intellij-plugin-verifier/releases
java -jar verifier-cli-<ver>-all.jar check-plugin \
    dist/idea-deu.zip \
    /path/to/idea-IU-261.25134.95     # unpacked IntelliJ IDEA 2026.1.3
```

Expected: no compatibility problems (a language pack contributes only a
`languageBundle` extension and resource bundles; the verifier confirms the
descriptor and compatibility range).

## 3. Optional Gradle configuration check

`build.gradle.kts` (IntelliJ Platform Gradle Plugin 2.17.0) provides
`verifyPluginProjectConfiguration` and a `verifyPlugin` wired to IU-2026.1.3.
Generate the wrapper once (`gradle wrapper --gradle-version 8.14`), then:

```bash
./gradlew verifyPluginProjectConfiguration
./gradlew verifyPlugin
```

> These Gradle tasks were **not executed** in the environment that authored this
> repository (no JDK 21 / no network). Run them where both are available. For
> verifying the actual shipped artifact, prefer method 2.

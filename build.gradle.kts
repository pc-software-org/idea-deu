import org.jetbrains.intellij.platform.gradle.IntelliJPlatformType

// Verification-only project. It does not compile plugin code — the language
// pack is a code-free resource plugin built by the Python pipeline into
// dist/idea-deu.zip. This build exposes JetBrains' project-configuration and
// Plugin Verifier checks against the exact target platform.
//
// NOTE: not executed in the build environment used to author this repo (no JDK
// 21 / no network there). Run it where JDK 21 and network are available:
//   ./gradlew verifyPluginProjectConfiguration
//   ./gradlew verifyPlugin
// To verify the artifact the Python pipeline produced, prefer the standalone
// Plugin Verifier — see docs/plugin-verification.md.

plugins {
    id("org.jetbrains.intellij.platform") version "2.17.0"
}

group = providers.gradleProperty("pluginGroup").get()
version = providers.gradleProperty("pluginVersion").get()

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        create(
            IntelliJPlatformType.IntellijIdeaUltimate,
            providers.gradleProperty("platformVersion").get(),
        )
        pluginVerifier()
    }
}

intellijPlatform {
    pluginConfiguration {
        id = "org.pc-software.idea-deu"
        name = "German Language Pack"
        version = providers.gradleProperty("pluginVersion")
        ideaVersion {
            sinceBuild = providers.gradleProperty("pluginSinceBuild")
            untilBuild = providers.gradleProperty("pluginUntilBuild")
        }
    }
    pluginVerification {
        ides {
            ide(IntelliJPlatformType.IntellijIdeaUltimate, providers.gradleProperty("platformVersion").get())
        }
    }
}

kotlin {
    jvmToolchain(providers.gradleProperty("javaVersion").get().toInt())
}

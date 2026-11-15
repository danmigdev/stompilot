plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The app runs the desktop's Python package and interface unchanged: both are
// copied from the repository at build time. Build the interface first
// (frontend: npm run build); Build Android.cmd does both.
val repository: File = rootProject.projectDir.parentFile
val generatedPython = layout.buildDirectory.dir("generated/python")
val generatedAssets = layout.buildDirectory.dir("generated/assets")

val syncPythonPackage by tasks.registering(Sync::class) {
    from(repository.resolve("midi_configurator")) {
        exclude("test_*.py", "__pycache__/**", "*.pyc", "desktop.py")
        into("midi_configurator")
    }
    into(generatedPython)
}

val syncInterface by tasks.registering(Sync::class) {
    val build = repository.resolve("frontend/build")
    doFirst {
        if (!build.resolve("index.html").isFile) {
            throw GradleException("Build the interface first: cd frontend, then npm run build.")
        }
    }
    from(build)
    into(generatedAssets.map { it.dir("web") })
}

android {
    namespace = "app.stompilot"
    compileSdk = 35
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "app.stompilot"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "0.4.2"
        ndk {
            // Phones are arm64. Each ABI adds a Python runtime, so the emulator's
            // x86_64 is built only on request: gradlew assembleDebug -Pabis=x86_64
            abiFilters += (findProperty("abis") as String? ?: "arm64-v8a").split(",")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    sourceSets {
        getByName("main") {
            assets.srcDir(generatedAssets)
        }
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        // Keep releases independent of the build host's Python bytecode version.
        pyc {
            src = false
        }
        // Profiles are read from files beside the package.
        extractPackages("midi_configurator")
    }
    sourceSets {
        getByName("main") {
            srcDir(generatedPython)
        }
    }
}

tasks.named("preBuild") {
    dependsOn(syncPythonPackage, syncInterface)
}
// Tasks that read the copies must say so, or Gradle rejects the build.
tasks.matching { it.name.endsWith("PythonSources") }.configureEach { dependsOn(syncPythonPackage) }
tasks.matching { it.name.endsWith("Assets") }.configureEach { dependsOn(syncInterface) }

// Chaquopy's asset hashes are correct, but their JSON key order can vary between
// filesystems/JDKs. Canonicalize the generated manifest before APK packaging so
// F-Droid can reproduce the release and retain the upstream signing certificate.
tasks.matching { it.name.endsWith("PythonBuildAssets") }.configureEach {
    doLast {
        val manifest = outputs.files.asFileTree.single { it.name == "build.json" }
        @Suppress("UNCHECKED_CAST")
        val data = groovy.json.JsonSlurper().parse(manifest) as Map<String, Any?>
        val ordered = data.toSortedMap().mapValues { (key, value) ->
            @Suppress("UNCHECKED_CAST")
            if (key == "assets") (value as Map<String, String>).toSortedMap() else value
        }
        manifest.writeText(groovy.json.JsonOutput.prettyPrint(groovy.json.JsonOutput.toJson(ordered)) + "\n")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    testImplementation("junit:junit:4.13.2")
}

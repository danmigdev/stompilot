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

    defaultConfig {
        applicationId = "app.stompilot"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.4.0"
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

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    testImplementation("junit:junit:4.13.2")
}

// swift-tools-version: 6.2
// A2A macOS alpha: libraries, CLIs, and deterministic tests.
// Build with full Xcode selected (see AGENTS.md); the Command Line Tools
// SwiftPM install on the reference Mac cannot link package manifests.
import PackageDescription

let deploymentTarget = "26.0"

/// Embed a tool's Info.plist so TCC and system services see a stable bundle identity.
func embeddedInfoPlist(_ tool: String) -> [LinkerSetting] {
    [.unsafeFlags(["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                   "-Xlinker", Context.packageDirectory + "/Tools/\(tool)/Info.plist"])]
}

/// Swift Build records the deployment target as the linked SDK version (`sdk 26.0` in
/// LC_BUILD_VERSION), and system frameworks gate behavior on it: Translation then reports
/// zh→en as `supported`/`notInstalled` although the languages are installed
/// (reports/week2/verifications.md). scripts/env.sh exports the real SDK version, and
/// scripts/check.sh fails when a built executable does not carry it.
func sdkVersionStamp() -> [LinkerSetting] {
    guard let sdk = Context.environment["A2A_SDK_VERSION"], !sdk.isEmpty else { return [] }
    return [.unsafeFlags(["-Xlinker", "-platform_version", "-Xlinker", "macos",
                          "-Xlinker", deploymentTarget, "-Xlinker", sdk])]
}

let package = Package(
    name: "A2A",
    platforms: [.macOS(deploymentTarget)],
    products: [
        .library(name: "A2ACore", targets: ["A2ACore"]),
        .library(name: "A2ATrace", targets: ["A2ATrace"]),
        .library(name: "A2AAudio", targets: ["A2AAudio"]),
        .library(name: "A2AModels", targets: ["A2AModels"]),
        .library(name: "A2AMedia", targets: ["A2AMedia"]),
        .executable(name: "a2a-tapctl", targets: ["a2a-tapctl"]),
        .executable(name: "a2a-pipeline", targets: ["a2a-pipeline"]),
        .executable(name: "a2a-preflight", targets: ["a2a-preflight"]),
        .executable(name: "a2a-transcribe", targets: ["a2a-transcribe"]),
        .executable(name: "a2a-stage-probe", targets: ["a2a-stage-probe"]),
        .executable(name: "FixturePlayer", targets: ["FixturePlayer"]),
        .executable(name: "A2A", targets: ["A2AApp"]),
    ],
    targets: [
        // Pure policies and types. No I/O, no Apple media frameworks, no models.
        .target(name: "A2ACore"),

        // C shim for real-time paths: SPSC rings, timing records, IOProc, mixer.
        .target(name: "A2ARTShim"),

        // WAV files, resampling, and signal measurements (non-RT, Foundation only).
        .target(name: "A2AMedia"),

        // JSONL serialization and the non-RT drain of RT trace rings.
        .target(name: "A2ATrace", dependencies: ["A2ACore", "A2ARTShim"]),

        // Process taps, aggregate devices, capture writer, output mixer.
        .target(name: "A2AAudio", dependencies: ["A2ACore", "A2ARTShim", "A2ATrace", "A2AMedia"]),

        // Model adapters: deterministic fixtures plus Apple system frameworks.
        // Third-party model packages are added per selected combination (T0.2/T0.3).
        .target(name: "A2AModels", dependencies: ["A2ACore", "A2AMedia"]),

        // Delayed picture of the source window (ADR-0012): capture, hardware encode, and
        // presentation on a host-clock timebase. App only; frames never leave memory.
        .target(name: "A2AVideo"),

        // Offline comparison tools only; never linked into the app or live pipeline.
        .target(name: "A2ALegacyModels", dependencies: ["A2ACore", "A2AModels"]),

        .executableTarget(
            name: "a2a-tapctl",
            dependencies: ["A2ACore", "A2AAudio", "A2ATrace", "A2AMedia", "A2ARTShim"],
            path: "Tools/a2a-tapctl",
            exclude: ["Info.plist"],
            linkerSettings: embeddedInfoPlist("a2a-tapctl") + sdkVersionStamp()
        ),
        .executableTarget(
            name: "a2a-loopprobe",
            dependencies: ["A2AAudio", "A2AMedia", "A2ARTShim"],
            path: "Tools/a2a-loopprobe",
            exclude: ["Info.plist"],
            linkerSettings: embeddedInfoPlist("a2a-loopprobe") + sdkVersionStamp()
        ),
        .executableTarget(
            name: "a2a-pipeline",
            dependencies: ["A2ACore", "A2AModels", "A2ATrace", "A2AMedia", "A2ARTShim", "A2AAudio"],
            path: "Tools/a2a-pipeline",
            exclude: ["Info.plist"],
            linkerSettings: embeddedInfoPlist("a2a-pipeline") + sdkVersionStamp()
        ),
        .executableTarget(
            name: "a2a-preflight",
            dependencies: ["A2AAudio", "A2AModels"],
            path: "Tools/a2a-preflight",
            exclude: ["Info.plist"],
            linkerSettings: embeddedInfoPlist("a2a-preflight") + sdkVersionStamp()
        ),
        .executableTarget(
            name: "a2a-transcribe",
            dependencies: ["A2ACore", "A2AMedia", "A2AModels", "A2ALegacyModels"],
            path: "Tools/a2a-transcribe",
            exclude: ["Info.plist"],
            linkerSettings: embeddedInfoPlist("a2a-transcribe") + sdkVersionStamp()
        ),
        .executableTarget(
            name: "a2a-stage-probe", dependencies: ["A2ACore", "A2AMedia", "A2AModels", "A2ALegacyModels"],
            path: "Tools/a2a-stage-probe", exclude: ["Info.plist"],
            linkerSettings: embeddedInfoPlist("a2a-stage-probe") + sdkVersionStamp()
        ),
        // Tap target for harness and lifecycle tests. scripts/build_fixture_player.sh
        // wraps the binary in a signed .app with a stable bundle identifier.
        .executableTarget(
            name: "FixturePlayer",
            dependencies: ["A2AMedia"],
            path: "Apps/FixturePlayer",
            exclude: ["Info.plist"],
            linkerSettings: sdkVersionStamp()
        ),

        .testTarget(name: "A2ACoreTests", dependencies: ["A2ACore"], exclude: ["Golden"]),
        .executableTarget(
            name: "A2AApp", dependencies: ["A2ACore", "A2AAudio", "A2AMedia", "A2AModels", "A2AVideo"],
            path: "Apps/A2A", exclude: ["Info.plist"], linkerSettings: sdkVersionStamp()
        ),
        .testTarget(name: "A2ATraceTests", dependencies: ["A2ATrace", "A2ACore"]),
        .testTarget(name: "A2AAudioTests", dependencies: ["A2AAudio", "A2ARTShim"]),
        .testTarget(name: "A2AMediaTests", dependencies: ["A2AMedia"]),
        .testTarget(name: "A2AVideoTests", dependencies: ["A2AVideo"]),
        .testTarget(name: "A2AModelsTests", dependencies: ["A2AModels", "A2ACore", "A2ALegacyModels"]),
    ]
)

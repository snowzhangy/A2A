**T0.3 runtime verifications — 24 October 2026**

This file records only checks that were actually run on this Mac, with their limits. Anything not run is listed as pending, together with what blocks it. Raw preflight output is in [raw/preflight.json](raw/preflight.json), and the command is `a2a-preflight --json reports/week2/raw/preflight.json --translate-probe`.

## Host and toolchain

| Check | Result | Limit |
| --- | --- | --- |
| Hardware | Apple M3 Pro (Mac15,6), 36 GB unified memory, 5 performance + 6 efficiency cores. Metal `recommendedMaxWorkingSetSize` 30.2 GB. | Above the D5 24–32 GB band. The 16 GB low-memory probe Mac is not provisioned. |
| OS | macOS 27.0.1 (26A434) | The macOS 26.0 deployment target has not been run on a macOS 26 machine (D4). |
| Toolchain | Xcode 27.0 (27A266a), Swift 6.4, MacOSX SDK 27.0 (26A425), uv 0.11.7 | `xcode-select` points at Command Line Tools. That SwiftPM fails to link any package manifest (`Undefined symbols … PackageDescription.Package.__allocating_init`), so every script exports `DEVELOPER_DIR` to Xcode (`scripts/env.sh`). |
| Metal compiler | Available through Xcode (`xcrun --find metal` resolves to the MetalToolchain cryptex) | Needed when MLX packages are added. No MLX build has been attempted. |
| Signing | Ad-hoc signing with hardened runtime works (`scripts/build_fixture_player.sh`). A valid Apple Development identity is in the keychain (an older one is revoked). | No Developer ID identity, so notarization is untested. |

## Speech (SpeechAnalyzer / SpeechTranscriber)

| Check | Result | Limit |
| --- | --- | --- |
| Availability | `SpeechTranscriber.isAvailable == true` | — |
| zh-CN support | `supportedLocale(equivalentTo: zh-CN) = zh_CN`. zh_HK and zh_TW are also supported (45 locales total). | — |
| zh asset | Installed on 24 October through the setup path (`a2a-preflight --install-speech-assets`: `AssetInventory.assetInstallationRequest(...).downloadAndInstall()`), then `AssetInventory.status = installed`. Analyzer format: 16 kHz mono Int16. | — |
| Per-app reservation | Assets are reserved per app (`maximumReservedLocales = 5`). After the install, `dev.a2a.preflight` saw `installed` but `dev.a2a.pipeline` still saw `supported`. A probe with another identity showed `reserve(locale:)` returns true, the status becomes `installed`, and `assetInstallationRequest` returns nil (nothing to download). `AppleSpeechRecognizer.checkInstalled` now reserves, and releases plus refuses if a download would still be needed (D8). | Reservations accumulate per tool identity (pipeline, transcribe, scratch probes). |

## Translation framework

| Check | Result | Limit |
| --- | --- | --- |
| Basic initializer | `TranslationSession(installedSource:target:)` compiles ungated for a macOS 26.0 deployment target | The SDK evidence from the planning review still holds. |
| zh-Hans→en from the CLI | **Resolved, 24 October.** Root cause: the executable's recorded SDK version. Swift Build (SwiftPM's default engine) writes the deployment target as the SDK version (`LC_BUILD_VERSION` `minos 26.0`, `sdk 26.0`); `swiftc` and the deprecated native build system write `sdk 27.0`. With identical code, a 27.0-stamped binary reports `installed` and translates; the same probe re-linked with `-platform_version macos 26.0 26.0` reports `supported` and throws `notInstalled`. Signing (ad-hoc, Apple Development team ID), entitlements (`get-task-allow`), code-signing identifier, bundle ID, executable path, and prior Speech/Metal calls were each ruled out. Fix: `sdkVersionStamp()` in `apple/Package.swift`; `scripts/check.sh` fails when an executable's SDK differs from `xcrun --show-sdk-version`. Now `a2a-preflight --translate-probe` passes: first call 1,035 ms (includes load), warm clause 305 ms. | The xctest host worked because it is built by Xcode with the real SDK. |
| zh-Hans→en from an app bundle | The ad-hoc-signed, hardened-runtime `.local/apps/A2APreflight.app` (`dev.a2a.preflight`) returned the same `supported` + `notInstalled`, both when executed directly and when launched with `open` | Rules out bundle identity and LaunchServices launch as the cause |
| zh-Hans→en from the Xcode test host | Under `com.apple.xctest` (an Apple platform binary), `checkInstalled()` passed. "我们下周开会。" translated to "We will have a meeting next week." (`AppleAdapterContractTests`) | **Open:** availability differs by process, and no denial appeared in the unified log. Hypotheses: language assets scoped to an identity or signature, or system models reachable only by Apple binaries. Next, install zh→en from System Settings › General › Language & Region › Translation Languages and re-run. Then test a Developer ID–signed build. Until then `a2a-pipeline --mt apple` is expected to refuse with `assetsNotInstalled`. |

## Core Audio

| Check | Result | Limit |
| --- | --- | --- |
| Process objects | `kAudioHardwarePropertyProcessObjectList` is readable without capture permission (35 objects). `a2a-tapctl resolve --bundle dev.a2a.FixturePlayer` returned the FixturePlayer process (object 164, running output). | Zoom was not running. Helper resolution was tested only with synthetic process lists (`SourceResolverTests`). |
| Default output | MacBook Pro Speakers, 48 kHz, 512-frame buffer, latency 70 frames, safety offset 74 frames. A DELL U3415W display output is also present. | Wired headphones were not connected, so this is not the D3 acceptance route. |
| Tap and aggregate creation | `AudioHardwareCreateProcessTap` (muted-when-tapped, private) and `AudioHardwareCreateAggregateDevice` both returned success: `TapSession.init` completed and execution reached `start()`. | — |
| Start without consent | `AudioDeviceStart` blocked for more than 60 s in `HAL_HardwarePlugIn_DeviceStart` → `mach_msg` (seen with `sample`), consistent with a pending System Audio Recording consent prompt for the terminal. The host was then terminated (SIGTERM, no teardown). A later `a2a-tapctl taps` listing was empty, but that proves nothing: the tap was private, so it is visible only to its creator. | The prompt was not answered. Capture content, mute audibility, re-render latency, the 30-minute fidelity run, and the A3 lifecycle (`a2a-tapctl lifecycle`) remain pending on the interactive runner once permission is granted. |
| Capture permission | `a2a-tapctl permission` (private `TCCAccessPreflight`, `kTCCServiceAudioCapture`) returns `granted` for the responsible process, Terminal.app. Terminal's Info.plist has no `NSAudioCaptureUsageDescription`. | The owner saw no consent dialog. When and how the grant was recorded is unknown (possibly the earlier blocked start). Denied/revoked/regranted cases are still untested. |
| Live tap format | Tap 48 kHz, 2 channels, input channel offset 0 in the aggregate. Aggregate input latency 0.69 ms plus 1.02 ms safety offset; output buffer 10.67 ms, output latency 1.46 ms plus 1.54 ms safety offset (`latency` in `raw/a1-capture.json`). | Reported HAL values, not a loopback measurement. |
| A1 pilot, 60 s (`.local/runs/a1-pilot`) | Correlation 0.99995 after alignment, minimum 10 s window 0.99989, 0 dropouts, 0 ring overflow frames, 0 rejected blocks, 0 record drops, 0 invalid timestamps. 2,878,976 of 2,880,000 expected frames: the first callback arrives about 21 ms after the clock starts. | Unmuted run (see the mute row). The 30-minute DoD run is recorded in `status.md`. |
| Tap teardown (A3, `raw/a3.json`) | 50 stop/start cycles on a private muted-when-tapped tap: every tap was visible before teardown and left `kAudioHardwarePropertyTapList` 7 ms (median) / 9 ms (max) after `AudioHardwareDestroyProcessTap`. Destruction is asynchronous: an immediate re-check still listed 25 of 50 taps (`raw/a3-run1.json`), which the first lifecycle build misreported as leaks. | — |
| Host killed while muted (A3) | `kill -9` of a child holding a **public** muted-when-tapped tap: coreaudiod did not destroy the tap in 5 of 5 cycles. In run 1 the taps were still registered more than 60 s later, after FixturePlayer itself had exited. A separate process destroyed each one (`AudioHardwareDestroyProcessTap` status 0, released within 2 s, 5 of 5 in run 2). | Private taps (the product's choice) are invisible to other processes, so their fate after host death, and whether the source stays muted, needs an output loopback test. |
| Live tap of a player app | `a2a-pipeline --tap-pid <ffplay>` resolved ffplay (no bundle ID), muted it while tapped, and re-rendered it through the mixer at 48 kHz on the MacBook Pro speakers: 0 capture overflow and 0 record drops in every run. What the pipeline heard matched the clip exactly after alignment (correlation 1.0). End-to-end results are in `status.md`. | QuickTime Player, Safari, and Zoom as tap targets are untested. |
| Tap mute behaviors | The SDK still exposes unmuted / muted / muted-when-tapped. The tools use `.mutedWhenTapped` for monitor, capture, and crash tests. Before 24 October, `capture --mute` silently fell back to `unmuted` on an unknown value (the 60 s pilot was unmuted because of this); it now rejects unknown values. | Whether the source is actually silent at the device output is unverified: a tap sees audio before the mute is applied, so this needs an output loopback. |

## Not run

| Item (T0.3) | Blocker |
| --- | --- |
| TranslateGemma accepts `zh` as `source_lang_code` | Weights not fetched. The manifest row is `declared`. |
| speech-swift streaming capabilities (ASR streaming vs repeated window, TTS audio-out vs incremental text) | Dependency not added yet. Pin its revision first (T0.2). |
| MLX load, inference, and cancellation inside a signed bundled XPC service | Needs an Xcode app + XPC target and the MLX package. Not started. |
| Actual ANE/GPU placement | Needs per-model profiling once models exist |

**Original / Translated playback switch — 25 October 2026**

The app now exposes **Listen to → Original / Translated**, available before starting and during a session. Translated is the default. Original selects the captured source bus; recognition, captions, synthesis, and voice conditioning keep running. Switching back uses the existing translation session. Gain changes use the mixer's existing 30 ms ramps, with no tap restart or callback-path changes.

The shared reducer owns the selection, so later handoffs, recovery, or device resets cannot override an explicit Original choice. Translated retains the prior silence-during-preparation/recovery policy. A bounded latest-value stdin channel connects the app to its helper, including clicks during model warm-up. Stdin audio and control are mutually exclusive. Muted or interrupted English units are excluded from complete-translation playback counts.

Verification:

| Command/check | Result |
| --- | --- |
| `scripts/check.sh --quick` — build and executable SDK stamps | Pass: every executable records SDK 27.0 |
| Same check — Swift suite | 106 deterministic tests passed; two Apple-runtime tests skipped as intended |
| Same check — manifest gate | 10 entries, one enabled, zero problems |
| Same check — quick harness | 33 passed; six existing fetch tests failed during local-server setup: `PermissionError: [Errno 1] Operation not permitted` at `socket.bind(("127.0.0.1", 0))` |
| `uv run --project harness pytest harness/tests/test_playback_control.py -q` with `A2A_PIPELINE` pointing to the rebuilt helper | Both passed, 3.28 s; synthetic file input and fixture adapters, no capture/models/audio-device playback |
| `scripts/build_app.sh` | Pass; updated `.local/apps/A2A.app` and bundled helper |
| `codesign --verify --deep --strict .local/apps/A2A.app` | Pass |
| `scripts/smoke_app.sh` | Could not open the native window here: LaunchServices `-10827`, `kLSNoExecutableErr` |

The rendered-audio test sends Original and Translated commands over the helper's actual stdin pipe. It checks the returned gain decisions, confirms source samples appear in the rendered WAV under Original, and verifies the output returns to silence under Translated when English is unavailable. Core tests additionally cover both directions during playback, preserved voice/generation, recovery/device-reset behavior, and incomplete-unit accounting.

The app executable exists, has execute permission, is an arm64 Mach-O, matches `CFBundleExecutable`, and passes signature verification. A direct synthetic-UI launch also aborted before window creation; its faulting stack is in macOS `_RegisterApplication` / `___RegisterApplication_block_invoke`. No native window or physical-audio verification is claimed for this environment.

Builds used workspace-local `CLANG_MODULE_CACHE_PATH`, `SWIFTPM_MODULECACHE_OVERRIDE`, and `UV_CACHE_DIR`. Swift build/test were invoked with `--disable-sandbox` because SwiftPM's nested manifest sandbox cannot initialize inside the managed sandbox (`sandbox_apply: Operation not permitted`). An initial test compile attempted to use an internal `BusGains` initializer; the tests now inspect the public gain values and pass. The check script also drains SDK-inspection output fully to avoid an early `awk` exit causing SIGPIPE under `pipefail`.

No model downloads, real participant audio, live tap run, voice-identity qualification, or external release were performed.

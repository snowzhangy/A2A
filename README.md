# A2A

A2A translates Mandarin ↔ English audio playing on an Apple silicon Mac. It uses
Hy-SimulMT for speech translation and Qwen3-TTS for an experimental voice matched
to the speaker. A2A is an **early source release**: voice identity, translation
coverage, signing and distribution are not yet qualified for production.
The native application below targets macOS. An experimental Windows desktop preview is
under `runtime/`; it has not passed live-audio lifecycle or voice-quality acceptance.
See [the portable runtime guide](runtime/README.md) for setup, measured results, and
current limitations. `python scripts/check.py` runs the deterministic checks on either platform.

The app captures the **whole system output mix**, including other apps, and excludes
its own playback. In local mode, inference runs on the Mac; choosing a Hy server
sends captured audio to that server. Use voice matching only with speakers who have
consented. The [Windows preview](runtime/README.md) is a separate experiment.

## Get started

You need macOS 26 or later on Apple silicon, full Xcode with its macOS SDK, Git,
[CMake](https://cmake.org/) and [uv](https://docs.astral.sh/uv/). Keep the source
checkout: the app uses its `research/` environment and `.local/` model cache.
The local Hy model needs about 19.7 GB of disk space; the voice and language models
need roughly another 2 GB. Sufficient unified memory and free disk space are needed.

Build the app from a checkout or the release source archive:

```sh
scripts/build_app.sh
open .local/apps/A2A.app
```

Choose **On this Mac** and click **Set up models**. This explicit setup installs
Python dependencies, builds the local Hy worker, and downloads and verifies Hy,
Qwen3-TTS and language identification models. It can take a while; setup can be
canceled and retried. For an existing Hy server, select **Server**, enter its URL,
click **Check**, and use **Set up models** to install only the local voice and
language models. No models are downloaded just by starting a translation session.

The same setup is available in a terminal:

```sh
scripts/setup.sh            # local Hy and voice models
scripts/setup.sh --server   # voice and language models for a Hy server
```

The server address defaults to `http://127.0.0.1:8082`; change it to the reachable
address of your Hy server if it runs on another machine. Existing saved addresses
are retained. Setup and downloads are explicit, resumable and verified using the
pinned entries in [models/manifest.yaml](models/manifest.yaml).

Choose a language direction and **One** speaker for a single person, or **Multiple**
for a conversation. Click **Start translation** and allow macOS System Audio
Recording when prompted. **Original / Translated** switches what you hear without
stopping translation. Speaker tracking and cross-language voice similarity can be
wrong; one-speaker mode keeps one voice stable rather than detecting other speakers.
Use **Reset** next to the speaker status to restart translation and learn the voices
again. This clears any selected WAV reference and briefly interrupts translation;
the picture window and playback settings stay in place.

Under **Playback settings**, you can keep music and effects, choose a delayed video
window, or buffer complete phrases before playback. The app sends the full validated
translation to the voice worker, with a longer bounded speech budget so slower voices
can finish sentence endings. A clean 1–15 second WAV of the intended
speaker can optionally be supplied for a single-speaker session; it is used locally
and is not saved by the app. Voice matching is not guaranteed. Longer translations
may play late or be skipped when the session falls behind; model generation also remains bounded.
A delayed picture needs Screen Recording access, and protected video may appear black.
At a 3-second picture delay, shorter audio segments can increase translation speed but
may split phrases, and the original-voice fallback is unavailable. **Use less memory
for voices** runs one Qwen worker instead of two, which can increase delays in dialogue.

Releases include source, an optimized macOS development app, and an experimental
[Windows 11 x64 desktop bundle](docs/releases/windows-development-build.md). The Mac app needs
the matching checkout and local model setup; it is not a self-contained installer and
includes no model weights. See the [build installation instructions](docs/releases/macos-development-build.md).
The development build is ad-hoc signed and is not notarized. No top-level project
license has been declared; model and dependency provenance and licenses are recorded
in the manifest.

## Development and validation

```sh
scripts/check.sh
scripts/smoke_app.sh
```

To package an optimized macOS build from a clean, tagged checkout:

```sh
scripts/package_release.sh v0.2.0-alpha.2
```

The archive and its SHA-256 checksum are written under `.local/releases/` for upload
as GitHub release assets. Model caches, recordings, voice profiles and diagnostics
stay out of the archive.

The checks cover deterministic Swift and harness tests. Live capture, voice identity,
translation accuracy, device behavior, and signing need separate interactive tests.
See [AGENTS.md](AGENTS.md) for development commands and data handling, the
[implementation plan](IMPLEMENTATION_PLAN.md), and the [status report](reports/week2/status.md).

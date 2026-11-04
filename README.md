# A2A

A2A translates Mandarin ↔ English audio playing on an Apple silicon Mac. It uses
Hy-SimulMT for speech translation and Qwen3-TTS for an experimental voice matched
to the speaker. A2A is an **early source release**: voice identity, translation
coverage, signing and distribution are not yet qualified for production.

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

Under **Playback settings**, you can keep music and effects, choose a delayed video
window, or prioritize complete phrases. A clean 1–15 second WAV of the intended
speaker can optionally be supplied for a single-speaker session; it is used locally
and is not saved by the app. Voice matching is not guaranteed. Longer translations
may play late or be skipped, and captions can contain more text than the speech.
A delayed picture needs Screen Recording access, and protected video may appear black.

This release contains source, not a self-contained installer or model weights. The
local build is ad-hoc signed by default and is not notarized. No top-level project
license has been declared; model and dependency provenance and licenses are recorded
in the manifest.

## Development and validation

```sh
scripts/check.sh
scripts/smoke_app.sh
```

The checks cover deterministic Swift and harness tests. Live capture, voice identity,
translation accuracy, device behavior, and signing need separate interactive tests.
See [AGENTS.md](AGENTS.md) for development commands and data handling, the
[implementation plan](IMPLEMENTATION_PLAN.md), and the [status report](reports/week2/status.md).

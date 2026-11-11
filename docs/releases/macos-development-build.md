# macOS development build

The release archive contains an optimized Apple silicon `A2A.app`, these instructions,
and `BUILD-INFO.json` identifying the source tag, commit and SDK. It requires macOS 26
or later and the matching A2A checkout. Models and Python environments are installed
separately; they are not bundled in the archive.

Install the prerequisites in the release's [README](https://github.com/snowzhangy/A2A/blob/v0.2.0-alpha.2/README.md), then check out
the release tag, for example:

```sh
git clone https://github.com/snowzhangy/A2A.git
cd A2A
git checkout v0.2.0-alpha.2
mkdir -p .local/apps
```

Extract the downloaded archive and copy its `A2A.app` into this checkout's
`.local/apps/` directory. Keep it inside the checkout so it can find the voice worker
and model cache. Complete explicit setup before starting translation:

```sh
scripts/setup.sh            # Hy on this Mac, plus voice and language models
# scripts/setup.sh --server # voice and language models for an existing Hy server
open .local/apps/A2A.app
```

The app captures the whole system output mix. Voice matching requires the speakers'
consent. Model downloads occur only during setup. Selecting a Hy server sends source
audio to that server; selecting local Hy keeps inference on the Mac.

This is an ad-hoc signed research development build, not a notarized installer.
macOS may block a downloaded build; building from the same checkout with
`scripts/build_app.sh` produces the local development app. Translation coverage,
speaker identity and device behavior are not production-qualified.

To reproduce the archive from a clean checkout with the tag at `HEAD`:

```sh
scripts/check.sh
scripts/package_release.sh v0.2.0-alpha.2
```

Only the app, installation instructions and build metadata are packaged. Source is
available separately on the release page. The package excludes models, participant
audio, captions, traces, saved voices and machine preferences.

# Portable runtime (experimental)

The Windows desktop preview uses WASAPI process loopback, the shared C mixer, and a Python
port of the policy core. The macOS app remains the supported development path in the root README.
The portable CLI also supports WAV replay on macOS, but live Mac capture uses the Swift app.
The Windows runtime is not yet a release-ready replacement: source-volume recovery and
the full live pipeline have not been qualified on interactive devices.

Install Python 3.12 or 3.13, `uv`, Visual Studio with the C++ desktop workload and a
Windows SDK supporting process loopback. From the repository root on Windows:

```powershell
uv sync --project runtime --locked --extra gpu
uv run --project runtime python runtime/native/build.py
uv run --project runtime python -m pytest runtime/tests
uv run --project runtime a2a-wintap devices
```

After setup, double-click `A2A-Windows.bat` in the repository root. Play audio in the
source app, press **Refresh**, select its audio session, and choose a direction and output
device. **Translated voice** uses OmniVoice, and **Captions only** leaves the source audio
at its normal volume. The listen selector switches between original and translated audio
while a voice session is running. **Stop** ends the session and clears captions.

The interface starts models locally and has no download button. If a model is missing,
it reports the explicit fetch command below. The GPU setting can be Automatic, NVIDIA GPU,
or CPU; the CLI below also supports per-stage choices. Windows source-volume recovery and
live voice quality are still experimental.

Install the explicitly named, enabled model artifacts separately. Fetching can require
many gigabytes; startup itself does not download weights:

```powershell
uv run --project harness python scripts/fetch_models.py --models faster-whisper-large-v3,qwen3-4b-instruct-2507,omnivoice-0.6b-torch
```

The following is a controlled **file-mode experiment**, using the installed models and
an appropriately consented source WAV. Put all output under `.local/` or restricted storage:

```powershell
uv run --project runtime a2a-pipeline-portable --input .local/input.wav --clock host --pace realtime --asr whisper --mt llm --tts clone --device auto --tail-seconds 90 --trace .local/trace.jsonl --out .local/translated.wav
```

`--device auto` chooses CUDA for Whisper when available and CUDA/MPS/CPU for translation
and cloning. When `nvidia-smi` reports insufficient free memory for the weights and a
reserve, Whisper or Qwen automatically uses the CPU. This is an estimate, not a
reservation; another application can still exhaust GPU memory. Faster-whisper uses CPU
on Apple silicon; it does not have an MPS backend.

You can set each stage separately. For example, when other applications are using the
GPU, keep translation and the voice on CUDA and put recognition on the CPU:

```powershell
uv run --project runtime a2a-pipeline-portable --input .local/input.wav --clock host --pace realtime --asr whisper --asr-device cpu --mt llm --mt-device cuda --tts clone --tts-device cuda --fluent --tail-seconds 90 --trace .local/trace.jsonl --out .local/translated.wav
```

Use `--asr-device cuda`, `--mt-device cpu`, or `--tts-device cpu` to override another
stage. CPU inference can be much slower and still needs substantial system memory. The
three models share resources and are not yet qualified for live latency. To smoke-test the installed models independently, opt in with
`$env:A2A_RUN_MODEL_TESTS='1'` and run `uv run --project runtime pytest runtime/tests/test_model_runtime.py`.
The bounded tail allows in-flight recognition and speech to finish after input ends. If
recognition is still pending at the limit, the process reports failure; increase the
limit or use a faster device.

To try a live source on Windows, first list audio sessions and note the PID of the
application that is actually playing speech:

```powershell
uv run --project runtime a2a-wintap list
```

With the source playing, this captions-only check captures that process tree and does not
duck its audio. Replace `1234` with the source PID; captions appear in the terminal:

```powershell
uv run --project runtime a2a-pipeline-portable --tap-pid 1234 --captions-only --events-stdout --asr whisper --mt llm --tts tone --trace .local/captions-trace.jsonl
```

For an experimental translated-audio test with the cloned voice, use a source and speaker
you have permission to process. This mode ducks the source while A2A renders audio; Ctrl-C
stops the session and attempts to restore its volume:

```powershell
uv run --project runtime a2a-pipeline-portable --tap-pid 1234 --asr whisper --mt llm --tts clone --fluent --speakers auto --trace .local/live-trace.jsonl
```

On a memory-constrained GPU, add `--asr-device cpu` or other stage overrides as above.
Models load before capture starts, so leave the source running during startup. If a
session terminates abnormally and leaves a source quiet, run
`uv run --project runtime a2a-wintap restore` and check the Windows volume mixer.

The current portable runtime has no system-voice or Silero adapter and no saved voice
profiles. The voice clone is research-only and is not qualified for
speaker identity. The Windows capture tool can enumerate endpoints, but live translated
playback ducks the source application's session volume and has not passed crash and
device-change acceptance. Do not treat its fallback or guard as a verified safety
guarantee. See [ADR-0007](../docs/adr/0007-portable-runtime-experiment.md).

Dub Anything is an offline, agent-directed video dubbing workflow with line-level
directing and QA; this runtime is a live interpreter. Similar voice, translation, and
delivery quality cannot be inferred from using related models. A comparison requires
consented evaluation audio, independent transcription and speaker-similarity metrics,
listening tests, and measured latency under concurrent load.

# Research (Python, uv)

This directory holds model benchmarks (M1), PyTorch parity runs, the voice-reference study (M2), and the heavy evaluation models that `harness` deliberately excludes: COMET (wmt22-comet-da), Whisper-large-v3 back-transcription, UTMOS, and an independent WavLM-based speaker verifier.

This directory has its own locked uv project. Ordinary `scripts/check.sh` checks do not resolve these dependencies. Only corpus audio whose consent covers it may go to external services, and any LLM judge must follow the same rule (see AGENTS.md).

The first isolated voice-cloning probe uses Qwen3-TTS 0.6B Base, a verified 4-bit MLX conversion, and mlx-audio 0.6.1. It runs locally and does not add a dependency to the Swift app or light harness. Setup is explicit:

```sh
uv sync --project research --locked
uv run --project harness python scripts/fetch_models.py --models qwen3-tts-12hz-0.6b-base
```

Use a mono PCM16 reference of 3–10 seconds from the full-band source (at least 24 kHz), its own ASR transcript in a text file, and the desired English text in another file:

```sh
uv run --offline --locked --project research python research/voice_clone.py \
  --reference .local/reference.wav --reference-text .local/reference.txt \
  --text .local/english.txt --out .local/runs/voice-comparison
```

The probe verifies all manifest artifact hashes, forces Hugging Face/Transformers offline, requires the local codec and speaker encoders, and writes `speech.wav` plus measurements to a new `.local/` directory. It does not capture, play audio, or integrate with the live pipeline. The initial source-conditioned English sample is documented in [voice-match.md](../reports/week2/voice-match.md); speaker identity, end-to-end latency, and PyTorch parity remain unqualified.

`voice_stream.py` measures the audio-streaming path with a resident model, a reference PCM array, and output chunks copied into RAM. All file reads precede generation; research WAV/JSON artifacts are written after all measured runs. The first run includes reference preparation, and subsequent runs reuse its in-process cache. It reports first audio, throughput, and the minimum initial buffering needed to play the observed chunks without starving. Example, using the same input files as above:

```sh
uv run --offline --locked --project research python research/voice_stream.py \
  --reference .local/reference.wav --reference-text .local/reference.txt \
  --text .local/english.txt --out .local/runs/voice-streaming --runs 3 --interval 0.5
```

This is an isolated TTS measurement. It excludes source acquisition, ASR, translation, commit waiting, IPC, output-device latency, and resource contention with those stages.

## Resident worker for the live pipeline

### Experimental model selection

The app's experimental voice selector and `a2a-pipeline --tts clone --clone-model
qwen|omnivoice|chatterbox` select one resident worker/model per session. Stop the session
before switching. Qwen is the default. All three are research-only; no run qualifies
speaker identity or product acceptance gates.

Setup is explicit and resumable. These commands fetch only the selected model and its
manifest-declared dependencies, verifying every file before atomic promotion:

```sh
uv run --project research python research/voice_models.py --list
uv run --project research python research/voice_models.py --install qwen
uv run --project research python research/voice_models.py --install omnivoice
uv run --project research python research/voice_models.py --install chatterbox
```

OmniVoice includes its full reference-audio codec (about 2.0 GB combined). Chatterbox
Multilingual V3 includes a separately pinned S3TokenizerV2 dependency (about 3.2 GB
combined). Chatterbox's loader initializes the verified local tokenizer directly, avoiding
the upstream hook's automatic unpinned Hub lookup. OmniVoice's bundled codec carries
the Boson Higgs Audio 2 Community License; inspect the manifest's recorded license
evidence before any external release.

`tts_model_adapters.py` prepares references in memory and reuses their tokens/conditionals
for subsequent units. Qwen supports ICL and xvector; the other choices use the default
ICL option. The installed OmniVoice and Chatterbox implementations return whole phrases;
their worker capability reports `audio_output_streaming: false`. IPC still uses bounded
audio chunks, and cancellation discards obsolete results, but it cannot interrupt a
whole-phrase model call. The current adapters do not add source/background separation.

### Qwen baseline

`tts_worker.py` keeps the same verified model resident and streams PCM to `a2a-pipeline --tts clone` (Swift adapter `CloneVoiceSynthesizer`) over a length-prefixed stdin/stdout protocol documented in the file header. At startup it verifies every manifest hash, loads offline, and warms both conditioning paths so the first unit does not compile Metal kernels. Per unit it bounds `max_tokens` by text length (a missed EOS cannot babble for 20 s), trims leading and trailing silence without delaying voiced audio, and matches loudness to the reference's voiced RMS with one gain per unit. Stdin EOF ends the process, so a killed host never leaves the model resident. `--fake` is a standard-library tone generator that the Swift contract tests use; it imports no model library.

With `a2a-pipeline --fluent`, each request carries the unit's source duration (`target_s`). The worker then compacts pauses longer than 180 ms and time-stretches with streaming WSOLA (`audio_pacing.py`, pitch preserved, at most 1.3×). The speed is calibrated per voice from shaped output. Pacing needs NumPy, so the standard-library fake worker skips it. A sentence-final unit keeps a 150 ms trailing pause; a clause keeps 60 ms.

The pipeline launches `research/.venv/bin/python research/tts_worker.py` directly (no `uv run` at session start), so run `uv sync --project research --locked` after changing the lockfile.

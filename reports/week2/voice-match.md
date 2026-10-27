# Voice cloning: isolated probe and live pipeline integration — 25 October 2026

The user requested English spoken in the source actor's voice. This probe tests TTS
alone, following the instruction to diagnose stages separately. It does not run
capture, translation, the playout policy, or the full live workflow.

The reference is a four-second mono 48 kHz PCM slice of the clean, exact-start
YouTube excerpt already selected by the user. It contains the first questioner,
ending before the answering speaker. The reference is taken from the full-band
source, without upsampling ASR audio. Apple SpeechTranscriber independently
transcribed this clipped reference. Raw media, reference text, generated speech,
and back-transcripts stay under `.local/runs/youtube-voice-match/`.

Qwen3-TTS 0.6B Base uses both the speaker embedding and reference codec/text
conditioning (ICL), rather than selecting a preset system voice. The manifest pins
the upstream checkpoint, the converted checkpoint, all 13 artifact sizes/hashes,
and model-card license evidence. The conversion card does not declare the source
checkpoint revision used at conversion time; numerical parity remains pending.
Both large artifacts matched their published Hugging Face LFS SHA-256s during
explicit setup. Runtime loaded the verified local directory with Hugging Face and
Transformers offline. The research environment pins mlx-audio 0.6.1 and MLX 0.32.4
in its separate uv lockfile. It does not resolve PyTorch in ordinary checks.

| Measurement | Result |
| --- | --- |
| Reference | 4.00 s, mono 48 kHz PCM16 |
| Generated English | 4.24 s, mono 24 kHz PCM16 |
| English back-transcription | All 12 words matched the requested line; diagnostic WER 0 |
| Model loading | 5.993 s |
| Generation | 6.395 s |
| First complete result | 6.338 s; nonstreaming probe |
| Real-time factor | 1.508 |
| Peak MLX allocation | 5,540,693,023 bytes; not whole-process RSS |
| Raw PCM peak | 0.2364; no clipping or gain adjustment |
| Identity result | Pending listening assessment; no calibrated independent verifier or Match-5 result |

The back-transcript is an intelligibility diagnostic, not evidence that listeners
recognize the original actor. This single four-second reference is not the M2
speaker study. The original nonstreaming generation is slower than playback;
the streaming follow-up below measures a different generation path. Neither probe
qualifies end-to-end live latency, cancellation, or bundled-worker behavior.
The Swift app still uses system TTS. No live pipeline is rerun for this probe.

The local comparison includes the source `reference.wav`, cloned
`clone-06b/speech.wav`, and a system voice speaking the same English line at
`system-baseline/speech.wav`. No source audio is automatically played.

Reproduction instructions are in [research/README.md](../../research/README.md).
Run the recorded reference and text through `research/voice_clone.py` with a fresh
output directory, then back-transcribe the output using `a2a-stage-probe asr
--locale en-US --chunk-ms 100`.

Validation: the real reference-conditioned inference and separate back-ASR both
succeeded. English-only sample playback with `afplay` exited successfully.
`scripts/fetch_models.py --check` reports ten entries, one enabled, zero problems.
All seven tests in `harness/tests/test_fetch_models.py` passed in 3.11 s, covering
integrity, resume, restricted selection, and atomic promotion independently of
model inference. The research lockfile sync passed offline.

## In-memory streaming follow-up

`research/voice_stream.py` loads the model once, converts the same source reference
to a 24 kHz MLX PCM array before timing, and passes that array to the cloning API.
Each generated PCM chunk is copied to RAM immediately. No audio/text files are
read or written during generation; WAV/JSON evidence is saved afterward. The
model's reference cache stays in the process. This uses `stream=True` with a
0.5-second requested interval (six codec steps, approximately 0.48 s of PCM).

| Run | Reference cache | First signal chunk | Generation | Output | Generation / output |
| --- | --- | --- | --- | --- | --- |
| 1 | Unprepared | 0.446 s | 1.502 s | 4.00 s | 0.376 |
| 2 | Prepared | 0.184 s | 1.223 s | 4.00 s | 0.306 |
| 3 | Prepared | 0.181 s | 1.219 s | 4.00 s | 0.305 |

All three runs return nine PCM chunks. The observed chunk-arrival schedule needs
no extra startup buffering beyond its first chunk to avoid starvation in an
ideal playout queue. Model loading takes 1.180 s in this follow-up; that is not
a clean-install or cold Metal-cache measurement. Peak MLX allocation is
2,437,584,997 bytes, excluding non-MLX process memory. Apple back-transcription of
run 3 again matches all 12 English words. Raw evidence is under
`.local/runs/youtube-voice-match/stream-memory/` and
`stream-memory-back-transcript/`.

This demonstrates in-memory reference-conditioned audio streaming faster than
playback for this one repeated utterance on this Mac. A 2–3 s steady-state
end-to-end target is plausible, not established: ASR and clause-commit waiting,
translation, worker integration, cancellation, concurrency, and output-device
timing are outside the measurement. A new speaker also needs an initial source
reference. Streaming audio's perceptual identity must be checked separately from
the original nonstreaming sample.

## Live pipeline integration

`a2a-pipeline --tts clone` replaces the system voice with the same verified model, kept
resident by `research/tts_worker.py` and driven by the Swift adapter
`CloneVoiceSynthesizer` (A2AModels) through `SessionPipeline`'s unchanged effects. The worker
verifies manifest hashes, loads offline, warms both conditioning paths, streams 0.32 s
chunks, cancels between chunks, bounds `max_tokens` by text length, trims leading and
trailing silence without delaying voiced audio, and matches loudness to the reference's
voiced RMS (first-unit RMS 0.029–0.036 against the reference's 0.037; before calibration
the first unit came out at 0.126). Stdin EOF ends it. Reference audio and text stay in
process memory.

The reference is either fixed (`--voice-ref`, prepared before capture) or acquired live by
`ReferencePolicy` (A2ACore): finalized utterances with recognizer token timing, cut on
padded token spans, rejected when quiet (< −45 dBFS), clipped, shorter than 0.8 s, or past the
10 s cap. Version 1 is prepared at 3 s of speech and one upgrade follows at 8 s (or when the
next utterance would exceed the cap). There is no diarization yet: one voice is used for
every speaker.

Exploratory file-mode runs, not tuning and not gate evidence. Source: the 120 s excerpt
(multi-speaker dialog), paced in real time, Apple ASR and MT, translation-only output,
offline render (no tap, no device playback). Onset lag is source onset to first English
sample on the render timeline. Measured with `research/run_metrics.py`; raw runs are under
`.local/runs/clone-e2e/`.

| Run | Thresholds (debt / lag) | Units played | Source coverage | `lag_onset` median / p90 | TTS first chunk median | English s per source char, median / p90 | English ÷ source duration |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Fixed reference | 3 / 4 s (defaults) | 10 / 29 | 0.159 | 2.51 / 3.32 s | 0.159 s | 0.278 / 0.37 | 1.56 |
| Fixed reference | 6 / 8 s | 20 / 29 | 0.454 | 4.39 / 5.58 s | 0.159 s | 0.309 / 0.50 | 1.83 |
| Acquired reference (v1: 4.0 s reference, ready at 8.5 s; v2: 6.5 s reference) | 6 / 8 s | 20 / 29 | 0.462 | 4.21 / 5.82 s | 0.160 s | 0.333 / 0.49 | 1.90 |
| Acquired, `--max-unit-keys 20` | 3 / 4 s | 11 / 34 | 0.132 | 2.58 / 2.84 s | 0.179 s | 0.287 / 0.31 | 1.53 |

Synthesis is not the bottleneck. The worker loads in about 2 s, warms up in 2.5 s, emits its
first chunk in 0.11–0.18 s, and generates at a real-time factor of about 0.31, with a peak
MLX allocation of about 2.4 GB. Two things limit coverage. First, the cloned English runs
1.5–1.9 times as long as its Mandarin source. Second, 23–24 of 29 commits wait for recognizer
finalization because Apple's partial results carry no clause punctuation. The debt
estimate (0.25 s per source character) is slightly optimistic against the measured
0.28–0.33 s, so the debt fallbacks are real, not estimator artifacts. A shorter forced-split
length alone does not help. Live acquisition costs nothing measurable against the fixed
reference here: its first profile was ready before the first translation arrived.

Intelligibility after the worker's trimming and gain shaping, as a same-framework
diagnostic: Apple's en_US SpeechTranscriber run on the complete rendered output, scored
against the English of units that played to completion, gives a WER of 0.133 (fixed
reference) and 0.145 (acquired reference). Against every unit that started playing, the
WER is 0.161 and 0.222, because fallback flushes cut those units short. Most errors are
proper nouns, such as "Quanzhen" transcribed as "Kwanzin".

Speaker similarity with Qwen3-TTS's own speaker encoder (`research/speaker_similarity.py`)
is **circular** and has little range. Against the source reference it gives 0.882 for the
system voice, 0.95–0.97 for ICL and x-vector samples of the same line, and 0.963–0.977 for
the complete pipeline outputs. It cannot separate ICL from x-vector conditioning, and it
is not identity evidence. M2 still needs an independent verifier and blinded listeners.

Open items, in order:

1. Clause-level commits from partials (M4).
2. Duration control. Outputs of the same text varied from 2.56 to 4.40 s at the default
   sampling temperature of 0.9. A lower temperature is the cheapest probe; measure its
   s-per-character p90.
3. Playout-rate control, designed together with the debt estimator.
4. A larger or less quantized model, after manifest verification.
5. Per-utterance speaker embeddings for multi-speaker sessions.
6. A live tap run of the clone path, and its startup and stop lifecycle under A3.

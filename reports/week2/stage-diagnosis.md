# Real-source stage diagnosis — 24–25 October 2026

Source selected by the user: [YouTube NRB0ICxMkrM, starting at 9:40](https://www.youtube.com/watch?v=NRB0ICxMkrM&t=580s).
The 9:40–11:40 excerpt was acquired explicitly for this local test. Source, subtitle,
transcript, and synthesized-audio artifacts remain in `.local/`; no redistribution
or voice-cloning qualification is claimed. All model processing used installed
Apple frameworks. No corpus audio or transcripts were sent to an external judge.

The user subsequently requested separate stage tests and no further complete
workflow runs. The running workflow was stopped. The results below are isolated
component measurements, not an end-to-end acceptance pass.

## Findings

1. **Media preparation was corrupting the source.** The test script used
   `adelay=6000` on stereo input before downmixing. Only one channel was delayed;
   the mono result contained both current speech and a six-second delayed copy.
   This invalidates the earlier real-source live runs for model-quality and latency
   conclusions. The fix is `adelay=6000:all=1`. A synthetic stereo regression checks
   leading silence, one copy of the content, and trailing silence. On the actual
   excerpt the corrected prepared content matches the original mono decode exactly
   (maximum sample difference 0; correlation effectively 1.0).

2. **The system TTS adapter could falsely overflow its stream.** A 64-buffer limit
   counted small AVSpeechSynthesizer callback buffers rather than audio duration.
   Isolated synthesis of eight known translations failed with “synthesis consumer
   fell behind.” The adapter now coalesces native buffers into 100 ms chunks, flushes
   the short final chunk, and bounds queued audio to 12.8 seconds. The same eight
   utterances then completed. This is bounded buffering, not an unbounded queue.

## Separate tests

| Stage | Input and isolation | Observation |
| --- | --- | --- |
| Capture | 24 s of original human audio played by FixturePlayer; tap muted; no ASR, MT, TTS, or mixer playback | Correlation **0.9999999967**; 0 capture overflow, trace drops, rejected blocks, or invalid timestamps. Native capture rate 48 kHz in this run. The leading delay and trailing silence are excluded by waveform alignment. |
| Resampling | Same original 24 s; 48 kHz→16 kHz | Streaming and whole-buffer output identical: 384,000 frames each, maximum difference **0**. Against FFmpeg: correlation **0.9999991381**, RMSE **0.00005205**. |
| ASR, original file | Original downloaded 120 s, with no capture, MT, TTS, or commit policy | 12 final segments; 375 normalized characters versus 362 characters from 34 fully contained published subtitle cues; diagnostic CER **11.33%**. |
| ASR, streaming | Original 24 s, 20 ms chunks at live pace | Diagnostic subtitle CER **17.65%** without VAD finalization, **22.35%** with it. These short-clip scores include a truncated boundary cue and are not directly comparable to the 120 s score. VAD changes finalization behavior, but does not explain the earlier large mismatch by itself. |
| MT | Eight published subtitle lines supplied directly, bypassing ASR and scheduling | **8/8** completed, no empty output. Median **397 ms**, max **1,430 ms** including the first request. This establishes execution and latency; semantic adequacy was not independently rated. |
| TTS, corrected | Those eight English translations, bypassing source recognition, capture, and live playout | **8/8** completed; **20.74 s** generated speech; median first chunk **298 ms**. English back-transcription retained 70 words with **2 word differences (2.86% WER)**. This is a same-framework diagnostic, not an independent naturalness/identity grade. |

Subtitle CER uses local OpenCC Traditional→Simplified normalization plus the
harness's punctuation/width normalization. Published subtitles may paraphrase;
they are not a human-checked verbatim transcript. No negative subtitle-alignment
latencies from the invalid earlier runs are used as performance evidence.

The new `a2a-stage-probe` executable runs `asr`, `mt`, `tts`, and `resample` separately,
without starting a tap or rendering to an audio device. ASR probes optionally use
live pacing and VAD-triggered finalization. Probe text/audio outputs require an
explicit new output directory. Commands and inputs are documented in the README.

Research artifacts are under `.local/runs/youtube-stage-tests/`. The original
excerpt SHA-256 is `8ce928d3164ed1d14de4fa99cf9b0235603bc702018a0b802d9ccad4bc6a0c7a`.
Earlier full-run directories retain their raw evidence and an `INVALID_INPUT.json`
marker. English-only bus policy and stale-held-unit recovery have deterministic
regressions; the 44.1 kHz playout indices were also corrected to use device samples.

The post-gain monitor API now permits explicit `--rendered-out` recording of the
actual render buffer, including gains, silence, and flushes. It uses a preallocated
SPSC ring and non-RT WAV writing, and is off by default. Its buffer behavior is
covered by a C-shim test. The interrupted live monitor run is not acceptance
evidence: its process exited with signal 5 during shutdown and that live shutdown
path remains unqualified. The local crash stack identified an executor assertion
in the global-queue signal handler; its closure inherited the top-level MainActor.
The lock-only stop handler is now explicitly `@Sendable`. No new full workflow was
run after the user's isolation instruction, so live shutdown still needs qualification.

Current app playback follows ADR-0004: the original bus remains muted during an
active English-only session; unavailable English produces silence. A complete
live-source retest and the unchanged product quality/lifecycle gates remain open.

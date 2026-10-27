**Playback smoothness, efficiency, and memory — 25 October 2026**

This pass reviewed the playout path for audible defects (clicks, cut-off phrases, silent gaps) and fixed what file-mode evidence could show. Everything here is a signal-level proxy measured on rendered output. **No live tap run, physical output, or listening test was made**, so perceived smoothness is unverified. The 3 s / 4 s debt and source-lag thresholds and every gate claim are unchanged.

## Defects found and fixed

| Area | Defect | Fix |
| --- | --- | --- |
| Fluent playout (`--fluent`, the app's mode) | Soft health was skipped whenever any unit was playing. In continuous speech units play back to back, so debt and lag were never checked. The queue grew until the 8-unit cap cut the playing phrase and flushed everything. | The phrase being heard always finishes, and later units wait off the bus until it ends. Debt or lag skips the oldest units nobody has heard yet (`catch_up` trace, reason `catch_up`), always keeping the newest. Hard failures still fall back. Decision recorded in [ADR-0004](../../docs/adr/0004-translation-only-playback.md). |
| Fluent recovery | Long units are older than the 4 s lag limit by the time they commit. Units were expired on arrival, and the healthy interval never started, so most of a real session was silent. | The newest candidate is never expired. Recovery from a hard failure needs a healthy worker and a fresh unit. |
| English-only sessions | A unit that failed translation or synthesis forced a fallback. That could only replace the phrase being heard with silence. | The unit is skipped. When the original bus is audible, it still falls back. |
| Translated bus (RT) | A flush (fallback, speaker change, discontinuity) stepped from speech straight to zero. It also discarded audio written after the flush call. | The head of the discarded audio fades out over 5 ms. Only audio queued before the call is discarded. A bus that runs dry ramps its last sample to zero, and resumed audio fades in. |
| Stop | The device stopped before the closing gain ramp ran, cutting speech mid-waveform. | The device stops once the ramp lands (bounded wait). |
| Streaming clauses | The pause split needs gaps between token time ranges. Apple SpeechTranscriber partials carry one range for the whole text, and final token ranges are contiguous (measured on the real-source excerpt), so the split never fired. | The threshold is now configurable (`minClausePauseSamples`). Tests cover recognizers that report gaps. |
| Clone time-stretch (WSOLA) | A ±5 ms overlap search misaligns voices below about 85 Hz, giving 11–13% level dips at 65–70 Hz. | ±8.3 ms search. Level stays within 3% from 65 to 220 Hz (tests). |
| Clone pacing | Speed was calibrated on raw warm-up audio, including silence, and over-sped the first units. The cap was 1.6×. | Calibration uses shaped audio. The cap is 1.3×, matching the system voice. |

## System voice pacing (fluent only)

With a target duration (the unit's source span), `SystemVoiceSynthesizer` sets `AVSpeechUtterance.rate` from a measured rate→speed table, capped at 1.3×. It learns seconds per character from completed units. It also trims the 190–200 ms of digital silence AVSpeechSynthesizer appends to every utterance to 180 ms after a sentence or 90 ms after a clause. The clone worker keeps 150 ms after a sentence. The pipeline warms the voice at startup.

| Rate | 0.50 | 0.52 | 0.54 | 0.56 | 0.58 | 0.60 |
| --- | --- | --- | --- | --- | --- | --- |
| Speed-up (compact en-US voice, two sentences agree within 0.5%) | 1.000 | 1.077 | 1.234 | 1.354 | 1.424 | 1.591 |

The first utterance in a process started after 723 ms, and later ones after 225–301 ms (stage probe). Only compact-quality voices are installed on this Mac, so higher-quality voices were not measured.

## A/B results

File mode, real-time pacing: `--asr apple --mt apple --translation-only`, debug builds on both sides. "Before" is the code as found, including its uncommitted `--fluent` work. Clips: the synthetic `zh-demo` (3 s gaps) and `zh-demo-continuous` (0.8 s gaps), and the user's 120 s real-source excerpt (`youtube-NRB0ICxMkrM-0940`). Measured with `research/run_metrics.py` plus a rendered-audio click check. Raw runs are in `.local/runs/ab/`.

| Clip, voice | Units played | Coverage (source s) | Fallbacks | Catch-up skips | `lag_onset` median / p90 / max (s) |
| --- | --- | --- | --- | --- | --- |
| zh-demo, system | 4 → **8** of 8 | 0.32 → **1.00** | 2 → **0** | 0 | 3.32 / 3.90 / 3.90 → 2.71 / 4.50 / 4.50 |
| zh-demo-continuous, system | 6 → **8** of 8 | 0.61 → **1.00** | 2 → **0** | 0 | 2.44 / 3.50 / 3.50 → 2.75 / 4.08 / 4.08 |
| real excerpt, system | 13 → **24** of 29 | 0.21 → **0.77** | 5 → **0** | 5 | 3.52 / 5.38 / 6.34 → 4.98 / 10.98 / 13.20 |
| real excerpt, clone (acquired reference) | 12 → **22** of 29 | 0.20 → **0.72** | 6 → **0** | 7 | 2.88 / 4.16 / 4.49 → 5.28 / 8.26 / 13.08 |

On the real excerpt, the higher lag after the fix is the cost of playing what the old path dropped. Units of 10–14 s commit only when the recognizer finalizes them. Their onset lag is inherent: for example 13.1 s onset but 3.2 s from the unit's end. The next phrase also waits behind such a unit. Clause-level commits from partials (M4) are the remaining fix.

System-voice English per source character fell from 0.258 to 0.207 s (median) on the real excerpt. The clone's rose from 0.215 to 0.259 s because its speed cap fell from 1.6× to 1.3× and played units now include long ones. The clone's first-chunk median stayed at 0.18 s. Its 3.24 s maximum is a request queued behind a 10 s unit's synthesis: synthesis is serialized by design, and the old code had expired both units.

**Clicks at fallbacks** (non-fluent mode, where fallbacks still cut the playing phrase): on the real excerpt, both builds fell back 7 times. The original output stepped from speech to digital silence 3 times, with steps up to 0.18 and peaks of 0.19–0.47 in the 10 ms before. The new output has no such step; the last milliseconds decay, for example 0.162 → 0.107 → 0.078 → 0.047 → 0.029 → 0.004 (1 ms peaks). Without `--fluent`, fallbacks match the original on all three clips. Units played and coverage are identical on the synthetic clips; on the real excerpt they differ by one unit (10 vs 9 played), within recognizer timing variation between runs.

## Efficiency and memory

| Item | Before | After |
| --- | --- | --- |
| App helper build | debug (per-sample DSP about 11× slower: 60 s resampled in 1.9 s vs 0.17 s) | optimized by default; `scripts/build_app.sh --debug` keeps a debuggable build |
| Translated ring, live 48 kHz | 60 s stereo, rounded to 87 s: 33.5 MB prefaulted | 30 s mono, rounded to 44 s: 8.4 MB |
| Monitor ring, live delay line | 2.1 MB + 2.1 MB, always allocated | none unless `--rendered-out`; 0.26 MB |
| Pipeline footprint, file mode (median) | 45.6 / 40.4 / 66.3 MB | 34.8 / 30.6 / 48.0 MB (zh-demo / continuous / real excerpt) |
| Worker PCM decode | per-sample closure | one copy |

Live-mode RT memory falls by about 29 MB by calculation; no tap session was run. The clone worker's MLX peak is 2,314 MB: 1,783 MB active plus generation tensors, with 35 MB cached because mlx-audio clears its cache after each unit. `mx.set_cache_limit` at 256 MB or 64 MB changed neither peak nor real-time factor (0.32–0.33), so it is not applied.

## Verification

`scripts/check.sh` passes: 130 Swift tests executed and 55 harness tests, with golden traces unchanged and all executables stamped SDK 27.0. The harness suite also passes against the release `a2a-pipeline`.

New tests cover:
- RT flush fade, discard only before the call, dry-bus ramp and fade-in, mono bus, optional monitor
- fluent waiting, catch-up, queue cap, English-only skip, recovery, `targetSeconds`
- streaming clause splits
- system-voice rate mapping
- `audio_pacing` duration, streaming equivalence, low-voice phase, bounds, and pause compaction

Still open: a live tap run of the fluent path with both voices, physical output and a listening check, A2 loopback latency, and selecting a higher-quality installed system voice where one exists.

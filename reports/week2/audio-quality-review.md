# Audio quality and reliability review — 25 October 2026

The reviewed changes fix reproducible playback discontinuities, strengthen input handling,
and reduce session bookkeeping and unnecessary DSP allocation. Commercial voice quality
remains unqualified: the deterministic results below establish software behavior, not
naturalness, speaker identity, hardware latency, or long-running real-model performance.

This pass builds on the phrase pacing and fluent-playout changes already present in the
working tree. It preserves the reducer boundary, local-only inference, research-model
classification, and existing latency/debt thresholds.

## Findings fixed

| Priority | Finding | Change and evidence |
| --- | --- | --- |
| High | A short resumption during an unfinished dry-bus fade discarded the old tail, or restarted the next fade from a sample that excluded it. | The C mixer now continues the tail through resumed PCM and remembers the combined output. Two regression cases previously produced maximum sample jumps of **0.5772** and **0.5667** on a constant 0.6 signal; both now satisfy **<0.04**, remain bounded, and settle to zero. These are synthetic sample-domain checks, not perceptual scores. |
| High | Oversized IOProc buffers were clamped to scratch capacity. The output beyond that capacity remained untouched, and capture truncation was unreported. | Reject the complete cycle, zero every output buffer, and increment rejection telemetry. Tests cover oversized input and oversized noninterleaved output. No allocation, lock, or framework call was added to the callback. |
| High | WAV decoding read format fields before validating their available bytes, and silently shortened truncated data chunks. | Validate chunk bounds, basic/extended format lengths, and complete interleaved frames; malformed inputs throw `WAVError` instead of indexing outside the input or silently losing samples. |
| Medium | Once the session was translating, later fluent phrases could start from the first small chunk, bypassing the configured prebuffer. | Every fluent phrase waits for its PCM reserve. A completed short phrase is released below that threshold. Tests cover empty-bus startup, waiting behind another phrase, and short completion. |
| Medium | The gate retained an individual canceled/finished ID indefinitely, even after the reducer pruned the matching record. The driver also retained every canceled synthesis ID. | Pruned IDs coalesce into retired ranges that continue rejecting late PCM and preserve holes for unfinished units. The driver rejects late messages using its existing live play records. A 600-unit regression checks bounded recent tombstones and late-PCM rejection. This does not establish a process-wide memory plateau with real models. |
| Medium | Repeated engine creation and host-clock reads rewrote shared timebase values while callbacks could read them. | Publish the cached timebase once with `pthread_once` on ordinary threads. Callback conversion only reads the initialized values. |
| Medium | Power-of-two capacity rounding and delay-size arithmetic could wrap; some delay configurations could make the rounding loop never terminate. | Reject unrepresentable ring sizes and calculate delay requirements in 64 bits before allocation. Regression tests use maximum integer sizes. |
| Low | An equal-rate resampler constructed a sinc table that its pass-through path never used and reported nonexistent filter latency. | Skip kernel construction and report zero latency. At the default kernel width this avoids **34,824 bytes** of table storage per equal-rate instance, plus table calculation. This is a calculation from the allocation size, not an end-to-end benchmark. |

Relevant implementation:

- [Mixer and IOProc](../../apple/Sources/A2ARTShim/engine.c), [rings and timebase](../../apple/Sources/A2ARTShim/rings.c).
- [Phrase scheduling and pruning](../../apple/Sources/A2ACore/SessionPipeline.swift), [PCM gate](../../apple/Sources/A2ACore/PlayoutGate.swift), [driver](../../apple/Tools/a2a-pipeline/main.swift).
- [Resampler](../../apple/Sources/A2AMedia/Resampler.swift), [WAV reader](../../apple/Sources/A2AMedia/WAV.swift).

## Verification

Runnable DoD:

```sh
scripts/check.sh
scripts/build_app.sh
scripts/smoke_app.sh
```

All passed on the reference Mac with full Xcode 27 and SDK 27.0:

- **134 deterministic Swift tests executed**, with the two interactive Apple runtime
  tests intentionally skipped. Baseline: 124 executed.
- **56 harness tests passed**, including a new end-to-end fluent fixture replay that
  requires each audible phrase to end before the next begins and the final phrase to
  finish. Baseline: 55 passed.
- All executable SDK stamps matched 27.0; the manifest gate reported 10 entries,
  1 enabled, and 0 problems. Existing golden traces passed without regeneration.
- Optimized local app build and strict signature verification passed.
- Native window startup and playback-switch smoke assertions passed in synthetic mode.

Local evidence, kept outside git:

- `.local/review-baseline.log`
- `.local/review-regression-before.log` — expected failures reproducing the fade,
  oversized-buffer, and prebuffer defects before their fixes.
- `.local/review-after.log`
- `.local/review-build-app.log`
- `.local/review-smoke-app.log`
- `.local/runs/app-smoke.BHDKz6/app.json`

The runnable development build is `.local/apps/A2A.app`.

## Work still required for commercial qualification

1. Evaluate intelligibility, naturalness, speaker similarity, and omission/coverage on
   consented development recordings with fixed thresholds. Fluent catch-up can skip
   unheard units; finishing a phrase does not prove complete translation coverage.
2. Measure live ASR/MT/TTS together during a real meeting, including sustained debt,
   callback deadlines, output gaps, and process/model memory over a long session.
   The allocation and bookkeeping improvements above do not quantify those budgets.
3. Complete A2/A3 wired-headphone loopback latency, private muted-tap crash teardown,
   output-device changes, sleep/wake, and permission transitions, as tracked in
   [status.md](status.md). Synthetic window startup does not exercise these paths.
4. Finish the isolated inference host and qualify a product-eligible cloning model.
   The system voice remains plumbing-only; the current Qwen worker remains
   research-only with `tts_identity_qualified: false`.

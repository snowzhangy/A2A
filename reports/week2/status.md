**Implementation status — 25 October 2026**

2 November. Each line now takes its actor's time. A2A paced translated speech in one direction:
a long take was sped up to 1.3×, a short one was played short and the rest of the actor's turn went
by with nothing on the bus. dub-anything re-times every line to end where the actor stops and its
listeners put that rhythm above the acting; we had neither the fix nor the number. With a picture
delay each phrase already plays in its own slot, so a line that ends on its actor's last sound
costs nothing — the phrase after it waits for its slot anyway. The reducer grants the permission
(slots, real token timing, and the unit not yet late for its slot); the worker chooses the number
(`estimated / target` clamped to [0.85, 1.3], a deficit under 0.25 s left alone), and
`StreamingWSOLA` now stretches as well as compresses. `tts.request` carries `fill`,
`tts.done.pace` can be below 1, and `a2a-pipeline --no-turn-fill` is the A/B control. The harness
reports the reference's own vocabulary: `turn_dead_air_s`, `turn_overrun_s`, and a signed
`turn_gap_s` per unit. On the deterministic 90 s replay (fake tone voice, three runs differing only
in pacing): dead-air median 1.47 → 1.38 s, total 43.26 → 41.04 s, and 3 of 33 turns now land
within 0.25 s of their actor against 0, with the same 33 lines in every run. The floor binds
immediately at these take lengths, which is the point: a take's length is set by its **words**, so
what is left is a length-controlled translation, not more stretching. No listener has heard the
stretched takes. [ADR-0013](../../docs/adr/0013-turn-timing-fill.md),
[the parity program](dub-parity.md) — the lever-by-lever comparison with dub-anything, including
the two things we still do not do that its listeners notice most: per-line delivery conditioning
and text written to the time available. Checks: `scripts/check.sh` ok (267 Swift tests, 123 harness
tests).

1 November, night. "Multiple speakers mostly get the first speaker's voice" is the speaker tracker, not voice
routing. It fails three ways in the app's own traces, and this entry fixes one of them, so the fix is partial.

What is fixed: the tracker kept at most 4 speakers and never gave a slot back.
- On real video, music and effects took slots in the first minute. One session created S1–S4 within 30 s, and
  nobody after them could be new; in another, two singletons held two slots.
- A speaker heard in fewer than 4 segments, and silent for 30 s, now gives its slot to the next new voice.
- IDs are never reused, because a freed ID may still name a pinned cloned voice (ADR-0009).

Results, against CAM++ actor groups on two drama episodes:
- On six 180 s slices, the voice changes at 0.57 of actor turns instead of 0.48, while the same-actor consistency
  holds at 0.81 against 0.82.
- On whole episodes, separation rose from 0.08 to 0.36 on ep08 and stayed flat on ep09 (0.43 to 0.44).
- In one app-configuration replay:
  - 7 voices played instead of 3;
  - the voice changed at 4 of 5 actor turns instead of 2;
  - one actor moved once, mid-scene, from a voice probably learned from another actor's line to his own;
  - 2 lines were skipped waiting for a new voice;
  - the first-chunk maximum worsened, from 1.6 s to 3.0 s, unexplained.
- The calibration set is unchanged: the narrator stays one speaker, and the female/male dialogues get the same
  labels.

Not fixed, and the more common case:
- the DSP features merge actors (one session stayed S1 for 184 s with 33 VAD stretches);
- the energy VAD stays open under music.

Measured and not adopted:
- more slots, which cost consistency;
- isolated vocals for the tracker;
- updating a speaker only on close matches;
- Qwen3-TTS's speaker encoder, at f 0.47–0.68 against 0.51–0.60 for tuned DSP.

[Speaker slots, and what they do not fix](speaker-slots.md).

1 November, evening. The whole-pipeline onset (3.52–3.54 s median on the paced dialogue replay) breaks down
as follows:
- 2.17 s of span (maximum 3 s);
- 300 ms of VAD hangover on speech-end spans;
- 331 ms of Hy after commit;
- 260–281 ms to the first TTS chunk;
- about 100 ms waiting for the second chunk.

Three ideas that would not change what is said were measured, and none is adopted:
- **Playback on the first chunk:** it never fired under the 0.24 s reserve. Without the reserve, 9% of 245
  recorded streams would start with a gap. Reverted.
- **Holding Hy back while TTS starts:** TTS was just as slow with Hy idle. Not built.
- **Hy's tail turn during the hangover:** the drama fixture has 0.59 false pauses per real speech end at
  140 ms, against a 160 ms ceiling on speech-end spans only. Deferred.

What is left are product calls, both of which change the units:
- the 3 s maximum span;
- the 300 ms hangover.

Using the new global tap also found a bug: with nothing playing, the tap gets no IO callbacks at all. A
session started before the video therefore failed as "capture stalled". It is fixed with
`ResolvedSource.mixIsIdle`, and the `--seconds` tail now ends by the clock. `TapSession` also refuses a global
tap with an empty exclusion list. Checks: `scripts/check.sh` ok (252 Swift tests, 97 harness tests).
[Where the 3.5 s goes](pipeline-latency.md).

1 November, later yet: the app has no source list. It taps the machine's whole output mix —
`CATapDescription(stereoGlobalTapButExcludeProcesses:)`, `--tap-global` on the pipeline, `--global` on
`a2a-tapctl` — so whatever is playing gets translated, including an app started after us, and starting a
session needs no choice. Choosing was also unanswerable: macOS tells a third-party process only that a client
has an active output unit (`isRunningOutput` stayed true for WebKit with nothing playing and for `ffplay` 8 s
after its clip ended). The failure mode of a global tap is answering your own translation, so the new
`a2a-loopprobe` plays a 12.5 kHz tone into our own output bus and then through `afplay`: the control lands
35 dB above the floor, ours sits 3 dB below it — `ok: our own output stays out of the global mix`
(`raw/global-loopprobe.json`). Saved voices now share one `System audio` store, separated by voiceprint. Not
closed: the mute behaviour of a global tap needs a listening pass (`.muted` now means every other process),
and consent now covers everything the machine records. `--tap-bundle`/`--tap-pid` remain for the A1/A3 and
harness tests. First bug, from using it: every session failed at once with "source app exited", because the
driver's health check read `members.allSatisfy { … }` and a global source names no members — vacuously true.
Now `ResolvedSource.processesAllExited()`, answered "not gone" for a global or empty list, with a test on all
four cases; for the whole mix the real signals are a 2 s capture stall and an output-device change, and
silence is neither. [The system's output is the source](system-audio-source.md).

1 November, later still: a Hy-SimulMT turn is now one batch instead of three. `mtmd`'s helper evaluated the
text before the audio, the audio and the assistant header as three separate passes, and each pass re-reads
this 30B MoE's active weights, so the turn's prompt now goes to `llama_process` once as a mixed
token/embedding batch (identical positions, mask and logits point; falls back to the helper for M-RoPE,
non-causal or over-`n_batch` turns). Idle that is 5–7% off the span and 12–52 ms off the wait after commit;
with a Qwen3-TTS worker generating in another process — the sharing the live app really runs in — it is 9%
off the span (1270 → 1161 ms median) and 155 ms off the worst span. Content is unchanged: 10 of 10 spans
identical on one region, 7 of 10 on another where three spans picked a different near-equal token
("customers"/"guests", "yet dared"/"yet she dared") — merging three passes into one changes the reduction
order, so greedy sampling can flip a near tie; `--no-merge-prompt` restores the old tokens exactly, and the
dev-set metric pass is still owed before this is called quality-neutral. Measured and
rejected with their numbers: `--kv q8_0` (slower *and* different text), `--no-kv-unified`, `--flash-attn`,
`--mlock`, extra tower and CPU threads, and going straight to ST at flush (7 of 10 spans close on a turn
that answers text, which ST would then deliver later). New dev tools: `scripts/bench_hy_local.sh` (old vs new,
idle and under TTS load, hash on every line) and `research/tts_bench.py` (clone-TTS prepare 10–13 ms, first
chunk 202–216 ms, RTF 0.34–0.44; its profile keeps the pinned-loop `mx.compile` question answerable).
[One batch per turn](inference-latency.md).

1 November, later: local Hy-SimulMT now hears each span while it is spoken, so a committed span
waits only for its last turn. Both task prompts also stay resident in the KV cache, which removes
about 200 ms of prompt prefill from every new conversation and every ST fallback. On a paced replay
in the live app configuration (synthetic two-speaker dialogue, dual Qwen clone), median Hy time
after commit went from 857 to 340 ms. Median, p90 and maximum onset went from 4.23 / 4.70 / 4.91 s
to 3.50 / 3.74 / 3.84 s, and one more unit played (24 of 28), with underruns unchanged. Streamed
and whole spans give identical text: the worker's 48 kHz resampler is now exact under streaming
(`hy-worker --self-test`). Resident prompts flipped 2 of 25 spans at a near-tie in one fixture.
New: `--no-hy-stream`, worker protocol 2 (`discard`, `flush_ms`), `hy-worker --stream-ms N`, and
`mt.result` `worker_ms`/`worker_turns`/`worker_st`. Checks passed: 244 Swift tests executed, 97
harness tests, manifest and SDK gates, app build and synthetic smoke. One live tap of the same clip
gave 318 ms median Hy and 3.58 s median onset, with no capture overflow or underruns.
[Streaming source audio into Hy-SimulMT](hy-streaming-input.md).

1 November update: the three failure modes behind the lines lost in the last live sessions are
fixed. Unlabeled speech now inherits the speaker it continues — with no pause since the last label,
or as a brief reply just after one — which on three 3-minute drama excerpts voices 35 of the 44
pieces the tracker left unlabeled (78% → 95% of committed pieces) with 0 identity conflicts against
the tracker's own labels; the review's no-pause rule alone recovered 9. The cloned voice is capped by
the characters it has to speak (pace floor 3.2 Han / 11 English characters per second instead of
1.6× slack on an estimate), and a line that hits the cap reports `truncated`. An over-long
translation is now cut to the time its source audio took — clause boundary, then word boundary,
never on a dangling function word, never splitting a Han word when the system segmenter is available
— and an empty or filler answer to music or a pause is `silence`, not a failed translation: no error,
no fallback, no context entry. New knobs: `--speaker-carry`, `--speaker-carry-pause`,
`--speaker-carry-piece`, `--no-shorten`, `--no-filler-silence`; new trace fields `speaker_carried`,
`shortened`/`mt_chars`, `mt.result.silence`, `tts.done.truncated`, and the `silence` source
disposition. Checks passed: 230 Swift tests executed, 97 harness tests, manifest and SDK gates. The live A/B
still needs the Hy-SimulMT server: the configured address now answers as a different service with no
`v1/audio/translations` route, and the adapter now reports that as an HTTP status instead of a
vague invalid-response error. [Recovering the lines three live sessions lost](live-session-recovery.md).

31 October update: the commit policy now closes a stable, un-finalized unit at its last safe
clause boundary once that unit covers more than 4 s of source speech (`length_clause`,
`--max-unit-seconds`), and late recognizer punctuation no longer shifts the frontier and speaks
committed words a second time. On a new synthetic long-utterance fixture (`synth
--utterance-sentences`) the plan's default budgets went from 9 played units and 0.730 coverage
to 12 and 0.907, skipped-by-catch-up from 5 to 2, and maximum onset lag from 6.14 s to 4.88 s;
in the app configuration median onset lag went from 5.21 s to 3.51 s. The short-sentence fixture
is unchanged. Replaying the reviewed scene's recorded recognizer stream (404 real partials and
finals) through the policy moved plan-default coverage from 0.722 to 0.916 and p90 onset lag from
9.80 s to 8.94 s, and in the app configuration from 0.905 to 0.979 and 11.94 s to 8.16 s, with
nothing spoken twice. That replay also caught cuts landing inside Mandarin words; a host-injected
`WordSegmenter` (Apple `NLTokenizer`) now keeps them out — 2 of 33 unit boundaries inside a word,
then 0. What remains unmeasured is translation adequacy for the shorter fragments and cloned-voice
quality. [Length-clause commits](length-clause-commits.md).

Latest local update: cloned sessions now pin each speaker's first prepared reference,
isolate conditioning caches, and reset Qwen sampling per voice (ADR-0009). Matched
A/B/A/B model samples reproduced exactly when an actor returned. An actual live tap
kept S1 and S2 on version 1 throughout, completed 11/13 phrases (91.1% by committed
source duration), and had no underruns, PCM drops or capture overflow. Median/max
rendered onset delay was 5.61/9.75 s with separated background. One phrase was skipped
for catch-up and an unlabeled shout was skipped instead of borrowing a voice. Three
annotated actors still map to two tracker identities; full actor consistency is not
solved. See [voice consistency](../../research/live_dub_lab/VOICE-CONSISTENCY.md).
Checks passed: 190 Swift tests executed, 86 harness tests, eight lab tests, SDK/manifest
gates, optimized app build and synthetic startup smoke. Two Apple runtime tests remain
explicitly skipped; the real model/tap runs above are separate interactive evidence.

Earlier soundtrack update: after the user rejected the long delay and missing soundtrack,
the app defaults to streaming speech with adaptive buffering and 8 s / 8 s debt/lag
budgets, plus an experimental separated music/effects bus (ADR-0008). The selected
actual tap run completed 12/13 commits at 4.87 s median / 6.29 s maximum rendered
onset delay, with no underruns or capture overflow. One phrase was skipped. Local
Mandarin ASR found no words in the background track, which is not a listener verdict
on residual voices. See [soundtrack and delay](../../research/live_dub_lab/SOUNDTRACK-AND-DELAY.md).
Checks passed: 183 Swift tests executed, 83 harness tests, eight lab tests, SDK/manifest
gates, optimized app build and synthetic startup smoke.

27 October follow-up: the dub-anything review branch is integrated locally, and
the app exposes optional terminology and pronunciation files. OmniVoice profile
setup and reference-based pacing were improved, but a 37-second file-paced run
still played only 7 of 14 committed units at 8.98 s median onset lag. A shorter
forced-commit experiment reduced maximum lag but also reduced coverage and is not
the default. Matched samples, scope and verification are in
[near-realtime-dub-comparison.md](near-realtime-dub-comparison.md).

27 October continued experiments: Qwen now prepares codec/text/speaker conditioning
without synthesizing a throwaway sentence for every profile. Four matched pairs had
identical durations and waveform correlation above 0.9999998; direct preparation took
0.010–0.199 s versus 2.187–3.423 s with the warmup (shared caches, alternating order).
The clause policy also keeps `只不过` together. Optional `--adaptive-prebuffer` and an
8-step OmniVoice lab worker were tested under changing GPU contention; neither is an
app default. Metrics now distinguish started from completed playback, and corrected
timeline previews retain actual silence and delayed final speech. Details and limits:
[live lab progress](../../research/live_dub_lab/PROGRESS-2026-10-27.md). Full checks pass:
176 Swift tests executed, 83 harness tests, five lab tests, SDK/manifest gates, optimized
app build and synthetic app smoke. Dub-quality live playback remains unachieved.

Quality-first follow-up: the app now defaults to complete-phrase playback with a bounded
20-second debt/lag budget (ADR-0007). An actual private process tap of the synchronized
test player completed 13/13 committed units, with no underruns or capture overflow,
at 16.17 s median onset delay on the selected AirPods output. This establishes live
capture/playback completeness for one scene, not semantic accuracy or dub quality.
Contextual local MT was researched with preceding source context and explicit dev-set
ASR corrections, but its loaded live startup attempt timed out and it remains an
explicit CLI research option. Details: [quality-first report](../../research/live_dub_lab/QUALITY-FIRST.md).
Final checks: 180 Swift tests executed, 83 harness tests, eight lab tests, SDK/manifest
gates, optimized app build and synthetic startup smoke passed.

This page tracks the [implementation plan](../../IMPLEMENTATION_PLAN.md), revision 2.1. A ticket counts as **done** only when its verification command passes on this Mac. **Partial** means the code exists and its deterministic parts are tested, but the DoD needs hardware, permission, weights, or people. **Pending** means not started or blocked. Runtime evidence is in [verifications.md](verifications.md).

The current suites contain 169 deterministic Swift tests and 74 harness tests, plus the manifest and executable SDK-stamp gates. Two Apple model runtime tests are skipped unless `A2A_RUN_APPLE_TESTS=1` is explicitly set. The playback-switch change passes the Swift suite, SDK/manifest checks, and its two rendered-audio/control tests. The quick harness run has 33 passes and six local-server setup errors in this sandbox; native window verification is also blocked here. See [playback-switch.md](playback-switch.md) for exact evidence and limits. Full real-model/capture workflows were not repeated.

The latest implementation and gate decision are in [decision.md](decision.md). Playback smoothness, efficiency, and memory fixes, with file-mode A/B evidence (fluent playout: 0 fallbacks and 0.72–1.00 coverage, against 2–6 fallbacks and 0.20–0.61 before), are in [smoothness.md](smoothness.md); no live tap or listening check was run. Each speaker in a dialogue now gets their own voice (`--speakers auto`, [ADR-0005](../../docs/adr/0005-multi-speaker-voices.md)): on the drama excerpt, three tracked speakers, and rendered units match their speaker 23 of 23 times with system voices and 15 of 20 with cloned voices, against chance before; see [speakers.md](speakers.md). Also file mode only, with no listening check. A signed local SwiftUI app now builds with `scripts/build_app.sh`: source selection, explicit language setup, private session captions, Stop/Quit cleanup, and a bundled development helper. `scripts/smoke_app.sh` verifies native window startup with synthetic display data and no capture/inference. Runtime repairs and the failed unchanged-threshold live development run are recorded in the decision. The cloning-port source audit is in [model-capabilities.md](model-capabilities.md).

Subsequent user-requested real-source work is in [stage-diagnosis.md](stage-diagnosis.md). The app now offers **Listen to → Original / Translated**, including during a session. Translated remains the default under [ADR-0004](../../docs/adr/0004-translation-only-playback.md), with silence during voice acquisition and recovery. Original plays source audio while translation continues in the background. The isolated E1 worker is still unfinished.

An isolated Qwen3 voice-cloning probe now generates English conditioned on a four-second reference from the user's source. Its back-transcript matches all 12 requested words. This is a research sample, with identity assessment and live integration pending; generation took 6.39 s for 4.24 s of audio. See [voice-match.md](voice-match.md). One TTS model is verified, enabled for research, and explicitly downloaded; the other candidates remain disabled.

The in-memory audio-streaming follow-up emits its first signal chunk in 0.18 s with a resident model and prepared reference, generating 4.00 s of speech in 1.22 s. All 12 words again match the back-transcript. This establishes isolated streaming throughput on that sample. See the follow-up measurements in [voice-match.md](voice-match.md).

`a2a-pipeline --tts clone` now runs that model live through a resident worker (`research/tts_worker.py`, Swift `CloneVoiceSynthesizer`), with the reference either fixed (`--voice-ref`) or acquired from the source's first finalized utterances (`ReferencePolicy`). The app exposes it as an experimental, research-only toggle. File-mode runs on the real excerpt give a 0.16 s median TTS first chunk, but the cloned English runs 1.5–1.9× longer than the Mandarin it translates, and 23–24 of 29 commits wait for recognizer finalization: coverage is 16% at the 3 s / 4 s defaults and 45% at 6 s / 8 s. No live tap run with the clone path has been made. See "Live pipeline integration" in [voice-match.md](voice-match.md).

| Ticket | State | What exists | Verify with | Remaining / blocker |
| --- | --- | --- | --- | --- |
| T0.1 Repo bootstrap | **Done** | Section-6 layout. `apple/Package.swift` with `A2ACore`, `A2ARTShim` (C), `A2ATrace`, `A2AAudio`, `A2AMedia`, `A2AModels`, the CLIs, and FixturePlayer. uv harness, `AGENTS.md`, `CLAUDE.md`, `scripts/check.sh`, `.gitignore`. Contract tests for stale epoch/generation/canceled-unit rejection and trace/fixture validation. | `scripts/check.sh` | Hosted CI is not configured (no remote). A hosted runner would also need Xcode 27 / SDK 27. No commit has been made yet. |
| T0.2 Manifest and fetch | **Partial** | Qwen3-TTS 0.6B Base is verified and enabled for research, with all 13 converted artifacts pinned and downloaded. Other candidates remain declared and disabled; HY-MT stays restricted. The fetcher verifies size and SHA-256, resumes, promotes atomically, and generates notices. | `uv run --project harness pytest harness/tests/test_fetch_models.py`, `scripts/fetch_models.py --check` | Complete the first ASR/MT/TTS/VAD combination, source-conversion parity, and product qualification. Downloads remain an explicit setup step. |
| T0.3 Preflights | **Partial** | `a2a-preflight` covers host, Metal, SpeechTranscriber locales and assets, Translation availability plus a probe, Core Audio processes, and the default output. The zh_CN speech asset is installed (setup path). The CLI Translation failure is explained and fixed: Swift Build stamped `sdk 26.0` into executables, and Translation gates on it (`sdkVersionStamp()`, enforced by `check.sh`). Results are in `verifications.md`. | `a2a-preflight --json … --translate-probe` | Not started: speech-swift capability probes, the bundled XPC/MLX probe, TranslateGemma `zh` check, and ANE placement. |
| T0.4 Corpus and consent | **Pending** | Synthetic fixtures (`a2a_harness.synth`) unblock the harness without any human data. | — | People and legal work: consent form, recording protocol, speaker booking, public-set license table. |
| A1 Tap capture CLI | **Done (FixturePlayer)** | `a2a-tapctl permission/list/resolve/devices/taps/capture/fidelity`. Helper resolution goes through app-bundle ownership, rejects ambiguity, and excludes our own processes. A C IOProc feeds an SPSC ring, and the writer thread writes WAV. 30-minute DoD run (`raw/a1-capture.json`, `raw/a1-fidelity.json`): muted-when-tapped, correlation 0.999955, worst 10 s window 0.999906 (179 windows), 0 dropouts, 86,398,464 of 86,400,000 frames (first callback after 32 ms), 0 overflow / rejected blocks / record drops / invalid timestamps, 0 span gaps over 168,748 callbacks, host-vs-sample residual 4.76 ms over 30 min. | `.local/runs/a1/run.sh` (interactive) | Zoom helper membership on a real Zoom install is untested. |
| A2 Mute and re-render | **Partial** | `a2a-tapctl monitor`: muted-when-tapped, re-render through the two-bus mixer, delay 0–3 s, per-bus gain, crossfade test. Mixer ramps are verified click-free in unit tests (max step under 0.002 per sample). The live pipeline runs the same path on a real player (ffplay): source muted, original bus re-rendered, translated bus mixed in. | `swift test --filter EngineTests`; interactive: `a2a-tapctl monitor --bundle dev.a2a.FixturePlayer --crossfade-every 2` | Physical loopback or click-track latency measurement (≤60 ms target) and an audible double-playback check. |
| A3 Lifecycle | **Partial — fails** | `a2a-tapctl lifecycle` (`raw/a3.json`; first run `raw/a3-run1.json`). 50/50 stop/start cycles clean: each private tap left the system list 7 ms median / 9 ms max after destroy (an immediate re-check races the asynchronous destroy, which run 1 misread as 25 leaks). `kill -9` while muted, 5/5: coreaudiod did **not** destroy the dead host's public tap (still registered more than 60 s later); a separate supervisor process destroyed each one (status 0). | interactive: `a2a-tapctl lifecycle --bundle dev.a2a.FixturePlayer --json reports/week2/raw/a3.json` | Product taps are private, so another process cannot see or destroy them: whether a private muted tap outlives a crashed host, and whether the source stays muted, needs an output loopback test. Device-change, sleep/wake, and permission revoke/regrant are not automated. **Takeover stays blocked until A3 passes.** |
| M1 Model benchmarks | **Pending** | Adapter seams exist. Apple Translation works in the test host. | — | Weights and dev-set audio |
| M2 Voice reference study | **Pending** | — | — | TTS models, 8 consented speakers, blinded listeners |
| M3 Concurrency benchmark | **Pending** | `resource.sample` (phys_footprint/RSS) and queue telemetry exist. | — | Real models, plus a Zoom call on the same machine |
| M4 Commit-policy simulator | **Partial** | `CommitPolicy` v0 (LocalAgreement-2 stability, monotonic frontier, closed units, negation/number/dangling holds, a 900 ms reconsider window that never forces an unsafe split, a forced-length cap). `SessionPipeline` reducer. Golden traces: basic unit, fallback with stale-generation rejection and recovery, acquisition without profile, discontinuity, MT validation. | `swift test --filter "CommitPolicyTests\|GoldenTraceTests"` | Replay of *logged* ASR partials from M1. Comparison against wait-k, continuation, and sentence-end baselines. Speaker-handoff golden (state-machine test only so far). |
| H1 Replay harness | **Done (plumbing)** | `harness/run.py` offline (virtual clock, byte-reproducible) and paced (stdin at real-time pace, feed log, verified no future-audio access). Run directory holds the trace, output/stems WAV, `metrics.json`, `report.md`, Perfetto JSON, and host metadata. Live tap runs come from `a2a-pipeline --tap-…` and are checked by `a2a_harness.live_check`. | `uv run --project harness pytest harness/tests/test_runner.py` | `run.py --mode tap` itself still raises; use `scripts/live_demo.sh`. |
| H2 Metrics | **Partial** | `lag_onset`/`lag_live`/`lag_end` with independent reference alignment, translation coverage over the warm interval, source disposition, debt and source-age slopes, CER/WER, streaming-gate row checks, future-access checker. | `uv run --project harness pytest harness/tests/test_metrics.py` | COMET, back-transcription WER, independent speaker similarity, UTMOS, and the LLM judge. These go in `research/` with their own environment. |
| E0 Streaming demo | **Partial (default thresholds fail)** | `a2a-pipeline --tap-pid/--tap-bundle` translates another app live: Apple SpeechTranscriber (zh_CN) → Apple Translation → system voice, played through the tap mixer while the source is muted. `scripts/live_demo.sh` plays a Mandarin video in ffplay and checks every sentence plus a back-transcript of the audio actually played (results below). | `scripts/live_demo.sh --max-debt-seconds 6 --max-source-lag-seconds 8` (interactive) | **Plumbing only** with the system voice. `--tts clone` adds a research-only cloning path (file mode only so far; identity unqualified, so still not E0 evidence). Needs a qualified cloning model and identity study (M2), clause-level commits and duration control (M4), and A3 before takeover. |

## Plumbing soak results (fixture adapters, not model evidence)

The input is 10 minutes of synthetic tone-burst "speech" with scripted ASR, a dictionary MT, and a tone TTS. The TTS speaks at 0.32 s per English word, which runs longer than the Mandarin source.

| Run | `lag_onset` median / p95 | Coverage | Fallbacks | Debt slope | Source-age slope | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Offline, virtual clock (`.local/runs/soak-offline-10min`) | 2.70 / 3.22 s | 0.896 | 17 (`debt`) | 0.9 ms/min | 1.9 ms/min | 235 units, 68,599 events, schema valid, 0 drops |
| Paced, real time, first build (`.local/runs/soak-paced-10min`) | 2.36 / 2.86 s | 0.963 | 6 (`debt`) | −6.8 ms/min | 1.1 ms/min | 68,242 events, schema valid, 0 drops, **no future-audio access** (feed-log check). Two defects found: (1) the host clock skipped the fixture adapters' modeled latency, which is why this run looks better than offline; (2) the pipeline footprint grew about 0.7 MB/s (9 MB to 436 MB) because the stdin reader `Thread` called `FileHandle.availableData` with no autorelease pool. Both are fixed, see the next row. |
| Paced, real time, after fixes (`.local/runs/verify-paced-3min`, 3 min) | 2.72 / 3.24 s | 0.898 | 5 (`debt`) | 5.7 ms/min | −8.0 ms/min | Matches the 3-min offline run (2.70 / 3.22 s, 0.898, 5 fallbacks). No future-audio access. `leaks` reports 0 leaks, and the heap is flat between 30 s and 150 s. |
| Paced, real time, ring pages pre-faulted (`.local/runs/verify-paced-90s`) | 2.73 / 3.13 s | | | | | Footprint is flat at 23 MB over 84 s. The earlier slow growth was first-touch paging of lazily mapped `calloc` rings, which also meant page faults on the RT path. Ring and mixer memory is now pre-faulted at creation. |

What the soak shows about the policy: sentence-final commits put `lag_onset` at about the unit's own duration plus roughly 0.9 s of processing. Translated speech that runs longer than its source accumulates debt until the 3 s threshold forces a fallback. That is the M4 tuning target, along with clause-level commits and TTS pacing (risk register: "English output is longer than the Mandarin source").

## Live translation results (Apple frameworks, system voice: plumbing only)

The source is `.local/fixtures/zh-demo/video.mp4` (`a2a_harness.zh_demo --lead 4`): six Mandarin meeting sentences from the zh_CN system voice, with 3 s gaps, 38 s total, played by ffplay. `a2a-pipeline` taps ffplay by PID (muted while tapped) and plays English on the MacBook Pro speakers. `live_check` aligns what the pipeline heard with the clip (correlation 1.0 in every run), requires every sentence's keywords in the English that was played, and back-transcribes the played translated audio with the en_US SpeechTranscriber.

| Run | Thresholds (debt / source lag) | Sentences played | Fallbacks | Errors | `lag_onset` median / max | Back-transcript WER | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `.local/runs/live-demo` | 6 s / 8 s | 6 / 6 (7 units) | 0 | 0 | 3.04 / 5.36 s | 0.00 | **pass** |
| `.local/runs/zh-live2` | 6 s / 8 s | 6 / 6 (8 units) | 0 | 0 | 2.41 / 4.17 s | 0.00 | **pass** |
| `.local/runs/live-demo-continuous` (0.8 s gaps, 25 s) | 6 s / 8 s | 6 / 6 (7 units) | 0 | 0 | 4.06 / 5.38 s | 0.00 | **pass** |
| `.local/runs/live-demo-defaults` | 3 s / 4 s (plan defaults) | 4 / 6 | 2 (`debt`) | 0 | 2.22 / 3.56 s | 0.04 | fail |

The 3 s gaps in the main clip let the English catch up. With near-continuous speech (`--gap 0.8`) the run still passes at 6 s / 8 s, but lag rises by about 1.6 s; longer continuous speech than this 25 s clip has not been measured and will fall back more often.

Example output: "Today we will discuss the plans for the next quarter.", "We disagree with this plan.", "The budget needs to be increased by 20%.", "Please send me the report before Friday."

With the plan's default thresholds, any sentence of about 13+ characters fails over to the original. Debt counts the committed unit's projected English at 0.25 s per source character (measured: 0.17–0.31 s per character for the system voice), so a single 14-character unit already projects 3.5 s. That is exactly the M4 target: clause-level commits that keep units short, plus TTS pacing. The relaxed thresholds are a demo parameter, recorded in each run's `summary.json`, never a gate setting. Lag is measured from device capture time of the speech onset to the mixer's presentation time of the first English sample. It excludes output-device latency (about 3 ms reported).

Known gaps found by these runs: in file mode, the recognizer produced a one-word "you" from the trailing digital silence (not seen live); a VAD-overlap guard on finalized units is the likely fix. The live tap source has no bundle ID (ffplay), and QuickTime/Safari playback has not been run yet.

## Next actions, in order

1. A3: output-loopback check of a private muted tap after `kill -9` (built-in mic and speakers, or a loopback device), plus supervised device-change, sleep/wake, and permission cases. Takeover stays blocked until it passes. A2: loopback latency (≤60 ms) with wired headphones and a cable.
2. M4: clause-level commits and TTS pacing so the plan's default 3 s / 4 s thresholds hold on real sentences (the live demo fails 2 of 6 sentences with them today). The VAD evidence guard is now implemented and deterministically tested; it still needs runtime qualification.
3. T0.2/T0.3: pin speech-swift and the first MLX MT/TTS pair in the manifest (verifier plus evidence), add the selected packages, and build the XPC target for the MLX worker probe (ADR-0002). The SwiftUI app shell now exists; the cloning adapter and worker do not. See model-capabilities.md for the pinned source audit.
4. Dev-set metrics with and without `hy-worker --no-merge-prompt` (COMET plus onset and underruns) to bless the single-batch turn as quality-neutral, or revert it. See [inference-latency.md](inference-latency.md).
5. T0.4 consent and recording, which has the longest lead time.

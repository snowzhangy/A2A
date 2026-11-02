# Closing the gap with dub-anything — 2 November 2026

[The first review](dub-anything-review.md) compared A2A with
[dub-anything](https://github.com/swax/dub-anything) on one film excerpt and listed what still
separated them. This is the program: every quality mechanism the reference dub has, whether a live
pipeline can have it at all, and where A2A stands today. It is written against that project's
`docs/research.md`, which records what its authors measured and what a listener chose — the
closest thing to a quality specification either side has.

**The boundary, stated once.** dub-anything is offline and agent-directed: it has the whole file,
can listen to a rendered line and re-roll it, and spends hours of GPU per feature. A live pipeline
cannot re-render a line that has already reached the listener, and cannot know a phrase before it
has been spoken. Parity therefore means *losing nothing that latency does not force*, and being
honest about the rest. Every row below says which of these it is: **live-possible**, **live-limited**
(partial, for a stated reason), or **offline-only**.

## The levers

| Reference mechanism | Why it matters there | A2A today | Verdict |
| --- | --- | --- | --- |
| Voice-free background: music, laughter and effects stay in the original | Between lines you hear the film, not silence | Shipped: ADR-0008, `AUSoundIsolation` off the RT thread, its own bus and delay ([spike](dub-anything-review.md)) | live-possible, done |
| Casting: every line gets its actor's voice, chosen across the whole file | A dub with one voice is not a dub | Shipped: `--speakers auto`, per-speaker cloned references, first profile pinned (ADR-0005/0009). The DSP tracker still merges same-gender actors and fails under music ([speaker-slots.md](speaker-slots.md)) | live-limited; the tracker is the weak part, not the cloning |
| References cut from separated vocals | Cleaner clones, less noise between words (their finding 4) | **Not done.** References are cut from the mix, music and all, by `ReferencePolicy` | live-possible, open |
| Per-line delivery conditioning: each line's reference is the actor's clips **plus the actor's own original of that line** | The single biggest acting gain: a shout stays a shout, a murmur stays a murmur (findings 6, 17-20; kept on for Mandarin → English, finding 20) | **Built, off by default** (`--clone-delivery line`, [ADR-0015](../../docs/adr/0015-per-line-delivery-conditioning.md)): the unit's own audio is appended to its speaker's reference for that take, and the worker reports whether it could be used. Measured: +30–50 ms to first audio, and the take carries 2.2× the actor's register movement | live-possible, shipped as a research knob; unlistened-to, so it is not the default |
| Timing: every take is re-timed to end where the actor stops | Their listeners ranked the rhythm of the exchange **at least as high as the acting** (findings 7-8) | **Shipped today** ([ADR-0013](../../docs/adr/0013-turn-timing-fill.md)), plus the metric that reference uses: dead air and overrun per unit | live-possible, done within a stretch bound |
| Length: the translation is *written* to fill the actor's time, with the take's length predicted before rendering (`fit.py`) | Fills the time instead of needing fill; their finding 3 was that English ended far too early on many lines and nothing flagged it | **Partial.** Over-length is cut after synthesis (`TargetShortener`, simultaneous-interpreter deletion) and under-length is now stretched; nothing asks the translator for a length. The cut and the ask use the same rate constant, so they cannot disagree | live-limited: needs a translator that takes an instruction. Apple Translation does not; the research `--mt context` worker is the place, and `mlx_lm` is not installed in `research/` today |
| Per-line QA: every rendered line is transcribed back, garbles re-rolled, best take kept | Nothing garbled reaches the listener | **Offline-only in general** — a line already played cannot be re-rendered. A2A retries only a take that produced no voiced audio. Live-possible subset: re-roll before playout when the unit still has slot slack, and after-the-fact flags in the report | both; the pre-playout subset is open |
| Terminology and pronunciation control | Names survive the round trip | Shipped: `--glossary`, `--pronounce` ([review](dub-anything-review.md)): 1/6 → 6/6 agreed terms rendered as agreed | live-possible, done |
| Transcript repair: several recognisers, burned-in captions, an agent that fixes what ASR misheard | ASR is the ceiling on what can be said | Single Apple recognizer; no caption reading; no repair pass | live-limited: the picture is already captured (ADR-0012), so burned-in text is reachable; multi-recogniser voting costs latency |
| Text on screen translated as subtitles | Titles and signs | Not done | live-possible, open, depends on the same caption reader |
| Instruments: `eval/timing.py` (dead air / overrun), `eval/score.py` (delivery correlation, likeness, quality, accent), `lineqa.py` | Their defaults were picked **by ear against measurements**; without them "better" is a feeling | Timing: shipped today in the harness. Delivery correlation, SQUIM-style quality and accent: not ported. Likeness: `research/speaker_similarity.py` exists but is circular on our own clones ([voice-match.md](voice-match.md)) | live-possible; timing done, delivery instruments open |

## What changed today: each line takes its actor's time

A2A paced speech one way only: a take longer than its source was sped up to 1.3×, a take shorter
than it was played short, and the rest of the actor's turn went by with nothing on the bus. The
reference measures exactly this (dead air) and fixes it by re-timing; A2A had neither the fix nor
the number.

- `SynthesisJob.allowFill` is the reducer's permission, granted only with a picture delay
  (ADR-0012 slots), on a unit with real token timing, and while the unit is **not yet late for its
  slot**. A filled line ends at `slot + its own speech` — where the actor stops, never later — so
  it cannot push the phrase after it, which still waits for its own slot. Without a delay nothing
  changes: playing early is the point there.
- The worker owns the number: `choose_speed(..., allow_fill=True)` returns `estimated / target`
  clamped to `[0.85, 1.3]`, and leaves a deficit under 0.25 s alone — the same tolerance the
  speed-up side has had. `StreamingWSOLA` now stretches as well as compresses (exact duration,
  streamed equals whole-buffer, overlaps in phase down to 65 Hz).
- The target is the unit's own span, whose end is the detector's last voiced sample. A clause
  committed in the middle of a turn is filled to its own pause, not to the end of the sentence.
- `tts.request` records `fill`, `tts.done.pace` can be below 1, and `a2a-pipeline --no-turn-fill`
  turns the whole thing off for an A/B listen (`turn_fill` in the summary).
- Harness: `turn_dead_air_s`, `turn_overrun_s`, and a signed `turn_gap_s` per unit.

### Numbers

Deterministic replay, `synthetic-zh-90s`, scripted ASR, dictionary MT, `--tts clone` with the
standard-library fake voice, `--pace realtime`; three runs differing only in slot delay and fill
(`.local/dub-parity/turn-fill/`, `measure.py` re-runs it):

| Run | Played | Filled takes | Pace median | Dead air median / max (s) | Dead air total (s) | Turns ending within 0.25 s of the actor |
| --- | --- | --- | --- | --- | --- | --- |
| Live playback, no slots | 33 | 0 | 1.00 | 1.47 / 1.92 | 43.26 | 0 of 33 |
| Slots + fill (ADR-0013) | 33 | 32 | 0.85 | **1.38 / 1.87** | **41.04** | **3 of 33** |
| Same slots, `--no-turn-fill` | 33 | 0 | 1.00 | 1.47 / 1.92 | 43.26 | 0 of 33 |

The control row matters: with the same 6 s picture delay and `--no-turn-fill`, the replay is
indistinguishable from the no-fill run, so every difference above is the pacing and nothing else.
33 of 33 lines are the same lines in all three runs — coverage is untouched.

Two things in these numbers matter more than the totals. First, the fake voice produces 0.3–0.4 s
takes in 1.5–2.2 s of speech: the 0.85 floor binds immediately, so fill recovers a slice of the
gap, not the gap. That is the same wall the reference hit from the other side — a take's length is
set by the words, and re-timing can only move it a little. Their answer was to write the text to
the time available; ours is the same lever (§Length above). Second, the run's *estimate* of take
length is what the pace is chosen from: Qwen has a per-speaker average, OmniVoice has a reference
rule accurate to about ±0.2 s. Fill is only as good as that prediction, so the OmniVoice path
should ask the model for the longer duration directly (`duration_s`) rather than stretch afterwards
— the reference found the same thing about asking for a slower speed (their finding 9).

## Evidence, and what it is not

`scripts/check.sh` passes: 267 Swift tests (6 new `TurnFillTests`), 123 harness tests including
`test_turn_fill.py` (pace choice, stretch math) and a driver-level test that runs pipeline → worker
protocol → playout → metrics and asserts the `--no-turn-fill` control. **No listener has heard the
stretched takes**, and the fixture's voice is a tone generator: this is the mechanism and its
instrument, not a quality verdict. Identity, naturalness and the product gates are unchanged, and
cloning stays research-only (`tts_identity_qualified: false`).

## Delivery: does a shout survive the pipeline?

The instrument landed before the feature, because without it every delivery claim is unfalsifiable:
BLEU and SECS say nothing about whether a shout survived. `research/dub_delivery.py` scores the
**contour** of register — pitch (YIN), vocal effort (spectral tilt, the cue their listeners voted
strongest), and level — as rank correlation against the actor's own lines, because a dubbed voice is
never the actor's absolute loudness: the target is *moving where the actor moved*, and the offset is
reported apart (`shape`) from the follow-through. It also reports `dynamics`, how much of the
actor's movement reaches the take at all, and the noise floor between lines.

Same English lines, same pinned profile, one real episode excerpt, five actor lines
(`.local/dub-parity/delivery/ab/`; research audio, aggregates only):

| Conditioning | arousal | pitch | effort | level | dynamics | shape | floor dBFS |
| --- | --- | --- | --- | --- | --- | --- | --- |
| pinned profile | 0.10 | n/a | −0.30 | 0.60 | **1.28** | 1.03 | −22.1 |
| + the line's own audio | −0.20 | n/a | **+0.30** | 0.30 | **2.76** | 1.96 | −36.3 |

**Dynamics more than doubles**: the conditioned takes carry movement the pinned ones flatten away.
The overall arousal correlation does not improve, and five lines cannot settle that — the interval on
a five-point Spearman is wider than the effect, so the honest reading is *the mechanism moves real
signal; whether it sounds better is unmeasured*. `shape` worsening is expected rather than damning: a
pinned profile sits at the session average, a conditioned line sits where that line actually was.

Cost on the same scene: **+30–47 ms** of reference preparation per line, **+30–50 ms** to first
audio — real money on a live budget, and precisely the cost ADR-0009's pinning was invented to hide.
The appended clip also lifts the bed between lines (−22 → −36 dBFS), which is one more reason
isolated references are the next item.

## Next, in order of what a listener would notice

1. **A listening test for delivery conditioning, and a corpus long enough to score it.** The flag is
   built and costs are measured; no ear has judged it and five lines cannot resolve a correlation.
   DoD: a blinded A/B of pinned vs conditioned takes over a full scene per speaker, scored with
   `research/dub_delivery.py` on ≥ 20 lines, and a decision on whether it becomes the default.
2. **Isolated-voice references** (their finding 4). DoD: reference cuts taken from the ADR-0008
   isolation path rather than the mix, with the noise floor between words and a reference-free
   quality estimate measured on the same lines both ways.
3. **A length ask, then a length rule.** Send the translator the seconds it has
   (`TranslationValidator` already computes that budget for the cut), starting with the research
   `--mt context` worker; `mlx_lm` must be added to the `research/` environment first (the retired
   `.local/live-dub/venv` already carries `mlx_lm 0.32.2` — take the version from there, do not run
   measurements out of that venv). DoD: on the
   dev set, `english_over_source_seconds` and `truncated` units fall with number/negation/glossary
   preservation checked against the unbudgeted answer, before any quality claim.
4. **Port the delivery instrument** (`eval/score.py`'s pitch/effort/loudness correlation, SQUIM
   quality, accent confidence) into `research/`, so step 1 can be scored rather than felt.
5. **Pre-playout re-roll where the slot has slack** — the live subset of their QA loop.

# Whole-sentence units and timestamped playout: research

2 November 2026. This research compared picture synchronization, continuous synthesis,
sentence-sized units, and latency. The subsequent implementation is recorded below.

Evidence comes from one live app session: 605 s, a drama with several speakers, Hy on this Mac, `--fluent --adaptive-prebuffer --clone-mode xvector --dual-clone`, picture delay D = 8 s. The build includes ADR-0016. The trace and the analysis scripts (`sync.py`, `live_status.py`) are under `.local/scratch/live-monitor/`, which is gitignored. Numbers only here.

## Update: implemented as ADR-0017

Implemented:
- design A (run-ahead);
- C.1–C.3 (pause detection on the isolated voice, a cap of D − 2.5 s, the dip cut).

Not implemented:
- joining short phrases (C.2's minimum length), because it can merge two speakers into one voice;
- Hy punctuation (C.4), left as a follow-up.

The note's figures assumed a 2.0 s minimum unit and joining; the shipped rule has neither.

Replays used the app's live flags, D = 8 s and `--speakers auto`, comparing the current build against the new one:

| Clip | Units closed at the cap | Unit length, median | Slack p10 / median | End within 0.25 s | Worst start | Ran on |
|---|---|---|---|---|---|---|
| Movie, 120 s | 22/44 → 0/34 | 2.70 → 1.48 s | 0.50 / 2.02 → 2.61 / 4.72 s | 32% → 50% | +0.22 → +0.17 s | 3% → 0% |
| intro110, 93 s | 8/39 → 1/34 | 1.72 → 1.81 s | 0.10 / 1.29 → 2.77 / 4.42 s | 59% → 56% | +0.12 → +0.17 s | 3% → 0% |

Notes on the replays:
- **Longest units:** the longest units (about 5 s) kept only 0.23 s and −0.11 s of slack against their slot. So the D − 2.5 s cap formula held on these replays, not merely as an estimate.
- **Pace and shortening:** on the movie clip, lines at the 1.3× ceiling rose from 38% to 50%, and shortened lines from 15/39 to 17/34. A pause-ended unit's budget is its speech only, while the pause after it could also absorb the line. A budget that includes the following silence is the next step.

## Answer in short

- **Timestamp scheduling already exists and works.** ADR-0012 gives each unit a slot: its source start time, through the capture anchors, plus D. Audio is held until that slot.
  - Starts land a median 0.03 s from their slot.
  - All 36 starts that were more than 0.2 s late had the same cause: the line before was still playing.
  - No start was late because its audio was not ready.
  - So the scheduler is not the sync problem. Line *length* and line *shape* are.
- **Units are 3 s pieces, not sentences.**
  - 74% of units (139/187) close at exactly 3.00 s. A fixed cap in `AudioSegmentPolicy` cuts them, mid-word, not at a pause.
  - The VAD reported only 41 speech ends in 605 s of dialogue.
  - Units that closed at a pause are much better in sync: 45% end within 0.25 s of the actor, against 23% for capped ones.
  - Capped units also leave no gap after them, so any overrun pushes the next line late.
- **The two threads exist, but one rule couples them.**
  - Translation, synthesis and playback already run independently.
  - With `--dual-clone`, `canStartSynthesis` (main.swift:1381) starts a line's synthesis only once the line before is playing. 181 of 191 lines waited on it.
  - That wait is a *spacing* limit, not a delay cost. A line's slack is about: (gap between its source start and the previous one's) − 0.28 s − (its generation time). Median error of that model: 0.05 s.
  - So the lines that nearly missed their slot are the quick ones: 22 lines with under 0.3 s of slack, whose median start gap was 1.73 s.
  - A shorter D does not make this worse, and a longer D does not fix it. Running ahead does.
- **Latency is set by the longest unit, not by the queue.**
  - The smallest workable D is about: longest unit + VAD hangover + MT tail + TTS to first playable audio + reserve.
  - Today's 3 s units would fit at about D ≈ 5 s.
  - Sentences up to 4.5 s need about 6.2 s; up to 6 s, about 7.7 s (table in section D).
  - So "whole sentences" and "lower latency" do trade off. Choose the longest sentence, and D follows.

## What dub-anything does (the goal)

From `Research/dub-anything` README and `docs/research.md`:

1. **A line is a window that ends in a pause** (findings 10 and 14, plus the `dips` step). Fixed-length cuts are never used.
   - A window that cuts the actor mid-sound produces garbled takes.
   - Pauses too short for speech detection still show as 20–32 dB dips within 0.15 s, so windows are cut at those dips.
2. **Each line takes the actor's time** (findings 7–8).
   - Takes are re-timed 0.8–1.25× so each one ends where the actor stops.
   - "The rhythm of the exchange — lines taking the time the actor takes, turns landing where they did — mattered at least as much as the acting."
   - After one fill pass, 80% of cloned takes end within 0.25 s of the actor (median 0.13 s); with IndexTTS's duration control, 92%.
3. **The length is predicted before rendering,** so the translation is written to fill the window (finding 9).
4. **Each line is acted from the actor's own delivery of that line** (finding 6). A2A has this as ADR-0015, off by default.
5. **Every line is transcribed back; a garbled one is re-rolled.**
6. **The original soundtrack plays between lines.** A2A does this with the separated bed (ADR-0008).

Dub-anything is offline, so it can wait for whole lines, write the translation to length, and re-roll as often as it needs. Live, we have only the delay D, and Hy's translation cannot be directed to a length. The rest of this note is about spending D the way dub-anything spends unlimited time.

Published work on automatic dubbing points the same way:

- Amazon's prosodic-alignment and isochrony papers segment the translation so its phrases and pauses match the source's, and they also tune a smoothness term: how much the speaking rate changes between neighbouring segments.
- Their rate-control paper notes that phrase-separating punctuation usually brings a pause of at least 150 ms.
- Streaming speech-translation work finds that translating fixed durations breaks acoustic units, and that segmenting by acoustics plus translation history gives a better quality–latency trade-off.
- Hibiki (Kyutai) times each target word to the earliest moment the source supports it. Its follow-up, NaturalFlow, reports that this leaves many pauses, so per-word lag alone does not sound smooth.

## Measurements (live session, 605 s)

| What | Value |
|---|---|
| ADR-0016 outcome: takes that ran to the token cap | 10% (was 79% in the earlier live run) |
| Speech pace / pace at the 1.3× ceiling | 211 ms per character (was 378) / 31% |
| Voices ready (4 speakers) | 3.5, 10.0, 106.3, 248.7 s |
| Catch-up skips | 0 |
| Units closed by the 3 s cap / by a pause; VAD speech ends | 139 / 48 (of 187 played); 41 |
| Start error vs slot: median, p90, max | +0.03 s, +0.48 s, +2.37 s |
| Late starts (>0.2 s): line before still playing / audio not ready | 36 / 0 |
| End within 0.25 s of the actor: capped / pause-closed units | 23% / 45% (dub-anything: 80–92%) |
| End error p90, capped / pause-closed units | +0.66 s / +0.53 s |
| Lines that ran into the next line's slot: at the 1.3× ceiling, and characters per source second | 40 lines: 45% and 5.3 (the other 146: 28% and 4.7) |
| MT, request to result: median, p90 | 0.33 s, 0.48 s (Hy streams while the span is spoken) |
| TTS first chunk after the gate opens: median, p90 (n = 181) | 0.28 s, 0.46 s |
| TTS first chunk, lines that never waited: median, p90 (n = 10 only) | 0.35 s, 0.85 s |
| First chunk until playable (2 chunks and 0.24 s, the adaptive prebuffer): median, p90 | 0.10 s, 0.17 s |
| Slack: slot minus TTS done, median and p10 | 1.45 s, 0.09 s |
| Lines shortened to fit the time budget | 69/195 |

End error means little for capped units: the actor did not stop at 3.00 s. The pause-closed row is the fair comparison with dub-anything.

Why so few speech ends? The detector is `EnergyVAD` at −40 dB with a 300 ms hangover, and it runs on the **mix**. The hypothesis is that under a music or effects bed the level rarely drops below −40 dB, so no speech end comes and the cap fires every 3 s. The VAD-on-voice arm of the sweep below tests this.

The host times in `sync.py` are what the scheduler planned. Output-device and display latency are not in them; the app's ±500 ms picture fine-tune covers those.

## Design

### A. Producer: run ahead to a horizon set by slot time

The producer is segmenter, then Hy, then the TTS lanes. Replace `canStartSynthesis`'s "front is playing" and "at most 2 plays" with a horizon: any unit whose slot is within H seconds (H ≈ D) may synthesize when a lane is free.

- Finished audio waits in the reducer until its slot, as it already does.
- Order is set by slot, not by finishing order.
- Memory cost is trivial: about 0.5 MB for 6 s of 24 kHz float.
- No new thread is needed. The reducer and render callback are already the timestamped consumer.
- What it fixes: quick exchanges, meaning lines whose source starts come closer together than about 0.3 s plus their generation time. It does not lower the smallest D.
- Without a picture delay (D = 0), keep today's rule: running ahead there only makes lines late.
- Risks:
  - Two Qwen lanes plus Hy 30B all running ahead share the GPU. `resource.sample` records memory only, so first-chunk p90 under contention has to be measured.
  - Lines alternate between the two clone workers more often, and each worker learns gain and pace separately. That bears on the "voice not consistent" report, so share or pin the learned pace across lanes.

### B. Consumer: keep it, then fix line length

The slot scheduler is not the bottleneck: starts are a median 0.03 s from their slot, and no start was late for lack of audio. Every late start came from an earlier line overrunning.

The lines that overran carried more text: 5.3 characters per source second against 4.7, and 45% were at the 1.3× ceiling against 28%. Two levers, with a trade-off:

- **Pauses between units.** A pause-ended unit is followed by silence, which absorbs a small overrun. Units cut back to back at the cap have nothing to absorb it. This comes free with section C.
- **Text budget against speed.**
  - The target budget uses 0.25 s per zh character, while clean takes run 0.18–0.21. That rule shortened 69 of 195 lines.
  - Loosening it keeps more of the translation, which is *accuracy*. But it puts more text in the same window, which costs *sync*.
  - Tightening it does the reverse.
  - Dub-anything avoids this by writing the translation to length. Hy cannot be asked for a length, so here the budget is a dial between accuracy and sync, not a free fix. Tune it after section C lands, because longer units change the ratio.

Sample-accurate placement is a later refinement, not a fix: it means writing each unit at the output sample its slot maps to, instead of releasing it on a 20 ms tick.

### C. Units are sentences, bounded by D

1. **Find pauses in the voice, not the mix.**
   - Run the VAD on the isolated voice: the separator's input minus the bed, with latency aligned.
   - Or use a voice-probability VAD, as the `EnergyVAD` doc comment already plans with Silero.
2. **Close a unit:**
   - at the first pause of at least P after a minimum length;
   - at any speaker change (`spk.change` exists).
   - The close is only known after the VAD hangover, so P and the hangover add directly to latency.
3. **Hard cap tied to the delay:** L_max = D − (hangover + MT p90 + TTS-to-playable p90 + reserve) ≈ D − 1.7 s. At the cap, cut at the deepest energy dip in the last ~1 s (dub-anything's `dips`), never at a fixed sample.
4. **Second option, smaller latency cost:**
   - The Hy worker already sends `{"ev":"delta","text":…}` for each turn that commits text, including on streamed spans (`research/hy_worker/hy_worker.cpp:18, 848`). The driver drops it (`HyLocalTranslator.swift:286`).
   - Sentence-final 。？！ in that stream marks where the *target* sentence ends, within about one 800 ms turn.
   - A unit could close at that turn, at the nearest dip, without waiting for a long source pause.
   - Driver work only, but it needs a test of how reliably Hy places punctuation turn by turn.

Settings tied to the 3 s that must change together:

- `AudioSegmentPolicy()` default (main.swift:670). It has no flag: `--max-unit-seconds` sets the text-path `commit.maxUnitSourceSeconds` only, so a sweep needs a new flag.
- `AudioStreamFeed(maxSamples:)`, built from the policy's `maxSamples`.
- `VoiceprintSpeakerTracker.maxSegmentSamples` (3 s). Keep 3 s pieces for labeling, but let a unit span several same-speaker pieces, and close it when the label changes.
- The target length budget and shortening (section B).
- `--max-source-lag-seconds 2` and the pending-unit cap, which both count from the slot.

### D. The smallest D for each rule

Under `--fluent --adaptive-prebuffer`, a slotted unit may start once it has 2 chunks and 0.24 s buffered and generation keeps ahead. Generation ran about 2× real time, so no whole take is needed.

A unit makes its slot when:

L + (VAD hangover, pause-closed only) + MT tail + TTS to playable + reserve ≤ D

where L is the unit's length. Measured p90s: hangover 0.30 s, MT 0.48 s, TTS 0.46 + 0.17 = 0.63 s. Reserve: 0.3 s.

| Segmentation | Longest unit | Smallest D (estimate) |
|---|---|---|
| Today: 3 s cap | 3 s | about 4.7 s (a 3 s pause-closed unit is the worst case) |
| Sentences, cap 4.5 s | 4.5 s | about 6.2 s |
| Sentences, cap 6 s | 6 s | about 7.7 s: fits today's D = 8 |

These are estimates from one session's p90s. The run-ahead contention in section A may raise the TTS term. Choose the cap, and D follows; never set the cap independently of D.

## Validation plan

Replay with `.local/scratch/tts-runon/replay.sh` (live flags) on intro110 and the movie clips. Sweep:

- segmentation: today, pause rule with P ∈ {150, 300} ms, cap ∈ {4.5, 6} s, VAD on the mix vs the voice;
- run-ahead: off vs on;
- D ∈ {5, 6, 8} s.

For each run, report:

- the share of units closed by the cap;
- start error p90, and how many late starts came from an earlier line overrunning;
- end error p90 and the share within 0.25 s, pause-closed units only;
- slack p10, and slack for lines that start under 2 s after the previous one;
- ran-on %;
- shortened lines;
- the smallest D whose slack p10 is above 0.

Pass when:

- most units close at a pause;
- no late start comes from an overrun;
- end within 0.25 s reaches dub-anything's level: 80% or more;
- all of this holds at the D the section D table gives for the chosen cap.

Smoothness is judged by ear on the same clip, as dub-anything picks its defaults.

## Decisions for the user

1. **Longest sentence against delay.** 3 s pieces at D ≈ 5 s, sentences up to 4.5 s at about 6.2 s, or up to 6 s at today's 8 s.
2. **Run-ahead:** recommended at any D. It removes the near-misses in quick exchanges and is the "translate continuously, play by timestamp" split asked for.
3. **Hy punctuation (C.4):** try it alongside the pause rule? It may give sentence-shaped units with less waiting for source pauses.

## Sources

- Dub-anything: `README.md` and `docs/research.md`, findings 6–11 and 14 (local checkout at `Research/dub-anything`).
- [Prosodic Alignment for off-screen automatic dubbing (Virkar et al., Interspeech 2022)](https://arxiv.org/pdf/2204.02530)
- [Isochrony-Aware Neural Machine Translation for Automatic Dubbing (Tam et al., Interspeech 2022)](https://ar5iv.labs.arxiv.org/html/2112.08548)
- [Intra-sentential speaking rate control in neural TTS for automatic dubbing (Amazon)](https://cdn.amazon.science/2b/54/3665414b4ef5b3df9be4edeac3af/intra-sentential-speaking-rate-control-in-neural-text-to-speech-for-automatic-dubbing.pdf)
- [Duration modeling of neural TTS for automatic dubbing (Effendi et al., ICASSP 2022)](https://assets.amazon.science/52/07/585fa2364bb39d8661326ab19bc7/duration-modeling-of-neural-tts-for-automatic-dubbing.pdf)
- [MoSST: fixed-duration waiting breaks acoustic units (arXiv 2109.07368)](https://www.arxiv.org/pdf/2109.07368)
- [Learning when to translate streaming speech (ACL 2022)](https://aclanthology.org/2022.acl-long.542)
- [High-Fidelity Simultaneous Speech-To-Speech Translation, Hibiki (arXiv 2502.03382)](https://arxiv.org/html/2502.03382v1)
- [NaturalFlow: reducing disruptive pauses in simultaneous S2ST (arXiv 2606.13121)](https://arxiv.org/pdf/2606.13121)

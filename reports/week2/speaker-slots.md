# Everyone in the first voice: speaker slots, and what they do not fix

1 November 2026, night. User report: "most of the time, if multiple speakers are in the stream, it always uses the
same voice tone from the first." This is a tracker problem, not a routing one: when the tracker does name two
speakers, they get two voices (session 10:12 below). The tracker fails in three ways. This page fixes one of them:
speaker slots held by passing sounds. It is a partial fix. In most of the user's sessions the slots never filled,
and there the DSP features themselves merge the actors, which nothing here changes.

## What the app's own traces show

Eight app sessions from 31 October and 1 November (`.local/live-dub/runs/app-sessions/`, plus one leftover in
`$TMPDIR`; labels and profile events only):

| session | length | tracker labels | cloned voices prepared |
| --- | --- | --- | --- |
| 11:05 | 184 s | S1 × 62 | S1 |
| 10:12 | 250 s | S1 × 29, S2 × 28, S3 × 1, S4 × 1 | S1, S2, S3, S4 |
| 11:49 | 54 s | S1 × 1, S2 × 2, S3 × 7, S4 × 8 | S2, S3, S4 |
| five short ones | 15–38 s | S1 only | S1 |

Three separate failures are visible in these traces:

- **The cap.** The tracker kept at most 4 speakers and never gave a slot back. In 11:49 all four were created
  within 30 s, and nobody after that could be new. In 10:12, two of the four slots went to speakers heard once.
- **Merging.** In 11:05 the tracker heard 33 VAD-bounded stretches and still put all of them in S1. The cap was
  never reached, so this one is the features, and it is not fixed here.
- **VAD on video audio.** Four sessions have 1 or 2 `vad.start` events in total: music under the speech keeps the
  energy VAD open, so the tracker's segments become fixed 3 s windows that straddle turns. Not changed here: the
  same VAD shapes the units and the latency work.

Once a session's labels collapse into S1, every line uses S1's voice. Since ADR-0009, that voice is also pinned to
S1's first reference.

## How it was measured

Speaker turns, without listening:
- The cast is the actor grouping in `.local/live-dub/actors/condor-ep0{8,9}-offline/enrollment.json`: line
  start/end times on two 45-minute episodes of a Mandarin-dubbed drama.
- Those groups come from a CAM++ encoder clustering, not from people, so they are pseudo-labels.
- A tracker segment belongs to an actor when one of that actor's lines covers more than half of it.
- The metric reproduces an earlier result exactly: the DSP tracker's 0.351 own-voice on episode 9.
- The only human labels (scene-37 `plan.json`, 4 lines) are too few to separate variants.

Two numbers, because a fix can trade one for the other:
- **turn separated**: at a change of actor (within 30 s), the voice changes too. This is the user's complaint.
- **same actor kept**: two lines of one actor within 30 s get the same voice. This is ADR-0009's complaint, one
  actor in two voices.

`a2a-stage-probe speakers` runs the Swift tracker on the audio and writes labels only. The new research flags are
`--max-speakers N` and `--stale-segments N`.

## Results

Six 180 s slices (minutes 5, 15 and 25 of each episode, 54 actor turns): about the length of a real session,
starting mid-video.

| tracker | turn separated | same actor kept | voices per slice |
| --- | --- | --- | --- |
| before: 4 slots, never freed | 0.48 | 0.82 | 2–3 |
| **after: 4 slots, a passing voice frees its slot** | **0.57** | **0.81** | 2–6 |
| 8 slots, never freed | 0.62 | 0.75 | 2–6 |
| 16 slots, never freed | 0.62 | 0.70 | 2–7 |
| 16 slots, new-speaker distance 0.45 | 0.72 | 0.60 | 4–8 |

Whole episodes (45 min each, 103–115 actor turns):

| tracker | ep08 turn separated / kept | ep09 turn separated / kept |
| --- | --- | --- |
| before | 0.08 / 0.97 | 0.43 / 0.69 |
| after | 0.36 / 0.91 | 0.44 / 0.76 |

On ep09 separation is flat; the gain is ep08's.

End to end, one file-mode replay of the minute-15 slice of ep08 in the app configuration, before and after. The
setup:
- `--asr audio --mt hy-local`;
- dual Qwen clone (xvector);
- `--fluent --speakers auto --adaptive-prebuffer`;
- 8 s / 20 s budgets;
- real-time pacing.

| | before | after |
| --- | --- | --- |
| speakers labeled / cloned voices prepared | 4 / 4 | 8 / 8 |
| lines played, and in the first speaker's voice | 58, 54 | 56, 36 |
| voice changed at an actor turn (played lines) | 2 of 5 | 4 of 5 |
| an actor's lines within 60 s in one voice | 0.99 | 0.83 |
| lines skipped waiting for a new speaker's voice | 0 | 2 |
| first chunk median / p90 / max (ms) | 156 / 319 / 1620 | 160 / 422 / 2963 |
| underruns | 0 | 0 |

The segment list and the translation shapes are identical across both runs, so only the voices differ.

The consistency drop (0.99 → 0.83) is one actor changing voice once:
- Before, all 17 of A01's played lines were in S1.
- S1 was created on A04's opening line, and its pinned reference was ready 3.1 s into the slice. Before, A01 was
  therefore most likely voiced from A04's speech the whole time. Reference spans are not traced, so this is
  inferred from timing.
- After, A01 is in S1 until 99 s and in S6 from 102 s on, with no switching back. On this slice, S6 was learned
  from speech that is mostly A01's.

That is a correction, but a listener hears it as a character's voice changing mid-scene. The 0.99 before was
consistency in someone else's voice.

The slow first chunks fall in the clip's last 15 s in both runs, after the last profile preparation at 161 s. The
larger maximum after the change is therefore not explained by preparation; with one run each, it is not
explained at all.

Three of the new voices were prepared for speakers heard one or two times. Each costs a preparation on the worker
and can push back the next line's first chunk.

Recycling is the only change that raises separation without giving up much consistency: −0.01 on the slices.
More slots buy more separation, but at a cost in consistency that ADR-0009 was written against. The 0.45 threshold
won on the slices, but its sweep was not monotonic and flipped between the episodes, so it is not adopted.

## The change

`VoiceprintSpeakerTracker` still holds at most 4 speakers. When all four are taken and a new voice qualifies, a
speaker heard in fewer than 4 segments and not at all for 30 s gives up its slot (`staleSegments`,
`staleSamples`). If several qualify, the least heard goes first.

Speaker numbers come from a counter and are never reused. A freed `S3` may still name a pinned profile, and the
next new voice is `S5`, not `S3` again. Over a whole episode, the run created 8 (ep08) and 10 (ep09) IDs.

With `staleSegments = 0`, the labels on all six slices are identical to the old tracker's.

The app's voice-learning line now lists up to 8 speakers instead of 4, because a session can now reach `S5` and
beyond.

Checks:
- The calibration set from [speakers.md](speakers.md) is unchanged:
  - the audiobook narrator stays one speaker;
  - both female/male dialogues give the same labels;
  - the male/male and female/female dialogues still merge;
  - the drama excerpt and scene-37 give the same labels.
- New test `aPassingVoiceGivesItsSlotToANewSpeaker`: a passing voice frees its slot, the newcomer gets a fresh ID,
  and the regulars keep theirs. With recycling off, the newcomer is read as someone already known.
- `scripts/check.sh` passes: 253 Swift tests, 97 harness tests, manifest 17 entries with 0 problems.
- The app bundle was rebuilt (`scripts/build_app.sh`). No live tap session or listening check was run.

## Measured and not adopted

- **Isolated vocals for the tracker.** The vocals came from the separator the background bus already runs
  (AUSoundIsolation), with a VAD of their own. At 4 slots, ep08 went 0.23 → 0.26 own-voice; with 16 slots the
  gain flipped between episodes. Cost: about 3% of a core. The delay of the separator's voice output was not
  checked independently, so these numbers are provisional. Not wired.
- **Update a speaker only on close matches** (distance ≤ 0.3 or 0.2, against 0.5): consistency fell
  (0.82 → 0.67), and separation did not rise. Removed.
- **Thresholds at 4 slots.** Nine combinations of new-speaker, immediate and confirmed distance gave identical
  episode results: the cap was binding.
- **Qwen3-TTS's speaker encoder instead of DSP.** This is the encoder the clone worker already loads. Offline, on
  the same segments:
  - about 4 ms per segment;
  - same/different-actor AUC 0.82–0.85 against the CAM++ groups;
  - raw cosines bunched at 0.90–0.98;
  - online clustering, centered on the other episode's mean, reached f 0.47–0.68, against 0.51–0.60 for tuned DSP.
  That is not enough to justify moving tracking into the worker protocol.
  The bank results under `.local/live-dub/runs/tracker-ep0*` look better (CAM++ at about 0.75 own-voice), but they
  are scored against CAM++'s own groups, which is circular. CAM++ is also declared but not verified in the
  manifest, and its checkpoint license is unverified.

## Still open

- Merging inside the slots: session 11:05 shows a single speaker for 184 s. A real per-character voice needs a
  speaker embedding qualified on held-out, human-labeled dialogue. That is a separate, larger change.
- The VAD that stays open under music.
- A listening check of a live session.

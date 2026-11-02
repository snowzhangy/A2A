# Dub quality, live — research notes (27 October 2026)

Target: `dub-anything/videos/condor (dubbed).mp4`, the 37 s inn scene from *The Return of the Condor
Heroes* (1995) episode 8, 583–620 s, dubbed offline by an agent. Plus a per-series actor database that
recognizes the cast on a new episode. Hardware: M3 Pro, 36 GB. All numbers are single runs on two
episodes of one show; they are research measurements, not product gates or listening verdicts.

## 1. Why the current live path cannot sound like the goal

dub-anything's own listening tests ([docs/research.md](../../../../Research/dub-anything/docs/research.md),
findings 2, 3, 7, 8) name what listeners heard:

1. **Timing ("fill").** Each English take starts and ends where the actor does. This was the
   biggest preference.
2. **Delivery.** Each take is cloned from the actor's own reading of that very line, so a shout stays a
   shout.
3. **Clean references.** References come from RoFormer-separated vocals.
4. **The original background** (music and effects) plays under the dub.

Each of the first three needs the whole line before its English is rendered. A zero-delay pipeline
speaks after the actor has finished. The English then trails the picture by 5–13 s (current A2A:
4.7 s median onset lag and 13 s maximum on this clip). It cannot fill the actor's time, and it has no
line-level delivery to copy. Faster models do not fix this, because the lag comes from waiting for the
words.

**So the main variable is presentation delay D.** The picture and the background are shown D seconds
late, and every English line lands in its own original slot. That holds as long as the line is heard,
recognized, translated and rendered within D of its start. The experiments measure quality and
coverage against D under real compute.

## 2. Product implication of D (not built yet)

A2A must own the presentation. Today it taps another app's audio while that app shows the picture.

- **A2A plays the file or URL itself.** Read-ahead is unbounded, so this is effectively offline
  quality after a short start-up buffer.
- **A delayed mirror of any app.** ScreenCaptureKit captures the source window, and VideoToolbox
  hardware-encodes it into a ring of compressed sample buffers (about 20 MB per 15 s at 720p). An
  `AVSampleBufferDisplayLayer` shows them on a control timebase set D behind the capture clock. The
  existing muted process tap stays as it is. DRM-protected playback (FairPlay in Safari, for example)
  captures as black, so this route fails there.
- ADR-0004 ("only live translated") is respected: the bed contains no voice. Shipping a background bed
  still needs an ADR-0004 amendment and the user's agreement.

## 3. Stage measurements

Runs are in `.local/live-dub/runs/`. RTF is compute seconds per second of programme audio. Several
runs shared the GPU with another job and are marked *contended*, so their compute times are upper
bounds.

### Separation (voice and background)

| Separator | RTF | Latency | Notes |
| --- | --- | --- | --- |
| Kim Mel-Band RoFormer (MLX), 8 s windows, 50% overlap | 0.42 | 8 s | Reference setting |
| Same, 1/8 overlap | 0.23 (scene), 0.25–0.27 (full episodes, contended) | 8 s | Vocals within 25.6 dB of the 50% run |
| Same, 4 s windows | about the same | 4 s | Vocals within 28.3 dB of 8 s windows |
| AUSoundIsolation (macOS, high-quality voice) | 0.027 | 0.13 s | Voice within 17.3 dB of RoFormer; its background has half RoFormer's energy (5.6 dB agreement), unexplained |

fp16 and bf16 RoFormer are no faster than fp32 in MLX (1.57–1.61 s per 8 s window). RoFormer is what
dub-anything found cleanest for references; AUSoundIsolation is 10× cheaper with almost no latency.
Which background sounds better needs listening.

### Recognition (Mandarin)

CER is measured against the published subtitles, which drop particles (啊, 呢, 呐). `loose` removes
particles from both sides. Most of the remaining errors on the scene are subtitle paraphrase, not
misrecognition.

| Recognizer | Goal scene 583–703 s, loose CER | First 10 min, loose CER | RTF |
| --- | --- | --- | --- |
| Qwen3-ASR 1.7B, 8-bit | **10.5%** | **17.5%** | 0.09–0.20 (contended) |
| Qwen3-ASR 0.6B, 8-bit | 11.6% | — | 0.12 (contended) |
| SenseVoiceSmall | 12.6% | — | 0.025 |
| Apple SpeechTranscriber (current A2A) | 12.6% | 19.8% | Off GPU |

The first 10 minutes include the theme song, which raises every recognizer's CER. Qwen3-ASR also
takes the series glossary as hotwords and emits punctuation. The forced aligner turns that
punctuation into sentence times, and those split speaker turns that have no pause between them.

### Translation (local LLM, dubbing prompt)

The prompt gives the previous 6 lines with their chosen English, the series glossary, the speaker,
and a syllable budget of 4 syllables per second of the actor's slot. The test set is the goal
scene's 12 lines. Latencies were contended.

| Model (4-bit MLX) | Median latency | Syllables / budget | Errors on 12 lines |
| --- | --- | --- | --- |
| Qwen3-30B-A3B-Instruct-2507 | 2.5 s | 1.08 | None found; close to the directed script |
| Qwen3.5-9B | 3.2 s | 1.20 | 2 pronoun errors ("You two priests", "cut off your ears") |
| Qwen3-4B-Instruct-2507 | 1.9 s | 1.11 | Additions ("kinda rude, huh?"), "one of their ears" |

Showing the model the next line as context made it merge lines, so lookahead is off. The 30B MoE
needs 17 GB. It fits beside ASR and OmniVoice only once MLX's buffer cache is capped, which is a
memory budget a product sharing the Mac with a browser cannot assume.

### Voice (cloned English, goal scene, 12 lines)

Each reference was the actor's other clips in the scene followed by this line's own separated vocals
(dub-anything's per-line rule). OmniVoice was given the actor's slot length as `duration_s`.

| | Ends within 0.25 s of the actor | Median end error | Back-transcription WER | Compute per slot second |
| --- | --- | --- | --- | --- |
| OmniVoice, 32 steps | **12 / 12** | 0.02 s | 7.7% (all "Quanzhen") | 4.6 (contended) |

Profile per 2.9 s line (contended): 8.7 s at 32 steps, 4.3 s at 16, 2.3 s at 8. Reference encoding
takes 0.27–0.63 s, and batching four lines saves about 30%. The goal also put 12/12 lines within
0.25 s, but it got there by re-rendering. OmniVoice reaches it in one take because it is told the
duration.

## 4. Dub quality against delay D (the main result)

`pipeline.py` makes the causal decisions and times every stage. `simulate.py` replays those measured
costs on a single GPU queue for each D, and a line whose English is not ready at its slot is late or
dropped. The configuration: Qwen3-ASR 1.7B, the forced aligner, CAM++ with an actor database from the
*other* episode, Qwen3-30B-A3B, and OmniVoice at 16 steps with per-line references and slot duration.
Measured compute was 0.33 s of ASR plus alignment per segment, 1.2–1.3 s of MT per line and 1.0–1.2 s
of TTS per line. The simulator's drop decision uses each line's actual cost, so it is optimistic.

Separation options:
- **isolation**: AUSoundIsolation for everything, off the GPU, 0.15 s latency.
- **hybrid**: AUSoundIsolation for the bed, VAD and speaker, plus RoFormer on each line for a clean
  reference. The per-line RoFormer pass adds 0.2 × the line length of GPU time.
- **roformer**: streaming RoFormer for everything, 4 s latency and 0.25 RTF on the GPU.

Coverage is the share of the actor's speaking time that was dubbed:

| D (s) | ep08 480–780, isolation | ep08, hybrid | ep08, roformer | ep09 580–880, isolation | ep09, hybrid |
| --- | --- | --- | --- | --- | --- |
| 0 | 0% | 0% | 0% | — | — |
| 3 | 11% | 7% | 0% | 12% | 8% |
| 6 | 51% | 41% | 2% | 46% | 36% |
| 10 | 77% | 70% | 31% | 80% | 63% |
| 15 | **100%** | 86% | 57% | **87%** | 77% |
| 20 | — | 92% | 62% | 93% | 80% |

All figures here are **simulated** schedules over measured compute, not a real-time run.

**On time.** At D=15 (isolation), 65 of 66 lines and 105 of 112 lines start in their own slot.

**End timing is mostly by construction.** OmniVoice is told the slot length and the take is then
fitted to it, so ending within 0.25 s of the actor (98% of played takes) mostly confirms that the
model obeys the requested duration. The real risk is pace.

**Pace.** The median take runs at 4.7–5.1 English syllables per second, about natural speech. But 15
of 66 takes (ep08) and 28 of 112 (ep09) exceed 6.5 syllables/s, or pause for more than 35% of the
take. These are rushed lines in short slots, such as "This young lady." squeezed into 0.5 s. The
translator overshoots its syllable budget by 8–20%. Tightening that budget, or letting a take run into
the gap after the line, is the fix, and the flagged takes are the first to listen to.

**Speech never found.** Line coverage counts only lines the pipeline found. Against the published
subtitles, played takes cover 69% (ep08, hybrid, D=15), 70% (ep09, 16 steps) and 78% (ep09, 8 steps)
of subtitle time. Subtitles stay on screen longer than the speech, so these are lower bounds, but VAD
misses and empty recognitions are inside the gap.

**Density.** Coverage on ep09 is lower because dialogue is denser there (112 lines in 300 s). Compute
is about 2.5 s per line, so the GPU runs near capacity and the catch-up rule skips lines.

**D=0.** The dub-quality path cannot play anything at D=0, by construction: a line's English cannot
exist before its own end, and the simulator allows 1 s of lateness. The requirement is
D > line length + processing (about 2.5 s plus the queue). Today's A2A does play at no picture delay,
but 4.7 s median after the actor starts. That is interpreting, not dubbing.

**Goal scene** (ep08 583–620, D=15, hybrid): 10 of 11 lines play. The six-second merged line is
dropped by catch-up. Apple's en-US recognizer hears the rendered English almost word for word.

Remaining flaws:
- "Yes, that's **him**": ASR wrote 就是他 for 就是她, and MT followed it. 他 and 她 are homophones, so
  the prompt should tell the model to resolve them from context.
- "Taoist" was heard as "tooth always" and needs a respelling.
- The respelled "Quanzhen" ("Chwen-jen") cannot be judged by an English recognizer.

Renders (`.local/live-dub/runs/`):
- `compare/goal-vs-before-vs-lab.mp4`: goal, then current A2A live, then the lab at D=15; 37 s each.
- `dub-ep08-480-780/ep08-480-780-D15-hybrid.mp4`: 5 minutes of ep08.
- `dub-ep09-580-880/ep09-580-880-D15-isolation.mp4`: 5 minutes of the new episode, 16 steps.
- `dub-ep09-580-880-s8/ep09-580-880-D10-isolation-s8.mp4`: the same 5 minutes at D=10, 8 steps.

The renders use RoFormer references and the RoFormer background in every configuration. In
isolation mode a live engine would use AUSoundIsolation's voice and background instead, which have
not been listened to.

### Buying headroom: OmniVoice at 8 steps (ep09 580–880, same configuration otherwise)

| | TTS per line (median) | D=10 isolation | D=15 isolation | D=15 hybrid | D=20 isolation | Back-transcription WER | Takes with WER > 0.3 | Likeness (median) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 16 steps | 1.03 s | 80% | 87% | 77% | 93% | 15.5% | 16 / 112 | 0.599 |
| 8 steps | 0.56 s | **92%** | **97%** | 95% | 100% | 17.7% | 23 / 112 | 0.585 |

At 8 steps, 111 of 112 lines play on time at D=15. The translation cost also differed between the two
passes (median 1.00 s against 1.19 s, because different speaker names produced different prompts).
With each line's MT cost taken from the 16-step pass, 8 steps still give 91% (D=10) and 97% (D=15),
so the gain comes from TTS. The cost is slightly less intelligible English: back-transcription WER
rises from 15.5% to 17.7%.

Likeness is the CAM++ cosine between a take and the actor's own line. A take scores a median of 0.60
against its own line and 0.18 against a random other line, and beats the mismatched pair in 95% of
takes. Two Mandarin lines from different random speakers score 0.25. So the cloned English carries
each actor's voice by this measure. That is not a listening verdict on accent or naturalness.

## 5. Actor database (recognize the cast on a new episode)

Design, after two failed variants:
1. Online clustering from scratch at threshold 0.5 merged several women into one "actor" and several
   older men into another.
2. A strict online policy (0.6) fragmented the cast into 35 "actors" and left 68% of lines
   unassigned.

The working design separates **offline enrollment** after an episode from **live recognition**:

- **Enrollment** (`enroll.py`) clusters every line of the watched episode globally (average linkage,
  CAM++, 0.5). It keeps clusters with at least 20 s of speech and stores up to 60 exemplar embeddings
  and the 4 most typical 3–8 s lines (RoFormer vocals plus transcript) as references.
  - ep08: 12 actors covering 66% of line speech. ep09: 10 actors, 73%.
  - The four largest ep08 clusters are visually pure: Yang Guo twice (playful and serious registers,
    useful as delivery references), the Mongol minister, and Lu Wushuang.
- **Recognition** (`ActorDB.identify`) takes the mean of the top-3 exemplar similarities and accepts at
  ≥ τ with a 0.05 margin over the best *other person*. Same-name clusters are one actor and do not veto
  each other. Anything else is voiced from its own line's audio, so an unrecognized voice is never
  given someone else's voice.

New-episode test (database from ep08, frozen; ep09 is a different episode):

- Visual audit of matches at ≥ 0.6: Yang Guo 8/8 correct, Lu Wushuang 8/8 correct.
- One ep08 cluster pulls in several women at 0.52–0.60. Below 0.6, precision falls.
- The ep08 "Mongol minister" voice matches the old master in ep09 at 0.54–0.68. That may be the same
  Mandarin dub voice actor; it needs listening.
- First scene of ep09, 11 lines labelled from the picture:

  | τ | Right | Wrong | Unassigned |
  | --- | --- | --- | --- |
  | 0.5 | 6 | 0 | 5 |
  | 0.55 | 4 | 0 | 7 |
  | 0.6 | 2 | 0 | 9 |

  The top-ranked actor was correct for all 11. Single-line CAM++ scores on this material (0.50–0.63
  for correct matches) are what limit recall.
- Cross-episode cluster centroids agree strongly: Yang Guo 0.90, Lu Wushuang 0.93, the old-man voice
  0.84.

On its own, a single line is recognized with high precision but only about 50% recall.

**Session tracks** (`actordb.SessionTracks`, `replay_db.py --tracks`) fix most of that:
- Lines within a scene join the track whose running mean they match at ≥ 0.6.
- Tracks reset after an 8 s pause.
- The pooled mean is matched against the database.

Results with ep09 recognized against the frozen ep08 database at τ=0.55 (picture labels: the lab
agent's reading of the frames):

| | Right | Wrong | Unassigned | Yang Guo, his first 7 lines | Lu Wushuang, her first 4 | Masked woman, her first 5 |
| --- | --- | --- | --- | --- | --- | --- |
| Single line | 8 | 0 | 8 | 3 of 7 | 3 of 4 | 2 of 5 (lines 3–4) |
| Session track | **13** | **0** | 3 | **7 of 7, from line 1** | **4 of 4, from line 1** | 2 of 5 (lines 3–4) |

Over the whole episode, recognized lines rise from 224 to 327 of 712. So **returning main actors are
recognized from their first line of the new episode**, with no wrong labels on these 16 lines.

The masked woman maps to an ep08 cluster that also collects other women: here, the 684 s line, a
different character. That cluster is impure. A guard is needed: use stored references only from
clusters the user has named or confirmed, and voice the rest from their own line. That keeps a merged
cluster from lending one woman's voice to another.
Names are proposals from frames (`name_source`: lab agent) for the user to confirm. The database
stores biometric data in `.local/live-dub/actors/` (0700/0600), like ADR-0006's store.

## 6. Recommendation

1. **(Superseded by the user's decision on 27 October: no picture delay. See section 8.)**
   Delayed presentation is the only lever that gets lip-level dub timing. Simulated coverage
   with each configuration named:
   - ep09, isolation, 8 steps: 92% at D=10 and 97% at D=15.
   - ep09, hybrid (cleaner RoFormer references), 8 steps: 84% at D=10 and 95% at D=15.
   - ep08, isolation, 16 steps: 77% at D=10 and 100% at D=15.

   Lines start in their own slot. Isolation's references and background have not been listened to,
   so hybrid is the safer default if reference cleanliness matters. The picture must be delayed too: either A2A plays the file or URL itself
   (simplest, with unlimited lookahead), or it shows a ScreenCaptureKit mirror delayed by D (any
   app; fails on DRM). That is a product decision for the user. It changes the app from overlay
   audio to an A2A player window.
2. **Engine:**
   - AUSoundIsolation for the bed, VAD and speaker embedding.
   - Qwen3-ASR 1.7B with series hotwords, plus the forced aligner for sentence and turn splits.
   - An LLM with context, glossary, speaker names and a syllable budget.
   - OmniVoice with per-line references and the slot duration at 8–16 steps.
   - The actor database for references and names.
   - RoFormer per line, when GPU headroom allows, for cleaner references (dub-anything's finding).
3. **Translation memory budget.** The 30B MoE gave the cleanest lines but needs 17 GB, and fits only
   with MLX's buffer cache capped. Before choosing, test prompt caching of the fixed prefix, since
   prefill is most of the cost, and Qwen3.5-9B with the homophone and speaker hints.
4. **Actor database:**
   - Offline enrollment after each episode.
   - Live recognition at τ≈0.55 with the name-aware margin and session tracks.
   - Stored references come only from clusters the user has named or confirmed.
   - The user names the clusters (proposals are in `db.json`).
   - Not yet in `pipeline.py`: the dubbing passes above used single-line recognition. Session tracks
     were measured by replay.
5. **ADR work.** ADR-0004 needs an amendment for a voice-free bed. The actor store follows ADR-0006's
   rules but spans episodes per series, so it needs its own ADR. It contains public actors' voices
   from TV episodes, which is personal research use, not a consented corpus.

## 7. Limits

- **Episodes and stretches.** Two episodes of one show, from one Mandarin dub studio. The dubbing
  passes are 5-minute stretches, not whole episodes.
- **No listening.** Every quality number is a proxy: back-transcription, CAM++ likeness, subtitle
  CER. Listening checks are still needed for:
  - naturalness and accent;
  - the AUSoundIsolation bed against the RoFormer bed;
  - 8 against 16 OmniVoice steps;
  - respelled names.
- **Optimistic simulator.** It assumes one serial GPU queue with each line's actual cost known
  before it runs. It ignores CPU contention, the A2A app itself, the browser playing the video, and
  ANE use by Apple's components. A real-time run of the engine is the next validation.
- **References in the renders.** Every render uses RoFormer references and background, even for
  configurations that would use AUSoundIsolation live.
- **Speaker labels.** These come from video frames: the person on screen is usually, but not always,
  the speaker. The "old man" voice match across characters is unresolved.
- **Changes after the measured runs.** The homophone hint in the translation prompt and the "Taoist"
  respelling were added after the measured runs and are not yet measured.

## 8. No picture delay: the cloned voice catches up (the user's direction, 27 October)

The user chose not to delay the picture. The English is spoken just behind the actor, and the engine
keeps that lag from growing.

### Changes

| Change | Detail | Effect |
| --- | --- | --- |
| Clause units | The VAD closes a unit on a 0.15 s pause and forces a cut at 5 s, so English can start after a clause instead of the whole sentence. | |
| Concise English | The syllable budget is a ceiling of 3.6 syllables per second of the actor's time, with a system rule to condense like a subtitle. | English is 0.88–1.0× the budget, down from 1.08–1.5×. |
| Natural length | Each take is rendered at OmniVoice's natural length for its text, capped at the actor's slot, instead of being stretched to fill the slot. | |
| KV-cached translation | The session is a running chat, so each line prefills only its own turn. | Median MT time falls from 1.11 s to 0.50 s on the goal scene, with the same output quality. |
| Cached actor clips | Each stored reference is encoded to codec tokens once; only the new line is encoded each time. | |
| Catch-up playout | A take that waited, or the take still playing when the next is ready, plays the rest of its content at up to 1.3×. A line whose English would start more than `max_lag` after the actor is skipped. | |
| Homophone hint | 他/她/它 are resolved from context. | The goal scene's "that's him" is now "that's her". |
| Memory guard | A pass waits for 26 GB of free memory before loading models (another agent shares the Mac). | |

### Results

Zero picture delay, simulated deadlines on one GPU queue, isolation separation, `max_lag` 7 s:

| Pass | Coverage | English starts after the actor (median / p90) | English ends after the actor (median / p90) | Pace (syllables/s, median / p90) | Skipped |
| --- | --- | --- | --- | --- | --- |
| ep09, sentence units filling the slot (8 steps, before mid-line catch-up) | 60% | 4.6 / 6.6 s | 4.6 / 6.6 s | 5.2 / 8.2 | 25 |
| ep09, all changes (8 steps) | **72%** | **4.1 / 6.0 s** | 3.9 / 5.7 s | 5.5 / 8.1 | 17 |
| ep08, all changes (8 steps) | **82%** | **4.0 / 5.7 s** | 3.8 / 5.3 s | 4.8 / 6.7 | 5 |

`max_lag` trades coverage for lag (ep09, all changes):

| `max_lag` | Coverage | English starts after the actor (median / p90) |
| --- | --- | --- |
| 7 s | 72% | 4.1 / 6.0 s |
| 6 s | 64% | 3.6 / 5.4 s |
| 5 s | 48% | 3.0 / 4.2 s |

For reference, today's A2A in file mode on the 120 s excerpt played 0.68–0.71 of source seconds. Its
median onset lag was 4.8–5.4 s, with a p90 of 12.4–12.8 s and a maximum of 13.2–13.5 s
(`dub-anything-review.md`). The excerpt differs from these stretches, so treat the comparison as a
direction only.

### Where the lag comes from

Mean onset lag on ep09 is 4.2 s:

| Part | Mean | Notes |
| --- | --- | --- |
| The actor's line itself | 1.35 s | English cannot start before the line is heard |
| VAD and separation | 0.30 s | |
| Compute | 1.60 s | ASR 0.32, MT 0.52, TTS 0.56, plus alignment |
| Waiting for the GPU queue or the speaker | 0.86 s | 0.29 s median |

Speeding up mid-line barely changes the lag, because the GPU queue in dense dialogue dominates the
wait. The floor of this architecture is therefore about line length plus 1.5 s. Getting below 3 s
needs:
- less GPU work per line: Apple's streaming recognizer off the GPU, a smaller translator, or fewer
  OmniVoice steps;
- starting before the line ends: streaming partials and clause-level simultaneous translation, which
  costs some quality.

### Quality cost of catching up

Condensed English loses some meaning. In the goal scene, 相貌清秀 once became "fair-haired" at a
3.3 syllables/s ceiling, which is why the passes use 3.6. In the zero-delay goal scene, 9 of 11 lines
play, at 2–6 s lag. There is no lip sync without delay. What this mode keeps from the dub is:
- the actor's own voice and delivery, through per-line references and the actor database;
- context-aware translation with consistent names;
- a voice-free background.

### Against the goal dub (goal scene, measured on both final mixes)

`compare_goal.py` cuts each English line from the mixed track where it plays and scores both dubs the
same way: Qwen3-ASR 0.6B back-transcription and CAM++ likeness to the actor's own line. Likeness is
lower here than in section 4 because the cuts include the background. The lab's last line runs past
the 37 s render and is left out.

| | Lines played | Start after the actor | Back-transcription WER | Likeness (median) | Syllables/s |
| --- | --- | --- | --- | --- | --- |
| Goal (offline, directed script, re-rendered takes) | 12 / 12 | 0 s (fills the slot) | 10% | 0.54 | 3.7 |
| Lab, zero delay | 9 / 11 | 2.1–5.9 s | 22% | 0.40 | 3.4 |

The gaps:
- **Dropped lines.** The opening question and the six-second merged punchline ("…she sliced off
  their ears") are skipped. Both are long units, and merging same-actor parts makes them longer.
- **Names.** "Quanzhen" is mispronounced (heard as "transition", "trained") and means nothing to an
  English viewer. The goal says "Taoist" instead. The glossary maps 全真教 to "Quanzhen Sect".
- **Voice.** Seven of the lab's nine lines had no recognized actor, so they were cloned only from that
  line's vocals: references of 0.55–3.1 s, against 7.3–8.3 s for the two recognized lines. The goal
  uses the same zero-shot OmniVoice, at 32 steps (`rendered.json`) with picked takes, and a reference
  of the character's clips plus the line's original (dub-anything finding 6); its finding 1 is that
  likeness was not its gap. The lab uses 8 steps and one take. Recognition alone does not explain the
  numbers yet: the two recognized lines score 0.40 and 0.56, the unrecognized ones 0.21–0.64
  (median 0.38), and short lines score low either way. Checked against the goal script's speakers
  (asker, waiter, companion):
  - Database scores sit just under the bar (0.51–0.61). The 1.0 s "Yes, that's her" scored 0.606 but
    failed the margin rule (0.04 over the runner-up, 0.08 required).
  - The waiter's five lines resemble each other (0.55–0.79) more than the database actor they match
    (A05, 0.51–0.63, enrolled from ep09 only). Pooling within the scene is the stronger signal.
  - Replaying `SessionTracks` on the stored embeddings pools four of the five waiter lines and
    recognizes the track from its third line. The asker's lines never join (0.40–0.58 against
    `join` 0.6), and the asker sits as close to the companion (0.44–0.48), so lowering `join` alone
    would merge them.
- **Timing.** This cannot match without picture delay.

### Renders

- `.local/live-dub/runs/compare/goal-vs-before-vs-lab-nodelay.mp4`: the goal, today's A2A, then the
  lab at zero delay.
- `.local/live-dub/runs/catchup2-ep09-580-880/ep09-580-880-nodelay-catchup.mp4`: 5 minutes of the new
  episode at zero delay.

Every render uses RoFormer references and background as a stand-in for the AUSoundIsolation path.

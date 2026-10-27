# External review against dub-anything — 27 October 2026

The user forwarded a review comparing A2A with
[dub-anything](https://github.com/swax/dub-anything) on the same film excerpt
(`youtube-NRB0ICxMkrM-0940`) and asked for A2A to be improved toward its result. This
report records which review claims still hold, what was measured, and what changed.
Exploratory runs, not gate evidence. Raw runs are under `.local/runs/` (named below).

dub-anything is offline and agent-directed: it transcribes the whole clip first, can
re-render any line, and spends hours of GPU per film. A live pipeline cannot match that
result; the review says so too. The target is to stop losing on what latency does not force.

## Review claims, checked

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| 1 | Most dialogue is never spoken (coverage 0.13–0.46; first audio ~58 s) | **Stale** | Those numbers predate fluent playout (`voice-match.md`) or are the reported failure that `clone-reliability.md` fixed (first audio now 5.3–5.6 s). Live tap run, 25 October: 22/27 units played, coverage 0.73, onset lag median 4.7 s. Still true: 24 of 29 commits wait for recognizer finalization; 10–14 s units drive a ~13 s maximum lag. |
| 2 | Original audio is muted, so no music or effects | **True** | Ducking the original would play the Mandarin voice about 5 s ahead of the English, against ADR-0004. dub-anything keeps a voice-free background; see the spike below. |
| 3 | ASR is the ceiling; enable the pinned models | **Ceiling true; fix wrong** | No listed model is pinned: every row is `status: declared` with null revision, hashes, and license evidence, and most have no adapter. `hy-mt1.5` is license-restricted and never a product dependency. |
| 4 | English runs 1.5–1.9× the Mandarin; 0.25 s/char is optimistic | **Partly stale** | Fluent playout already paces each unit to its source span (WSOLA, cap 1.3×) at temperature 0.6. After pacing, the median is 0.25–0.28 s of English per source character, but the p90 of 0.39–0.40 means the 0.25 estimate undercounts long units. The pacing trace added here shows why: the cap binds (below). |
| 5 | One voice for every speaker | **Stale** | `--speakers auto` gives each tracked speaker their own reference (S1–S3 on this clip). Still true: the spectral tracker can merge same-gender voices; references carry background music; delivery is not conditioned per line. |
| 6 | Translation is context-free; proper nouns mangled | **True** | Apple Translation accepts neither context nor a glossary. Glossary support added (below). |
| 7 | No per-line QA | **True** | A live session cannot re-render a line that has already played. |
| — | Speaker-similarity metric is circular | Already recorded | `voice-match.md` |

## Measurements

**Voice-free background (spike, no product change).** macOS `AUSoundIsolation`
(AudioToolbox, macOS 13+, no download), high-quality voice mode, offline over the 120 s
excerpt. The background is the dry path through the same unit minus the isolated voice,
so both share the unit's 92.5 ms latency. Processing cost: 1.2% of real time.

| Track | Apple zh-CN recognition (characters) | Diagnostic CER vs published subtitles |
| --- | --- | --- |
| Original | 375 | 11.05% |
| Isolated voice | 377 | 13.26% |
| Background (original − voice) | **0** (no speech found) | — |

The recognizer finds no speech in the background track. That is not proof of
inaudibility; it needs listening. Isolation does not improve recognition (one run,
362 reference characters), so ASR stays on the original signal. CER uses Apple's
`Hant-Hans` transform rather than OpenCC (not installed); the original scores 11.05% here
against 11.33% in `stage-diagnosis.md`. Run: `.local/runs/bed-spike/`, including
`demo-english-with-bed.wav` (the live run's English over the background).

**Reference leakage.** The review's fix — re-transcribe each reference and refuse a
mismatch — was tested before building it. Twelve whole-file recognizer segments, cut on
their token spans ±0.1 s as `ReferencePolicy` cuts them, re-transcribed: median CER 0.059
against the segment's own text, character counts within ±2; the maximum, 0.33, is one
substitution in a 3-character segment. No leakage at this level; the gate was not built
(it would delay the first voice). Live streaming token timing was not measured this way.
Run: `.local/runs/refcheck/`.

**Glossary and pacing.** Same padded excerpt and flags as the 25 October file-matched run
(`--fluent --speakers auto --translation-only`, Qwen3-TTS), plus a five-term glossary and a
two-word pronunciation map. Single runs; differences under ~5 points are noise.

| Run | Played | Coverage | Lag median / p90 / max | Units with a glossary term rendered exactly as agreed |
| --- | --- | --- | --- | --- |
| Baseline (`clone-live-20261025-2314/file-matched`) | 20 / 29 | 0.683 | 4.79 / 12.36 / 13.48 s | 1 / 6 |
| Glossary (`glossary-test`) | 20 / 29 | 0.708 | 5.37 / 12.76 / 13.18 s | **6 / 6** |

In the baseline, one unit had the agreed term and one differed only in case; the real
mistranslation was 全真教 as "True Zen sect" (twice), and 豺狼谷 came out as "Wolf Valley".
The glossary was written from this clip's own transcripts, so 6/6 shows that Apple
Translation passes Latin terms through intact and keeps them consistent; it says nothing
about coverage on unseen content, where someone must supply the terms in advance.

New `tts.done` pacing trace on the glossary run: **16 of 25** units hit the 1.3× cap;
native English length is a median **1.74×** (p90 2.35×) of the source span, so even at the
cap English lasts about 1.34× its source. That is the remaining coverage loss (8 units
skipped by catch-up). The pronunciation map is exercised by an integration test (fake
worker; `tts.done` carries a text-free `respelled` flag); whether the respellings sound
right needs listening, since back-transcription cannot judge a respelled proper noun.

## Changes

- `tts.done` trace event (numbers only): pace applied, native and emitted milliseconds,
  quiet audio removed, and the target span. `research/run_metrics.py` summarizes it.
- `--glossary terms.json` (Apple MT only): source terms (including known mis-hearings) are
  replaced with the agreed target term before Apple Translation, which passes them through. `TermMap`
  (A2ACore) matches CJK terms anywhere and Latin terms on word boundaries only.
- `--pronounce words.json`: respellings applied to the synthesizer's text only; captions
  and traces keep the real spelling. `tts.done` records whether a unit was respelled.
- Both flags are `a2a-pipeline` options; the SwiftUI app does not pass them yet.

## Open, in order

1. A voice-free background as an opt-in listening mode: isolation off the RT thread into a
   bed ring, replacing the original bus. Needs a listening check and an ADR-0004 amendment.
2. Duration: English needs 1.74× its source time. Options are shorter translations (needs a
   controllable MT; the LLM rows are not verified), a higher cap (naturalness cost), or
   commits that do not wait for finalization (M4).
3. Isolated-voice references (music-free conditioning, as dub-anything does), judged by a
   non-circular verifier and blinded listening, which still need verified model entries.
4. An offline quality mode built on `a2a-stage-probe`, as a separate proposal.

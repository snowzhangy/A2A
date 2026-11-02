# Long utterances no longer hold the first English audio — 31 October 2026

Item 2 of [the dub-anything review](dub-anything-review.md) was "commits that do not wait for
finalization (M4)": 24 of 29 commits in that run closed only because the recognizer finalized,
the longest committed unit covered 14.1 s of source, and `commit.hold` fired 59 times with
`reason: no_boundary`. This record covers the policy change, a duplication bug it exposed, and
what each is measured on.

## Change 1 — commit-policy source-time budget

`CommitPolicy` may now close a stable, un-finalized unit at its **last safe clause boundary**
once that unit covers more than `maxUnitSourceSeconds` of source speech (default **4 s**,
reason `length_clause`). Rules:

- Guards, all of them: at least `lengthClauseMinKeys` (12) stable keys pending, at least
  `minCommaUnitKeys` (4) content keys before the cut, at least `minTailKeys` (3) stable keys
  left behind so the rest of the utterance stays splittable, and `unsafeEnding` must be nil at
  the cut, so a clause never ends on a negation, a bare number, or a dangling function word.
  No safe position means no cut: the unit holds and reports `commit.hold` as before.
- Duration is the pending unit's **own** speech: the newest stable key's token timing when the
  adapter reports one, otherwise the recognizer's audio frontier. A frontier alone would cut a
  short clause whenever VAD stays open over music.
- Ordering keeps the better evidence first: punctuation boundary, then finalization, then the
  streaming pause/conjunction rules, then this budget, then the 60-key forced split.
- `--max-unit-seconds S` sets it (0 waits for finalization as before), recorded in the run
  summary as `max_unit_seconds`. `--quality-first` raises it to that mode's own source-lag
  budget, because quality-first buys whole phrases with latency.

## Change 2 — late punctuation no longer speaks words twice

Measured, not hypothetical: the new fixture's partials carry no sentence terminator and the
final result inserts one inside text that had already been committed. The committed-key prefix
then no longer matched the hypothesis, the frontier index pointed *before* its own end, and the
next unit re-committed the keys after it — in the first long-utterance run, `因为成本太高价格`
was followed by `价格是三点五万元。`. On real audio that is a dubbed line speaking a word twice.

The frontier now re-aligns: committed keys are matched forward over the revised hypothesis while
skipping inserted punctuation. A change in spoken content still reports
`revisionBehindFrontier` once and leaves the frontier where it is, because no alignment is safe
there. This applies to every commit path, not only the new budget.

## Change 3 — a fixture that reproduces the pattern

`a2a_harness.synth --utterance-sentences N` models a recognizer whose utterances span several
sentences: VAD stays open, partials cross sentence boundaries, sentence terminators inside the
group appear only in the final result, and nothing finalizes until the group ends. Synthetic
tone bursts and the clause dictionary only, so it lives in git and runs in hosted CI.

```
uv run --project harness python -m a2a_harness.synth --out .local/fixtures/synthetic-long \
  --sentences 12 --utterance-sentences 3 --id synthetic-zh-long
```

## Measurements

`a2a-pipeline` in file mode, `--asr scripted --mt dictionary --tts tone --fluent`, virtual
clock, offline (so deterministic; one run each, identical flags apart from the budget), summarized
with `research/run_metrics.py`. Tone synthesis and a fixture dictionary measure **policy and
playout**, never model quality, naturalness, or identity.

Long-utterance fixture, plan default budgets (3 s debt / 4 s source lag):

| Budget | Units | Played | Coverage | `lag_onset` median / p90 / max | Skipped by catch-up |
| --- | --- | --- | --- | --- | --- |
| 0 (previous behaviour) | 14 | 9 | 0.730 | 3.04 / 6.14 / 6.14 s | 5 |
| **4 s** | 14 | 12 | **0.907** | 3.11 / **4.83 / 4.88** s | **2** |
| 2.5 s | 16 | 14 | 0.873 | 3.17 / 3.84 / 3.84 s | 2 |

App configuration (`--fluent --adaptive-prebuffer`, 8 s / 8 s, everything played in all runs):

| Budget | Coverage | `lag_onset` median / p90 / max | English s per source char (median) |
| --- | --- | --- | --- |
| 0 | 1.000 | 5.21 / 6.35 / 6.59 s | 0.22 |
| **4 s** | 1.000 | **3.51 / 4.83 / 4.88 s** | 0.18 |

The unchanged short-sentence fixture (`synthetic-zh-3min`, where the recognizer punctuates
normally) is byte-identical in behaviour to before the change: 71 units, 71 played, coverage
1.000, `lag_onset` median 2.76 s, commit reasons `sentence_end` 51 / `clause_comma` 20 and no
`length_clause`. The budget is inert when there is real boundary evidence.

What the numbers say: the budget moves the **tail**, which is where catch-up skips come from
(5 skips → 2, coverage +0.177 at the plan budgets), and it moves median lag by 1.7 s only in the
app configuration where units otherwise queue behind whole utterances. At the plan budgets the
median barely changes (3.04 → 3.11 s) because fluent playout deliberately waits behind the
phrase on the bus; that waiting is the design, not this policy.

## Real recognizer rhythm — replay of the reviewed scene

`a2a-pipeline` no longer carries the Apple recognizer/translator adapters (they moved to
`A2ALegacyModels`, driven by `a2a-stage-probe` and `a2a-transcribe`), so the file-matched run of
[dub-anything-review.md](dub-anything-review.md) cannot be repeated from that binary. The recorded
evidence of that run can be replayed instead: the 404 hypotheses of `.local/runs/glossary-test`
(379 partials and 25 finals of the real recognizer, each at its own source-sample frontier) drive
`--asr scripted` over the same 131 s source, with the run's own Apple translations as the
dictionary and `--tts tone`. Inputs and outputs stay in `.local` (recognized text, D9).

Faithfulness first: with the budget disabled the replay reproduces the recorded run — 28 units
against its 29, and the same 373 content characters in the same order with the same two gaps.
So the rhythm being tested is the recognizer's, not a synthetic one.

| budgets | budget | units | played | coverage | onset lag med / p90 / max (s) | catch-up skips |
| --- | --- | --- | --- | --- | --- | --- |
| plan 3 s / 4 s | none (`--max-unit-seconds 0`) | 28 | 24 | 0.722 | 4.37 / 9.80 / 24.42 | 4 |
| plan 3 s / 4 s | 4 s | 34 | 30 | **0.916** | 3.50 / **8.94** / 20.50 | 4 |
| app 8 s / 8 s + adaptive prebuffer | none | 28 | 25 | 0.905 | 5.28 / 11.94 / 24.42 | 3 |
| app 8 s / 8 s + adaptive prebuffer | 4 s | 34 | 32 | **0.979** | 4.62 / **8.16** / 20.50 | 2 |

The gain is the same shape the synthetic fixture predicted: coverage +0.19 (plan) and +0.07 (app),
p90 onset lag 0.9 s and 3.8 s lower, one or two fewer units skipped while catching up. Absolute
lag is higher than the recorded run's 13.18 s maximum because a recorded partial carries no
per-token timing, so spans are estimated from the VAD onset — that inflates both columns equally
and leaves the comparison intact.

Speaking earlier also costs a little accuracy, and the number is small: against the recognizer's
finals, the un-budgeted replay omits 2 characters of 375 (never spoken), the budgeted replay
speaks 2 characters that the recognizer later corrected to homophones. Nothing is spoken twice in
any of these runs — the re-alignment invariant holds on real punctuation, which is what the
synthetic fixture cannot show.

## Change 3 — cuts stay off words (`WordSegmenter`)

Replaying real Mandarin exposed a defect the synthetic run could not: partials arrive with no
punctuation for seconds at a time, so the length budget cut at the last position its key-level
rules allowed — inside words. Two of the 33 unit boundaries in the replay fell strictly inside a
tokenized Mandarin word: one a four-character idiom, one a two-character clan name inside a proper
noun. Neither is quoted here (D9); the check is the count, not the text. `A2ACore` stays model-free, so the rule now consults an injected
`WordSegmenter` (A2ACore/CommitPolicy.swift); `a2a-pipeline` passes Apple's on-device
`NLTokenizer` for Mandarin (A2AModels/NaturalLanguageWordSegmenter.swift — an OS text-analysis
service, so no manifest row and no download, D8), and `forcedLength` splits obey it too. On the
same replay that count goes from 2 of 33 boundaries inside a word to **0 of 33**, with the same
timing aggregates as the row above. `--no-word-cuts` re-enables the old behaviour for A/B runs,
and where the system segmenter is unavailable the policy falls back to its own rules (the test
suite skips rather than fails in that case).

## Limits and follow-ups

- Real partial rhythm and coverage are measured above; **translation adequacy of the shorter
  units is not**. In the replay a fragment the recorded run never produced falls back to the
  fixture dictionary's length placeholder, so nothing there measures a translator. The runnable
  option is the contextual research worker (`--mt context --context-python … --context-worker …`,
  research-only, needs `uv sync --project research/live_dub_lab`, minutes of model load); Apple
  Translation is no longer wired into `a2a-pipeline`. Cloned-voice quality is untouched here.
- The word segmenter is only as good as the system model, and it can be wrong (`重阳/宫高/手` in
  one probe): it prevents the cuts that split a word, it does not guarantee an elegant clause.
  It is injected, so golden traces and `A2ACore` tests stay model-free.
- 4 s is the default because it measured best on coverage; 2.5 s trades 0.3 s of median lag for
  coverage and is available as a knob, not a default.
- Not tried here, from the same review list: isolated-voice conditioning per line (what
  dub-anything does), and shorter translations from a controllable MT.

## DoD

```
source scripts/env.sh && swift test --package-path apple                       # policy + invariant
A2A_PIPELINE="$(swift build --package-path apple --show-bin-path)/a2a-pipeline" \
  uv run --project harness pytest harness/tests/test_runner.py -q              # replay A/B, no respeak
scripts/check.sh                                                              # everything deterministic
```

Real-rhythm replay (`.local` inputs; convert a recorded trace's `asr.partial` / `asr.final` events
into an `hypotheses` script keyed by `span` end, and its `commit.source` / `commit.target` pairs
into a dictionary):

```
$(swift build --package-path apple --show-bin-path)/a2a-pipeline \
  --input .local/runs/clone-live-20261025-2314/source-padded.wav --clock virtual \
  --asr scripted --asr-script .local/runs/ab-realrec/clip-recog.asr.json \
  --mt dictionary --mt-dictionary .local/runs/ab-realrec/clip-recog.mt.json \
  --tts tone --profile-ready-ms 0 --fluent --translation-only --research-text \
  --max-unit-seconds 4 --trace t.jsonl --out out.wav          # 0 disables, --no-word-cuts splits words
python3 research/run_metrics.py <run-dir>…
```

Still open on the interactive tier: fragment translation adequacy via `--mt context`, and
cloned-voice quality with `--tts clone` — neither is answered by a replay.

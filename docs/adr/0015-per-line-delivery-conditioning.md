# ADR-0015 — Speak each line on the actor's own delivery

- Status: accepted (research measurement; off by default)
- Depends on: ADR-0009 (pinned speaker profiles), ADR-0006 (cloned voice on the actor's terms)
- Evidence: reports/week2/dub-parity.md (Delivery), `.local/dub-parity/delivery/ab/`

## Context

ADR-0009 pins one voice profile per speaker: the first segments of their turn build a reference, and
every later line is rendered from it. That is right for *identity* and wrong for *delivery*. A
profile is an average of how the actor sounds across a session, so a shouted line and a murmured one
come out at the same register. dub-anything does not have this problem — it dubs each line with that
line's own clip in the prompt — and its listeners ranked exactly this: "the shouty lines are the
ones that sell it" (`dub-anything/docs/research.md`).

A2A had no instrument for this until now, which is precisely why the gap was invisible: BLEU and
SECS say nothing about whether a shout survived the pipeline (reports/week2/dub-parity.md §1). The
first measurement we have is the one below — the same five lines rendered both ways, where the
pinned profile carries roughly half the actor's register movement that conditioning does. There is
no live alternative either: the actor's delivery of a line is known the moment the line closes, and
it is gone from the model's view if we do not use it then.

## Decision

1. **A unit may be synthesized with its own source audio attached** (`DeliveryClip`), and the worker
   appends it to the speaker's existing reference for that one synthesis. The pinned profile stays
   pinned — conditioning is per request, never a rewrite of the speaker's voice.
2. **The reducer owns the permission** (`SynthesisJob.deliveryAllowed`), because it is the only place
   that knows the two things that matter: whether the operator asked for it
   (`--clone-delivery line`, `PipelineConfig.cloneDeliveryLine`), and whether the unit has real
   speech timing rather than a forced split's guess (`timingEstimated`). A guessed span is not a
   performed line.
3. **Attribution is a precondition.** Conditioning is a cloning path, so it inherits the rule in
   ADR-0005: a unit whose speech is not attributed to a speaker is not synthesized at all, and the
   clip offered with it must be that speaker's. The clip is cut from the unit's own span, the same
   window the attribution decision was made about — it cannot smuggle in another actor.
4. **The worker decides whether the clip can be used**, and says why not. It takes the loudest
   `--max-delivery-seconds` slice of the line, refuses anything under 0.3 s of speech
   (`too_quiet`), refuses when the combined reference would exceed the model's 15 s
   (`reference_bound`), and asks for the line's transcript when the model wants audio-text pairs
   (`needs_source_text`). The verdict travels back as `delivery` and is traced with its cost — a run
   that silently stopped conditioning would otherwise read as a normal one.
5. **In-context conditioning pairs audio with words**, so the clip carries the recognized source
   text (`DeliveryClip.sourceText`, from the committed ASR words — never the English). With
   `--clone-mode xvector` the embedding averages the two clips and no text is needed.

## Costs, measured

`.local/dub-parity/delivery/ab/` renders the same five lines of a real episode twice from the same
pinned profile — once on the profile alone, once with each line's own audio appended:

- reference preparation: **+30–47 ms per line**
- first audio: **0.115–0.125 s → 0.146–0.171 s**, i.e. **+30–50 ms** on a live budget
- dynamics — how much of the actor's register movement survives into the take: **1.28 → 2.76**
- arousal rank correlation across five lines: **0.10 → −0.20**; vocal effort **−0.30 → +0.30**,
  level **0.60 → 0.30**; absolute offset to the actor's scale (`shape`) **1.03 → 1.96**

Read this as the instrument working and the mechanism moving real signal, not as a quality verdict.
Five subtitle-aligned lines from an unisolated mix cannot carry a correlation claim — the confidence
interval on a five-point Spearman is wider than the effect — and the sign flip is exactly why this
gate is measured rather than asserted. `shape` getting worse is expected and not necessarily worse
listening: a pinned profile sits at the session average, while a conditioned take sits wherever that
line actually was.

## Consequences

- The flag is **off by default** and research-only, like cloning itself
  (`tts_identity_qualified: false`). It changes the character of every cloned line, so it earns its
  place by a listening test, not by being implemented.
- Per-unit conditioning makes a **first-token cost visible that pinning deliberately hid**: pinning
  exists to move reference preparation out of the critical path. If preparation grows with a longer
  pinned reference, that is the signal to shorten it.
- The reference bound is hit **earlier** in long-utterance languages: a 12 s pinned reference plus a
  4 s line is already at the model's limit, and the refusal must be reported per run.
- The between-line noise floor is worse in the conditioned variant (−22 → −36 dBFS in this scene),
  which is the same artifact as the appended clip's bed: this is one more reason isolated references
  (next item in the parity program) matter.
- Held against: **per-episode casting** and **QA re-roll** change the actor or the take after the
  fact, which live cannot; conditioning works within what a live session knows at the moment it
  synthesizes.

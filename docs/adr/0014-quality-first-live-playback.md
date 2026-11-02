# ADR-0014 — Complete phrases within a bounded live delay budget

Status: accepted for local development, 27 October 2026, following the user's request:
"quality first. speed next. but need live".

Numbering note: [ADR-0007](0007-portable-runtime-experiment.md) was written the same day on the
branch that was published first, so it kept 0007 and this record took the next free number when
the two branches merged on 2 November. Nothing in the decision changed.

Update: [ADR-0008](0008-separated-background-and-live-delay.md) restores a streaming
default after the user rejected the measured delay. Complete-phrase mode remains opt-in.

The macOS app defaults to **Prioritize complete phrases**. It passes `--quality-first`
to the same pipeline used for process taps and paced file replay. The reducer admits
each phrase only when synthesis has completed; capture, recognition and translation
continue during this wait. This prevents a healthy but slow streaming model from
emptying the bus midway through a phrase and triggering cancellation/recovery.

This mode disables optional early connective/pause commits; recognizer-final results
and normal punctuation boundaries remain available. It allows 20 seconds of projected
debt/source lag and at most 16 pending units. The existing catch-up policy can still
skip unheard work beyond those limits. This is deliberately bounded: it does not
promise that all dialogue will be retained when sustained synthesis or English speech
duration exceeds incoming speech. Explicit CLI debt/lag overrides remain available.

The source is still live. There is no whole-file recognition, future transcript or
pre-rendered audio in the live host. Source-app audio ownership, private taps, callback
safety and the original/translated switch retain their existing contracts. Default
translated playback remains English-only with silence on recovery (ADR-0004).

Disabling the app toggle restores the existing fluent streaming behavior. CLI defaults
are unchanged unless `--quality-first` is supplied. Neither mode establishes voice
identity or dub-quality translation. The research report records completion, delay,
back-transcription and the distinction between file-paced and actual tap evidence.

DoD: `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`; real-model and
tap experiments are separately recorded under `.local/live-dub-lab/`.

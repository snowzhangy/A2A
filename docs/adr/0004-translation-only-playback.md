# ADR-0004 — English-only playback uses silence during recovery

Status: accepted for local development, 24 October 2026, following the user's
explicit instruction: “i should not hear original voice anymore. only live translated”.

This changes the original-audio fallback behavior in ADR-0002 and the default app
experience. When English-only playback is selected, the source tap stays muted,
and the original mixer bus is zero during preparation, translation, recovery,
device discontinuity, and shutdown. Missing or stale English audio produces
silence. It must never enable the original bus as a recovery action.

`PipelineConfig.originalAudioEnabled` enforces the policy in the common reducer;
`a2a-pipeline --translation-only` selects it. The host initializes both gains to
zero before starting the device, and creates a muted tap before warming adapters.
The SwiftUI app now selects this mode. `--captions-only` remains an explicit CLI
mode that leaves source playback intact, and legacy original-fallback experiments
remain available without `--translation-only`.

Stopping a session still destroys its tap and returns audio ownership to the source
app. The local media test stops its player after the test. Host-crash, device, and
physical-output qualification remain necessary before external release. The
existing latency/debt thresholds and original-speaker identity requirements are
unchanged. The system voice is development plumbing, not cloning evidence.

25 October update: the user requested an Original / Translated switch. Translated
remains the app default and retains the silence-on-recovery policy. Explicitly
selecting Original overrides that policy with original-bus playback while keeping
recognition, captions, synthesis, and the prepared voice running. Switching back
uses the current translation state, without restarting capture or enabling automatic
original fallback. Both modes use the same muted tap and mixer, with gain ramps.
`SessionPipeline` owns the listening choice through `.playbackMode`; the bundled
helper accepts bounded mode-name lines over `--control-stdin` and an initial
`--playback original|translated`. English PCM consumed while muted, or interrupted
by an Original selection, does not count as a fully played translation.

25 October update (fluent playout): the app also passes `--fluent`
(`PipelineConfig.finishPhraseBeforeCatchUp`). The phrase being heard always finishes;
later units wait off the bus until it ends. Debt and source lag no longer fall back:
they skip the oldest units nobody has heard yet (`catch_up` trace, disposition
`original_only`, reason `catch_up`), always keeping the newest, so speech continues instead
of pausing for the recovery interval. Worker loss, underruns, and the queue cap still fall
back. Separately, in any English-only session (`--translation-only`, fluent or not), a
unit that cannot be translated or synthesized is skipped rather than triggering a
fallback, which could only have replaced the phrase being heard with silence. With the
original bus audible, such a unit still falls back as before. The 3 s / 4 s thresholds are unchanged; they now decide what is
stale enough to skip.

# ADR-0012 — Delay the picture so translated speech lands on it

Status: accepted for local development, 1 November 2026, following the user's request:
"help me fix the syncize issue of video and audio. I'm think maybe we can delay the video
when we turn on our A2A translation. and make sure the translated voice could match the video."
This reverses the 27 October "no picture delay" direction (lab/live-dub/RESEARCH.md §8) and
ADR-0008's "there is no source-video buffering in the app", both as an opt-out default.

## Problem

Live playback starts each phrase as soon as it is ready. Its English therefore trails its
picture by a different amount every line: 1.57–3.29 s on the deterministic synthetic replay,
4.9 s median and 6.3 s at most in the measured live tap runs (SOUNDTRACK-AND-DELAY.md). No
model speed-up removes this: a phrase cannot be translated before it has been spoken.

## Decision

One delay D, applied to everything the listener sees and hears:

- **Picture.** A2A cannot delay pixels another process draws, so it shows the picture
  itself. The person picks the source window in the system content picker; `A2AVideo`
  captures it with ScreenCaptureKit, hardware-encodes HEVC (no frame reordering, one
  keyframe per second) into a bounded in-memory ring (256 MB cap, whole groups of pictures
  evicted), and an `AVSampleBufferDisplayLayer` decodes it on a control timebase running
  D behind the mach host clock. Raw frames are not held: they are gigabytes for a few seconds
  and holding ScreenCaptureKit's surfaces stalls its pool. Frames never leave memory.
- **Translated phrases.** `PipelineConfig.presentationDelaySeconds` (`a2a-pipeline
  --presentation-delay-ms`, fluent playout only). Each unit's slot is the host time of its
  source start, through the existing capture anchors, plus D. PCM is held until the slot,
  then released; a phrase still on the bus delays the next one, never cuts it. Lag limits
  (source lag, voice and attribution waits, catch-up staleness) count from the slot, so
  nothing expires merely for being delayed; audio still early for its slot is not debt; the
  pending-unit cap grows by D. A phrase more than the lag limit past its slot is skipped,
  including a refill already that late, rather than pushing every later phrase off its picture.
  The newest unit is still always kept. `play.scheduled` carries `late_ms`.
- **Original bus** uses the engine's existing delay line (maximum D + 1 s), so Original
  playback stays on the picture too.
- **Separated background** (ADR-0008) gets D, less the separator's reported latency, of
  silence queued before callbacks start. The tap and the output share the aggregate's clock,
  so that FIFO depth holds for the session.

**Pauses.** The global tap delivers no frames while nothing plays, and the driver loop was
driven by capture. During a pause the driver now runs an idle step every 20 ms: it delivers
model results, tracks playout, ticks, and starts phrases whose slots fall in the pause, which
the picture is still showing. A paused tap captures no trailing silence, so after 0.3 s
without capture one 0.6 s synthetic silence closes the phrase spoken just before the pause
through the usual VAD and segment rules. That silence has no capture anchor, no separated bed
and no output span of its own. If the device kept rendering through the pause and drained the
background FIFO, the next bed write tops it back up with silence. A phrase the pause cuts in
two plays after resume, late by the pause. Not yet handled: the original-bus delay line
follows capture frames, so if the device renders through a pause longer than D, Original
playback leads the picture afterwards (interactive check pending; the fix would pad the
delay line with silence for output cycles that had no input).

Both processes stamp time with mach host time (`a2a_now_ns`, `CMClockGetHostTimeClock`, and
ScreenCaptureKit presentation times; a frame more than 1 s off the host clock is re-stamped
and logged). Output-device and display latency differ per setup and are not derived: the app
offers a live ±500 ms picture fine-tune.

The app enables **Match picture to translated speech** by default for the local backend,
with D = 8 s (3–20 s): 8 s covers the measured live onset delays. The lag limit becomes 2 s
past the slot. Qwen Cloud (ADR-0011) streams provider audio without local slots and keeps
live playback. With D = 0 every path is unchanged; golden traces did not move.

## Consequences

- The person watches A2A's picture window, not the source window. The source app still
  plays, muted by the tap. Some players stop drawing a window that is fully hidden or
  minimized, and the mirror then freezes. Protected (DRM) playback captures as black. These
  are interactive-tier risks, not covered by deterministic checks.
- Captions in the main window still appear when translated, ahead of the picture.
- Everything is D later than live, including the first words of a session and any reaction
  to the audio. Changing D needs a new session; the fine-tune is live but moves only the picture.
- No private API, entitlement, runtime download or persistent data is added. ScreenCaptureKit
  capture uses the system picker's per-session grant, which is on the release-review list
  next to the tap permission.

DoD: `scripts/check.sh` (reducer slot tests `PictureDelayTests`, frame-ring tests
`DelayedFrameQueueTests`, harness `test_picture_delay.py`: every replayed phrase starts within
30 ms of source start + D, and within 50 ms across a simulated 10 s pause), `scripts/build_app.sh`,
`scripts/smoke_app.sh`. The hardware encoder settings are checked with
`A2A_RUN_APPLE_TESTS=1 swift test --package-path apple --filter EncoderConfigurationTests`. Interactive
evidence (picture-vs-click offset with FixturePlayer, live slot lateness) goes under
`.local/live-dub-lab/`.

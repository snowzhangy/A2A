# The system's output is the source: no app to choose

1 November 2026. The window used to list every app that had ever opened an audio object and ask you to pick
one — alphabetically, with Zoom preferred — then tap that app. Two things were wrong with that. You had to
know which app was making the sound (macOS does not tell a third-party process: `kAudioProcessPropertyIsRunningOutput`
says "has an active output unit", which `com.apple.WebKit.GPU` keeps true with nothing playing and `ffplay`
kept true 8 s after its clip ended), and whatever started playing next was not translated. The system's
output mix is what "what is playing right now" means, so that is now the source.

## What changed

- **`A2AAudio` can tap the whole output mix.** `CATapDescription` — the same class our per-app tap uses — has
  a global initializer, `initStereoGlobalTapButExcludeProcesses:`: one tap over everything the machine plays,
  including apps started after us, with a list of processes it leaves out. It is the same API surface
  (`AudioHardwareCreateProcessTap`), the same "System Audio Recording" permission, the same private aggregate
  device and IOProc, so none of the real-time rules move. Header note: that initializer is not in the SDK
  headers (`CATapDescription` is not declared there at all); it was found by introspecting the class at
  runtime, and the tool below is what makes it safe.
- **`ResolvedSource.global`** (`SourceResolver.systemAudio(processes:)`) names no app: `members` is empty, and
  `excluded` is our own output, which is what the tap leaves out. `TapSession.createTap` picks the global
  description when the source is global and looks up our own process objects **at tap time**, because this
  process only registers with CoreAudio when the aggregate starts — a list built earlier could miss the very
  process whose audio must not come back. `SourceResolver.ownAppRoot()` walks up from the executable path to
  the `.app` that contains it, since the app's helper runs from `Contents/Helpers` where `Bundle.main` is not
  the app bundle.
- **`--tap-global`** on `a2a-pipeline`, and `--global` on `a2a-tapctl resolve|capture|monitor|lifecycle`.
  `--tap-bundle` and `--tap-pid` stay: the A1 fidelity check, the A3 lifecycle test and the harness all tap a
  known fixture app, and that is the narrower thing to test with.
- **The window has no source list.** Section 2 was the picker and a Refresh button; it is now one line —
  "Listens to everything this Mac plays. Sound we produce is left out of what we hear." — and starting a
  session needs no selection. `SessionModel.refreshSources()` and the process enumeration went with it.
- **Saved voices keep one store.** "Remember voices for this source" keyed its directory on the chosen app's
  bundle id. There is no chosen app, so the store is keyed `System audio`; identities inside it are still
  separated by voiceprint, which is what actually decides reuse.

## Evidence

- **It hears the machine.** `a2a-tapctl capture --global --seconds 4`: 4 s captured, `-20.4 dBFS` RMS,
  peak 0.50, `record_drops 0`, `silent_capture false`, while a browser played. `resolve --global` reports
  `bundle_id: system:audio`, `global: true`, and the one excluded pid: the tool's own.
- **It does not hear us.** The failure mode of a global tap is a feedback loop — the pipeline answering its
  own translated voice. `a2a-loopprobe` (new tool, `dev.a2a.loopprobe`) plays a 12.5 kHz tone twice while the
  global tap runs: once into our own translated bus, once through `afplay` in another process.

  ```
  ok: our own output stays out of the global mix (control 35 dB up, ours -3 dB)
  ```

  Raw numbers (`reports/week2/raw/global-loopprobe.json`, like other raw output untracked): floor −53.1 dB,
  our tone −56.5 dB — below the floor — and the control tone −18.0 dB. The control sitting 35 dB up is what
  makes the null mean something. Exit code 1 is feedback, 2 is inconclusive (no output route or no
  permission), so it can gate.
- **Unit tests** (`SourceResolverTests`): a system-audio source is global, names nothing, and excludes exactly
  our own processes; the global description carries the exclusion list; `ownAppRoot` resolves
  `A2A.app/Contents/Helpers/a2a-pipeline` to `A2A.app` and a bare path to nothing.

## The first bug, found by using it

The window died as soon as a session started: *"source app exited; restart the session after reopening it"*.
The driver's health check asked whether every process the source names had gone away —
`source.members.allSatisfy { kill(pid, 0) == ESRCH }` — and a global source names **no** processes, so the
empty list made it vacuously true and every session failed at its first check. Fixed as
`ResolvedSource.processesAllExited(liveness:)`, which answers "not gone" for a global source and for an empty
list alike, with a unit test on all four cases (`onlyANamedSourceCanExit`). Verified on the shipped helper:
4 s of the mix through `A2A.app/Contents/Helpers/a2a-pipeline --tap-global`, `captured_frames 235520`,
`global: true`, `underruns 0`, and no such message. Two smaller things from the same look: the run summary now
carries `tap.global` (so a global run cannot be mistaken for a per-app one), and
`a2a-tapctl lifecycle --global` is refused with a reason — that test kills the source app on purpose and a
global tap has none.

For the whole mix the only honest source-failure signals are a **capture stall** and the **default output
device changing** (kept). Correction, from the evening's use: with nothing on the Mac playing, a global tap's
aggregate gets **no IO callbacks at all**, not frames of zeros, so a session started before the video failed
as "capture stalled" after 2 s. Missing frames on a global tap now count as a stall only while another process
has a running output (`ResolvedSource.mixIsIdle`); see [pipeline-latency.md](pipeline-latency.md).

## What this costs, said plainly

- **Muting changed meaning.** `--translation-only` asks for `.muted`, which used to silence one tapped app
  while we played English; on a global tap the tapped set is *every other process*. That is the behaviour you
  want — during a translated session only the English is audible — but it also means a session silences
  alarms, calls and notifications until it stops. **Not yet checked by ear**: whether `.muted` and
  `.mutedWhenTapped` behave that way on a global tap, and whether the Original/Translated switch still gives
  you the machine's own sound, needs a listening pass on the interactive Mac. The 12 s crossfade probe was
  inconclusive about mute (the captured level did not move when our original bus toggled, which is expected
  for the tap but says nothing about what reached the speakers).
- **Consent is wider now.** What we capture includes every sound the machine makes, not one app's — other
  people on other calls, messages chimes, someone else's video. The rules do not change but their scope does:
  `--source-out` audio is restricted research data and stays out of git, traces carry no audio or text unless
  `--research-text`, and a participant whose voice is only in the background of the mix is still a
  participant. Anything recorded through this mode needs consent that covers the whole machine's audio.
- **No per-app attribution.** The trace's source is `system:audio`, so a run cannot be filtered by which app
  spoke; speaker separation continues to come from the DSP voiceprints (ADR-0005) rather than from the app
  boundary. `CATapDescription` also exposes `processRestoreEnabled` and a per-`deviceUID`/`stream` scope, which
  is where per-process attribution inside a global tap would come from — unexplored.
- **ScreenCaptureKit was not needed.** It is the other way to hear system audio; it would have added a second
  capture stack, a different permission (Screen Recording) and buffers with no host-time guarantee, against
  the RT rule that invalid timestamps are reported and never fabricated. The process tap keeps one stack.

## Checks

- `a2a-loopprobe [--frequency 12500] [--json raw.json]` — the feedback gate; interactive Mac.
- `a2a-tapctl capture --global --seconds 4 --out x.wav` — the mix is captured, `silent_capture` false.
- `swift test --package-path apple --filter SourceResolver` — the three new tests.
- `scripts/smoke_app.sh` — the window still starts with no picker; its evidence now reports
  `source: system-audio`.
- A live session in the app (Translated playback) to confirm the mute behaviour and that Original still
  reaches you — the one item above this report cannot close.

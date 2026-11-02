# Restored soundtrack and reduced live delay

The user rejected the 16-second delay and missing background sound. The local app now
defaults to streaming speech with adaptive buffering, 8-second debt/source-lag budgets,
and **Keep music and effects (experimental)**. **Prioritize complete phrases** remains
available for the earlier, longer-buffered behavior. ADR-0008 records this change.

## Actual process-tap measurements

All three rows below use the same 37-second scene, Apple ASR/MT, local Qwen speech,
automatic reference acquisition and an actual private, muted process tap of our
synchronized FixturePlayer. The output device was the selected AirPods. These are
single-scene development runs with varying load and recognition/generation results,
not a controlled statistical comparison or wired-headphone/physical-loopback acceptance.

| Run | Soundtrack | Completed commits | Completed committed-source coverage | Median / max rendered onset delay | Underruns |
| --- | --- | ---: | ---: | ---: | ---: |
| Earlier complete-phrase mode | absent | 13/13 | 100% | 16.17 / 19.09 s | 0 |
| Streaming, debt 6 / source lag 8 | separated | 11/13 | 91.0% | 5.12 / 6.91 s | 0 |
| Streaming, debt 8 / source lag 8 | separated | 12/13 | 90.7% | 4.87 / 6.29 s | 0 |

The last run is the selected app configuration. No PCM drops, capture overflow,
render-monitor overflow, trace record drops or separator failure were reported.
One phrase was skipped by catch-up. Coverage is weighted by committed source spans;
it is not semantic accuracy or coverage of every word in the source. Two tracker
identities still represent three annotated actors. Dub-quality live translation is
not achieved. Render timing excludes unmeasured physical/Bluetooth presentation delay.

Local evidence: `.local/live-dub-lab/responsive-background-tap-01/` and
`responsive-background-tap-02/`, including the source, raw separated background,
actual post-gain rendered output, traces and summaries. `preview-timeline.mp4` in
each directory combines the reference picture with recorded live rendered audio,
including pauses and late final speech. It adds no background in post-processing.
The final picture is held while the remaining recorded audio finishes; picture
alignment is approximate and is not used to measure delay.

## Background path

Apple AUSoundIsolation runs in high-quality voice mode on the ordinary driver thread,
in bounded blocks of at most 4096 samples. A dry instance follows the same processing
path; subtracting the isolated voice from that dry output produces the candidate
background. ASR and cloning references still use the original captured source.

The C mixer has an optional bounded mono background ring, allocated and prefaulted
before callbacks start. Gain commands are atomic. Gain transitions, buffer exhaustion
and resumption use bounded ramps/fades; no separation, allocation, locking, logging or
Swift enters the audio callback. Translation flushes do not discard music. Background
continues during voice acquisition and between phrases, mutes for explicit Original
playback, and fades before Stop completes. Separation failure mutes this bus while
leaving English playback active; original dialogue is never used as its fallback.

A causal 20 ms block separator probe processed the 37-second scene in 1.77 seconds
including setup. The candidate background RMS was 0.01436, versus 0.03855 for the full
source; it was not a silent track. During the selected live run, rendered RMS before
the first English phrase was 0.00885, confirming the independent bus reached the
actual mixer. The rendered peak was 0.914.

Local Mandarin back-transcription found no words in the stage background or either
live background track. That does not prove that every trace of original dialogue is
inaudible; separation artifacts and voice leakage require listening. The current
background is mono and stays close to the source soundtrack timeline, rather than
being delayed to each English phrase. There is no source-video buffering in the app.

## Validation

`scripts/check.sh`, `scripts/build_app.sh` and `scripts/smoke_app.sh` are the DoD.
Mixer tests cover independent background, translation flushes, smooth edges and gain
completion. Reducer tests cover background during acquisition, Original/Translated
switching and Stop while preserving a muted original bus. Real-model/tap evidence is
interactive-tier evidence, separate from deterministic checks.

```sh
scripts/build_stage_probe.sh
apple/.build/debug/a2a-stage-probe background \
  --input .local/dub-comparison/scene-37.wav --out .local/probe/background-NEW
python3 research/live_dub_lab/run_quality_tap.py --responsive-background --debt 8 --lag 8 \
  --input .local/dub-comparison/scene-37.wav --out .local/live-dub-lab/background-tap-NEW
```

The CLI feature is explicit: `--background [--background-out FILE]`. Raw data remains
under `.local/`; default product diagnostics contain no source text or audio.

Final verification passed: 183 executed Swift tests (two Apple runtime tests skipped),
83 harness tests, eight lab tests, SDK/manifest gates, optimized app build and synthetic
startup smoke. Logs: `check-background-stop-20261027.log`, `build-background-final-20261027.log`
and `smoke-background-final-20261027.log` under `.local/live-dub-lab/`.

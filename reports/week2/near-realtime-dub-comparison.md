# Near-real-time comparison with dub-anything — 27 October 2026

The reference is the user's 37-second `condor (dubbed).mp4` and the overlapping
portion of fixture `youtube-NRB0ICxMkrM-0940`. The source tracks align at a
three-second offset, with 0.993 correlation. These are development experiments,
not product acceptance or an identity study. The directed script, source audio,
voice references, generated audio, transcripts and traces remain in `.local/`.

## What changed

- OmniVoice uses the speaker's reference transcript and encoded duration to predict
  output length. Its MLX default instead assumes a fixed English reference; the
  earlier A2A rate calibration was circular because OmniVoice fixes output duration.
- The globally warmed worker no longer generates a warm-up sentence for every
  OmniVoice speaker. It calibrates the first unit's gain from the complete generated
  phrase before sending bounded PCM chunks; Qwen's streaming path is unchanged.
  Fluent pacing uses the reference-based duration estimate for OmniVoice.
- The macOS app now lets a user select translation-terms and pronunciation JSON
  files, and validates them before starting the helper. Pronunciation changes the
  spoken text, not captions. This exposes the previously CLI-only controls.

## Matched voice samples

Four lines across three speakers were generated with the same directed English text
and the same clean, per-line voice references. Times exclude recognition, translation,
capture, device playback, and competing work. VoiceStudio uses PyTorch/MPS;
A2A uses its pinned MLX OmniVoice and Qwen models. Single generated takes do not
establish quality or identity equivalence.

| Engine | Median reference preparation | Median first audio | First line duration |
| --- | ---: | ---: | ---: |
| dub-anything / VoiceStudio OmniVoice | 0.64 s | 9.36 s | 3.17 s |
| A2A OmniVoice, original duration estimate | 3.83 s | 4.30 s | 7.01 s |
| A2A OmniVoice, reference-based estimate before preparation change | 4.09 s | 3.89 s | 3.49 s |
| A2A OmniVoice, final preparation change | 0.25 s | 3.80 s | 3.38 s |
| A2A Qwen, streaming | 0.59 s | 0.16 s | 5.56 s |

The final OmniVoice sample's Apple back-transcript flagged a repeated short phrase
and a one-word substitution in another line. These are recognition-based flags,
not a listener verdict. Faster preparation does not establish a quality improvement.
The 37-second guided candidate uses the reference's human-directed script, clean
per-line references and pre-separated background. It was made before the final
profile-preparation change and is a rendering comparison, not output produced
automatically by the live app.

## File-paced live pipeline

On the full 120-second source, reducing the forced split from 60 to 24 keys reduced
the maximum observed onset lag from 13.18 to 8.06 s in single runs, but coverage by
committed source duration fell from 0.708 to 0.591. The 24-key setting also produced
three forced splits. It is not enabled by default: the experiments vary across runs,
coverage is a trace diagnostic, and forced cuts can damage meaning.

The updated OmniVoice worker was also run through the actual file-paced pipeline
on the 37-second scene, with speaker acquisition and Apple ASR/MT. It played 7 of
14 committed units, with 0.497 coverage by committed source duration, median onset
lag 8.98 s, and median synthesis first audio 4.78 s. This run preceded the final
whole-phrase gain calibration but includes the faster profile preparation. It used
no process tap or physical output. These numbers do not match the directed dub or
the near-real-time product goal; Qwen remains the default streaming model.

## Verification

`scripts/check.sh` passed (173 Swift tests executed, two interactive tests skipped,
81 harness tests, all SDK stamps and the manifest gate). `scripts/build_app.sh` and
`scripts/smoke_app.sh` passed on the provisioned Mac. The smoke check uses synthetic
data and verifies that a missing terms file blocks startup before audio takeover. Real-model file runs and Apple
back-transcription are separate interactive-tier checks; they do not test a live
tap, sleep/wake, device changes, or a listener's judgment.

Local evidence: `.local/dub-comparison/`, including `a2a-directed-37s.mp4`,
`a2a-current-live-37s.mp4`, `a2a-omni-live-20261027/`, the matched samples, and
`check-final-20261027.log`. The two videos are different workflows and must be
listened to against the user's reference before judging voice likeness or the mix.

Next work is streaming recognition with reliable clause boundaries, shorter
meaning-preserving translation, and a voice-free background path. All require
measured semantic coverage, speaker assignment and listening checks; a larger
speed cap or arbitrary forced split cannot substitute for those checks.

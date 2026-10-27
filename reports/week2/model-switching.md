# Experimental voice model switching — 25 October 2026

Enable experimental voice cloning in the app, then choose a voice model. Stop before
switching: the old worker exits and releases its model before the next session starts.
The default system-voice mode remains available.

| Choice | Installed artifact | Total setup bytes, including dependencies | Audio delivery |
| --- | --- | ---: | --- |
| Qwen3-TTS | 0.6B Base, 4-bit MLX | 1,711,327,105 | Audio chunks during generation |
| OmniVoice | 0.6B bfloat16 MLX, full Higgs audio tokenizer | 2,042,327,608 | Completed phrase, then bounded IPC chunks |
| Chatterbox Multilingual | V3 MLX, separate S3TokenizerV2 | 3,207,975,456 | Completed phrase, then bounded IPC chunks |

All three are installed on the development Mac. Missing models show an explicit
**Install model** action and their total setup size; translated sessions cannot start
with a missing selection. Setup resumes downloads, verifies size and SHA-256, and
promotes the selected artifacts atomically. Runtime loading verifies the hashes again
and stays offline. No new Python dependency was required beyond the existing locked
research environment.

`--clone-model qwen|omnivoice|chatterbox` selects the same backend in the CLI. Qwen is
the default and supports ICL and xvector conditioning. The other choices use ICL;
incompatible CLI combinations fail before worker/tap startup. Each backend uses the
selected translation direction and retains reference audio/tokens/conditionals only
in memory. Profile preparation caches each speaker's conditioning for subsequent units.
Diagnostics identify the selected model, pinned conversion revision, and actual audio
streaming capability. All summaries retain `tts_identity_qualified: false`.

The Chatterbox loader initializes its speech tokenizer from the verified manifest
dependency directly. It does not call the upstream automatic, unpinned Hub download
hook. The setup subprocess drains and bounds diagnostics, caps catalog output, and
reports failure rather than accepting stale readiness.

## Verification

- `scripts/check.sh`: passed with **160 executed Swift tests**, **73 harness tests**,
  all seven executable SDK stamps, and **13 manifest entries / 4 enabled / 0 problems**.
  The two opt-in Apple adapter tests remain skipped by this deterministic command.
  Evidence: `.local/model-switch-check-final.log`.
- Contract tests exercise selection, both language codes, cached conditioning,
  missing dependencies, CLI validation, each fake worker's streaming capability,
  bounded setup output, large stderr progress, and nonzero setup exits.
- `scripts/build_app.sh`: optimized app built and signatures verified.
- `scripts/smoke_app.sh`: passed; the native synthetic UI switches all three choices,
  blocks missing models, and retains direction and playback controls.
  Evidence: `.local/runs/app-smoke.zPpUBz/app.json`.
- Separate real local inference ran all three models sequentially on the same synthetic
  English reference and scripted English → Mandarin unit. Each loaded successfully,
  completed one audio unit, and reported zero runtime errors, rejected PCM, dropped
  PCM, and underruns. Installed Apple Mandarin recognition recovered the requested
  sentence from each generated WAV. Evidence: `.local/model-switch-real-{qwen,omni,chatterbox}.log`,
  matching trace/WAV files, and `.local/probe/model-switch-asr-{qwen,omni,chatterbox}/result.json`.
- `git diff --check` passed. No capture or device playback was used in these probes.

The single synthetic sentence verifies functioning model selection and content delivery;
it does not rank naturalness, identity, emotion, accent preservation, or sustained speed.
Whole-phrase model calls cannot currently be interrupted mid-generation; cancellation
discards their results. OmniVoice duration estimation and both new models' latency need
broader Chinese/English testing under simultaneous ASR/MT load.

## Provenance and release status

The added model and codec artifacts have pinned revisions and file hashes recorded from
publisher metadata or downloaded bytes by Codex. Downloads subsequently verified every
recorded artifact. Numerical parity and product qualification remain pending.

OmniVoice's TTS conversion identifies Apache-2.0, while its bundled audio tokenizer
includes the **Boson Higgs Audio 2 Community License**. Chatterbox's conversion identifies
MIT. S3TokenizerV2 identifies its Apache-2.0 CosyVoice2 source but has no separate
conversion license declaration; that distinction is recorded in the manifest. These
are research integrations and require the existing external-release review before
distribution. They do not add background music/dialogue separation.

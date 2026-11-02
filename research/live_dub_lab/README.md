# Live dubbing lab

Latest: [returning-actor voice consistency](VOICE-CONSISTENCY.md). Cloned sessions keep
one prepared reference per speaker; actor tracking remains imperfect. The app
[streams with separated music/effects](SOUNDTRACK-AND-DELAY.md); the long-buffer mode is opt-in.

Research workspace for the user's local live-dubbing and returning-actor experiments.
Experiments live here; changes adopted into the app are recorded in the progress report.
All audio, annotations, vectors, traces, databases and generated videos belong under
gitignored `.local/`.

[The continued experiments](PROGRESS-2026-10-27.md) describe the adopted Qwen preparation
and clause-safety changes, optional adaptive buffering, lower-step OmniVoice, and the
distinction between started and completed playback. The actor database remains separate
from app voice memory.

[Quality-first live playback](QUALITY-FIRST.md) documents the earlier complete-phrase default, actual
process-tap evidence, bounded phrase buffering, and contextual translation experiments.

## Research findings

The current A2A DSP tracker can merge similar voices. On the supplied manually labeled
37-second scene, the earlier 120-second file-paced run assigned 83.9% of the annotated
dialogue duration to any tracker label and 58.8% to the correct actor after the best
possible label mapping fitted on that scene. This is an approximate span-overlap
diagnostic, not diarization error rate, semantic completeness or proof of identity.

Qwen's bundled voice encoder correctly ranked 9/9 later annotated turns in this one
scene against the first annotated turn for each known actor, both for full turns and
their first second. Two unknown actors, however, had cosine scores of 0.978 and 0.970;
these overlap the known scores. There is no qualified rejection threshold. Initial
enrollment uses manually identified, cleanly segmented turns; the result does not
establish automatic segmentation, new-episode recognition, or safe unknown rejection.
On a selected subset of existing tracker spans, after excluding any span overlapping
the enrollment turns, both Qwen nearest-neighbor recognition and the tracker with a
mapping fitted on this clip scored 95.5% of 13.11 seconds of annotated speech. This
subset excludes substantial and difficult portions of the scene; it does not explain
away the tracker failure above. The Qwen encoder is part of the TTS model and is not
an independent voice-likeness judge.

On the 37-second scene, two sequential real-time-paced file runs used Apple ASR/MT,
Qwen TTS, and the existing DSP tracker. Raising the queue limits from 3 seconds debt /
4 seconds source lag to 6 / 8 played 12/14 vs 11/14 commits. Median onset lag was
4.47 vs 3.82 seconds; 84.4% vs 73.4% of annotated speech duration overlapped a played
commit. Neither span overlap nor a played commit guarantees correct translation.
Each setting was run once; the clip, nondeterministic model execution, and warmup
limit any conclusion. No process tap, output device, listening study, or held-out
episode was tested. The two private comparison MP4s are cut at 37 seconds and can
therefore omit translated speech that would occur after the final video frame.
Those early queue MP4s also used the compact translated speech stem, which omits
empty-bus time; use the corrected timeline previews in the progress report for timing.

The existing app voice-memory path was also run twice on the same 37-second scene,
with 6 / 8-second queue limits. The first pass stored four reference versions for two
tracker identities. The second pass reported two reused profiles, ready at 4.00 and
11.86 seconds of source time, and still detected only two tracker identities for the
three annotated actors. This tests persistence on one replay, not whether the correct
actor was assigned to every line or recognized in a different episode.

The upstream [mlx-audio Qwen documentation](https://github.com/Blaizzy/mlx-audio/blob/main/mlx_audio/tts/models/qwen3_tts/README.md)
distinguishes audio streaming from text input: its generator receives complete text
before emitting audio. [pyannote's speaker embedding pipeline](https://github.com/pyannote/pyannote-audio/blob/main/src/pyannote/audio/pipelines/speaker_verification.py)
assumes a single speaker in each extracted segment. Together with the experiments,
these observations motivate better segmentation, known/unknown calibration, and
end-to-end measurement rather than relying on TTS first-chunk time.

## Local prototype database

`actor_memory.py` is a private SQLite embedding store, separated by exact model ID.
It groups multiple exemplars by actor and supports ranking. It stores embeddings and
user-supplied actor/sample labels, not original PCM, transcripts, model weights, or a
usable TTS cloning reference. Directories are 0700 and the database is 0600. It does
not enroll automatically, train or fine-tune a model, validate consent, encrypt its
contents, reject unknown speakers, or integrate with the app. It is not a production
biometric system. Delete the enclosing experiment folder to remove this database.
Existing app voice memory is a separate opt-in store; its Forget action does not delete
this research database.

## Reproduce

The paths below are generated by the earlier local comparison and are not in git.
The model must already have been installed and verified via the repository's explicit
setup workflow. Runtime model downloads remain disabled.

```sh
python3 -m unittest discover -s research/live_dub_lab -p 'test_*.py' -v
uv run --offline --locked --project research python research/live_dub_lab/evaluate_actors.py \
  --plan .local/dub-comparison/plan-full.json --audio .local/dub-comparison/source.wav \
  --out .local/live-dub-lab/actor-eval-NEW
uv run --offline --locked --project research python research/live_dub_lab/evaluate_tracker.py \
  --plan .local/dub-comparison/plan-full.json --audio .local/dub-comparison/source.wav \
  --trace .local/dub-comparison/a2a-live-current/trace.jsonl \
  --memory .local/live-dub-lab/actor-eval-NEW/actor-memory \
  --out .local/live-dub-lab/tracker-eval-NEW.json
python3 research/live_dub_lab/run_queue_experiment.py \
  --input .local/dub-comparison/scene-37.wav --out .local/live-dub-lab/queue-eval-NEW
python3 research/live_dub_lab/run_memory_replay.py \
  --input .local/dub-comparison/scene-37.wav --out .local/live-dub-lab/app-memory-eval-NEW
python3 research/live_dub_lab/audit_scene.py \
  --plan .local/dub-comparison/plan-full.json \
  --trace .local/live-dub-lab/queue-eval-NEW/baseline/trace.jsonl --offset 3
```

Next research: obtain separate episodes with manually verified actor identities and
unknown actors; compare a separately qualified speaker encoder, assess false accepts
and misses at fixed thresholds, and measure model contention alongside live ASR/MT/TTS.
Then investigate speech-turn splitting, reliable reference selection, concise context-
aware translation, and voice-free background mixing. Only integrate an approach after
these tests and a listening comparison demonstrate improvement.

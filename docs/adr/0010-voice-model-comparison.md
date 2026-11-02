# ADR-0010 — Compare local voice models on the direct Hy path

Status: accepted for local development, 30 October 2026, at the user's request.

The app offers Qwen3-TTS, Audio8-TTS-Realistic 0.6B, OmniVoice, and Chatterbox
Multilingual. The last is an additional audio-only reference baseline, not a claim
of superior quality. Qwen uses two streaming workers; the other models use one worker
and the pinned MLX implementations generate complete phrases before emitting audio.
All cloned voices are research-only and identity-unqualified.

Hy remains the sole translator. Qwen and Chatterbox condition on a speaker's audio
without a transcript. Audio8 and OmniVoice require the words spoken in their reference:
a separately bundled local SpeechTranscriber helper receives only the selected
speaker's short reference through a pipe and returns its transcription in memory once
when that speaker's profile is prepared. Its locale is the source language, not the
translated target language. No continuous Apple recognition, translated text,
reference PCM, or transcript is written to diagnostics or disk. An explicit setup
button installs OS speech assets if necessary; session startup never downloads them.
Speech-model or reference failures do not authorize using another speaker's voice.

Audio8's 44.1 kHz PCM is resampled to the worker's 24 kHz protocol. Its 4-bit MLX
conversion quantizes the tied embedding; the installed mlx-audio 0.6.1 implementation
needs `QuantizedEmbedding.as_linear` for its tied output projection. It also
requires the attention cache to use the decoded embedding's floating-point dtype;
using the packed integer weight dtype produced unintelligible speech. The overrides
are restricted to Audio8's quantized model; conversion parity and voice likeness
are not qualified. Codec-frame generation limits are scaled for the model's 21.5 Hz rate.
Qwen remains the default; voice switching only applies to the next session. Model
and runtime artifacts are pinned and verified through the existing explicit setup path.

DoD: `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`, plus real
worker smoke tests on synthetic Mandarin audio, without microphone or live capture.

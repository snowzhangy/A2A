# Voice model selector — 30 October 2026

The local app now offers Qwen3-TTS, Audio8 Realistic, OmniVoice and Chatterbox.
See ADR-0010 for model conditioning, transcription, privacy and provenance. All
four use verified local artifacts and remain research-only, identity-unqualified.

A short synthetic Mandarin reference and one English sentence were processed through
each real resident worker. The source reference and output stayed in memory. These
single-sentence tests are implementation smoke checks, not comparable quality,
identity or sustained-load benchmarks. First-audio times and durations, in seconds:

| Model | Reference preparation | First audio | Generated speech | Streaming |
| --- | ---: | ---: | ---: | --- |
| Qwen3-TTS | 0.01 | 0.21 | 1.92 | yes, one worker in this test |
| Audio8 Realistic | 0.89 | 3.07 | 3.34 | no; output later found unintelligible |
| OmniVoice | 0.98 | 2.61 | 1.62 | no |
| Chatterbox | 1.86 | 1.65 | 1.28 | no |

The Audio8 and OmniVoice reference-preparation times include the local, in-memory
reference transcription. Their source-language speech assets were installed beforehand.
These numbers exclude capture, Hy translation, device output, and contention; they do
not compare the two-worker Qwen app mode. The words and audio were synthetic.

The initial Audio8 integration used the packed integer weight dtype for the attention
cache. Its synthetic generation above and the initial live user test produced
unintelligible sounds, so those Audio8 timings are not valid quality evidence. The
cache now uses the decoded embedding output's floating-point dtype, matching upstream.
On a new isolated synthetic sample, local back-transcription recovered the exact
sentence "We will meet next week." The same sentence was also recovered after
the full resident-worker protocol, including shaping and 44.1→24 kHz resampling.
This is an intelligibility smoke test, not a
listener judgment or proof of voice matching.

A 14-second synthetic file was then processed with the real Hy server and Audio8,
automatic speaker tracking, and the normal direct-audio pipeline. Four Hy requests
completed, two of four units reached completed playback, and two were abandoned when
the finite test stopped; no trace errors were recorded. This is not a live-tap,
coverage or listening verdict; it used the broken cache and its audio was not
intelligible. Raw synthetic trace and output are under
`.local/runs/voice-comparison-smoke/`.

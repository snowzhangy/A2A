# Hy-SimulMT with responsive Qwen cloning

The development app selects Qwen3-TTS cloning by default and retains the last
Hy-SimulMT server address, initially `http://10.0.1.29:8080`. Responsive playback
uses the existing adaptive prebuffer, an 8-second debt limit, and a 20-second
source-age limit for Hy-SimulMT with cloning. The
complete-phrase option retains its existing 20-second limits. These limits may
discard unheard work under sustained overload.

On 29 October 2026, a five-second synthetic Mandarin clip was replayed in real time
through the Apple recognizer, the user's resident Hy-SimulMT server, and the
installed Qwen 0.6B worker with an automatically acquired reference. The worker
loaded in 3.24 s and warmed in 1.36 s before the replay. The source phrase committed
at 4.02 s, translation completed at 5.04 s, the first synthesized chunk arrived at
5.20 s, and rendered playback started at 5.30 s and completed at 9.40 s. The
reference contained 4.02 s of source audio and prepared in 0.07 s. There were no
underruns or dropped PCM in that run.

This was one file-paced, single-speaker synthetic test, not a live tap, listener
study, identity qualification, or evidence of long-session coverage. The worker
remains research-only. In multi-speaker sessions a speaker's line still waits for
its own safe voice profile and may be skipped if attribution or reference acquisition
does not complete within the configured limits. The current Hy adapter submits
closed audio segments; it does not stream input continuously.

Overlong finalized speech may contribute shorter reference clips only when every
token has nonoverlapping timing and each resulting clip passes the existing
single-speaker coverage and signal checks. Coarse timing is never divided or
guessed. A 16.2-second synthetic single-speaker utterance, replayed with the
automatic speaker tracker, produced a 3.12-second reference and a ready voice at
16.37 s; first output began at 16.61 s. Two translation requests failed and one
unit was skipped for catch-up; only the first unit played. This is not a successful
long-speech live benchmark: it illustrates that waiting for final recognition can
still delay the first voice and that the segment-based server adapter needs work.
The worker also emitted a resource-tracker warning about a leaked semaphore on exit.
This behavior has not yet been qualified in a multi-speaker live run.

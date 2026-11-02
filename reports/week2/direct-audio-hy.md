# Direct audio translation experiment

The experimental Hy + Qwen path can segment captured audio with the existing energy
VAD and send it to Hy without constructing an Apple speech recognizer. Spans close at
VAD ends or after three seconds of continuous audio. The existing reducer continues
to own speaker attribution, queue limits, cancellation and playback. Qwen uses its
audio-only x-vector conditioning, with a minimum three-second reference and the same
speaker-coverage checks. No Chinese transcript is produced in this mode. Remembered
voices and non-Qwen clone models retain the transcript-based path.

On the local synthetic `zh-demo` fixture (38.1 s, one synthetic voice), both runs used
the same Hy server, two resident Qwen workers, fluent playback and the same debt/lag
limits. Each completed eight of eight generated units, without reported errors or
catch-up skips. The direct path's median rendered onset from source-unit onset was
4.05 s (first 4.36 s), versus 4.21 s (first 4.42 s) for Apple Speech + ICL. This is
one run per path; boundaries and conditioning differ, so the numbers are not a
controlled perceptual-quality or identity comparison. The tracker split the single
synthetic voice into two identities in both runs. Worker warm-up precedes the source
timeline and is not included in onset latency. No physical loopback was measured.

The direct path is still bounded-segment translation: the HTTP response is collected
until complete before synthesis. An energy threshold can mistake music for speech,
and forced cuts may split words or meaning. English quality, identity similarity and
robustness on a real soundtrack need listener testing. Audio-only conditioning and
the research model remain unqualified for identity. Further reduction requires better
speaker/reference acquisition or verified incremental translation and speech output;
this small sample does not establish a general speedup.

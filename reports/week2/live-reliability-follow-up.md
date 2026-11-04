# Live reliability follow-up

3 November 2026. The monitored English → Mandarin session used local Hy-SimulMT and
two Qwen voice workers, with an 8 s picture delay. After about 15 minutes, 275 lines
had completed playback; one early underrun canceled two units, and five units had
no speaker label or ready profile. Of 285 target commits, 165 were shortened; in
those with a synthesis request, 45% of the model's characters were removed. These
are trace counts, not human judgments of meaning or speaker identity. Scheduling
lateness was 12 ms median, 210 ms p90, with 18 starts more than 500 ms late.

The first underrun exposed a prebuffer defect: the adaptive estimate measured speed
up to the last chunk, even when generation had since stalled. It now includes the
time elapsed since that chunk when considering playback. Complete PCM remains safe
to start. On a modeled delayed slot, a stalled stream is held until generation
finishes; the change can make a line later when generation is actually slow.

The `--quality-first` mode used to wait for a complete take while shortening its
text first. It now keeps the accepted target and leaves the hard model-output limit
in place. It can overrun the picture or drop units when its bounded queue fills;
it is opt-in and does not establish complete spoken coverage. Captions preserve
the full accepted model response in either mode.

The existing Qwen 4B Instruct checkpoint was also tested offline on four synthetic
sentences with instructions to preserve every fact. Two Chinese rewrites omitted
material facts (a monetary amount and the day moved from). Its single-request
times were roughly 0.4–1.3 s without the translation and speech models competing
for the GPU. It is therefore not used to rewrite live translations; this tiny
example is a safety counterexample, not a quality benchmark.

An exploratory simulation of 220 previously measured English → Mandarin units
used each line's measured pace and the actual next gap, an oracle unavailable in
live playback. Increasing the spoken budget from 5 to 5.5 target characters/s
reduced modeled removals from 17% to 11%, but doubled predicted overruns of more
than 250 ms (14 to 28). The model assumes pace is stable as text changes, and it
does not measure semantic accuracy or actual synthesis. Consequently the default
budget is unchanged pending an end-to-end A/B and listener evaluation.

A 27 s synthetic English clip was then replayed through local Hy, two Qwen workers,
language identification, and the isolated background path with an 8 s picture delay.
Both timing-oriented and complete-phrase modes played six translated units and restored
the same initial unit while its voice was unavailable; neither entered fallback and
neither restoration was late. Timing-oriented mode shortened 2 of 7 accepted targets;
complete mode shortened none. Maximum scheduling lateness was 77 ms versus 432 ms.
Both modes had one take report that it reached the worker's normal token budget.
This is one synthetic, single-speaker example, not an identity, semantic, or long-run
quality gate. It led to the opt-in 1.5× worker budget in complete-phrase mode.
Repeating complete mode with that budget kept all 7 accepted texts unshortened,
played the same 6 translated units, restored the same initial unit on time, and
reported no token-budget truncations. Its maximum scheduling lateness was 734 ms.
These are separate stochastic inference runs; they show the observed tradeoff on
this one clip, not a guarantee that the longer budget fixes every truncated take.

Regression checks: `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`,
and `PYTHONPATH=runtime uv run --project harness python -m pytest runtime/tests -q`.
Runtime parity covers only scenarios actually present in its golden tests; it does
not establish support for delayed picture or adaptive prebuffering.

Later monitoring of a session reported to have one speaker showed four prepared
speaker profiles and 26 tracker changes in about eleven minutes. The app now defaults
to one speaker, bypassing tracker-driven voice changes; multiple-speaker tracking is
still selectable for dialogue. A user may optionally supply a clean 1–15 second WAV
reference for a single speaker. The app validates the file before capture and does not
persist it. Neither choice establishes perceptual identity; no user audio or transcript
was retained for this diagnosis.

On the 27 s synthetic single-speaker clip with a fixed five-second reference, the local
Hy and Qwen pipeline prepared one profile, assigned all seven units to that speaker,
played all seven and reported no fallback or speaker change. This checks routing and
startup, not whether a synthesized voice sounds like the reference.

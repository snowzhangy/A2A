# ADR-0006 — Reusable voice references for local research sessions

Status: accepted for local development, 25 October 2026, following the user's request
to remember a person instead of preparing their voice from scratch in every session.

Cloning conditions a pretrained model on reference speech; no model weights are trained.
Previously the worker reused conditioning within one session, then discarded it at stop.

The app now offers **Remember voices for this source**, disabled by default. It maps the
source identifier to a directory under `.local/voice-profiles/`. The CLI accepts an
explicit `--voice-memory DIR`, including restricted research storage. This is a deliberate
exception to default in-memory-only references; default diagnostics still contain no
audio, transcripts, or voiceprints.

The Swift host stores source reference PCM, its matching transcript, and a DSP voiceprint.
The Python inference worker remains memory-only. Directories use 0700 permissions and
files 0600. Writes are atomic and serialized on an ordinary utility queue. At most 16
identities, with the initial and upgraded reference of each, are retained. Loading checks
file size, reference duration, finite samples, and feature dimensions. Corrupt files and
symbolic links are rejected. Metadata/model readiness work stays off the UI thread.

Matching uses source voice features and pitch with a strict distance and an ambiguity
margin. It groups two exemplars of one saved identity before comparing alternatives.
`S1` means the first tracked voice in a session and never identifies a stored person.
An uncertain match collects a fresh reference. A reused reference is reconditioned for
the selected model and target language in the new worker; this is not retraining.

The store contains biometric reference data and is a local research store, not an
encrypted production identity service. The user may delete a source's store using
**Forget saved voices** while the session is stopped. External release still requires
the project's license, consent, identity, and data reviews. DSP matching can confuse
similar voices and is not qualified biometric recognition.

The linked [dub-anything workflow](https://github.com/swax/dub-anything) is a useful
comparison for stable actor references and generated-line checks. Its offline separation,
whole-video casting, and rerender workflow do not establish real-time performance for
A2A. No VoiceStudio or other external project code was incorporated here.

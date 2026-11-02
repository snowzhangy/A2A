# Remembering each actor's voice for the whole session (31 October)

Question: learn a voice once from its first 3 s, then never wait again for that actor, with several actors.

## What the app does now

Each tracker identity gets one profile, locked for the session (`session_voice_locked`, `S1 v1` is
never replaced). So a voice is already remembered, but only under the DSP tracker's label
(`VoiceprintSpeakerTracker`: mel cepstrum, at most 4 speakers, prefers merging). A second wait only
happens when the tracker invents a new speaker (the 30 October synthetic run: one voice became S1 and S2,
and the S2 line waited 4.7 s).

On real TV the tracker does not separate the cast. `a2a-stage-probe speakers` on the original mix of
two whole episodes, each started at its first scene (ep08 from 80 s, ep09 from 140 s; the theme songs
end just after), scored against the offline CAM++ clusters of the same episode (`enroll.py`; clusters
with the same name, such as ep08's Yang Guo in two registers, count as one person; 11 and 10 people,
574 s and about 800 s of their speech):

- Four identities per episode. In ep09, S1 and S2 each hold all 10 people (largest share 26% and 52%);
  S3 and S4 were learned from music.
- The first 3 s of each identity, the voice the app locks, came from music or from another person for
  almost everyone: 0% (ep08) and 4% (ep09) of the cast's speech is voiced from its own actor.
- Started at 0 s instead, ep09 learned both main identities from the theme song.

## Session voice bank (`session_bank.py`)

Same segments, same order, one speaker embedding per segment, from an empty bank:

- A segment of at least 1 s joins the voice whose top-3 exemplar similarity is at least `accept` with
  a 0.05 margin over the next voice. Short segments need `accept + 0.05`.
- A piece that follows the previous one within 0.3 s (the 3 s forced cut inside one turn) keeps that
  voice unless its similarity drops below 0.4.
- Close but not accepted (best at least 0.45): no voice, and never a new one. This is what stops the
  fragmentation: without it the bank made 200 voices.
- Otherwise the segment joins a pending voice. A pending voice is usable once it holds 3 s of speech,
  which is the app's x-vector reference. It is then remembered for the rest of the session.

Mix unless stated. "Own voice" counts cast speech voiced with a voice learned from that same person's
speech, which is what a listener hears; "majority" credits each voice to the person who uses it most,
which is kinder. ep08 is held out: the rules were tuned on ep09.

| | ep09 own | ep09 someone else's | ep09 no voice | ep08 own | ep08 someone else's | ep08 no voice | Cast speech waiting (ep09 / ep08) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| App DSP tracker | 4% | 96% | 0% | 0% | 100% | 0% | not modelled |
| Bank, CAM++, accept 0.5 | **65%** | **26%** | 10% | **61%** | **23%** | 16% | 3.7 s / 7.8 s |
| Bank, CAM++, accept 0.55 | 60% | 28% | 13% | 57% | 13% | 29% | 5.5 s / 6.5 s |
| Bank, CAM++ on separated vocals, 0.5 | 66% | 29% | 5% | 58% | 33% | 8% | 2.3 s / 3.9 s |
| Bank, ERes2NetV2, accept 0.5 | 60% | 33% | 7% | 48% | 40% | 12% | 1.2 s / 8.0 s |
| Bank, ERes2NetV2, accept 0.55 | 65% | 26% | 9% | 65% | 18% | 17% | 3.3 s / 5.9 s |

With majority credit, the tracker reaches 36% and 44% and the CAM++ 0.5 bank 75% and 70%. Extra voices
for one person: 0–3 per episode for every bank row.

"Cast speech waiting" counts cast speech that arrived while its own voice was still pending, over the
whole episode: returning actors almost never wait again. Music and noise still create voices (V1 in the
run from 0 s was the theme song). In the app those voices would speak whatever Hy returns for music and
take part in the margin rule against real actors, so they need the filler gate as well.

Limits:
- Thresholds were swept on ep09. On held-out ep08 the CAM++ 0.5 bank drops from 65% to 61% own voice.
- The turn rule hardly matters: disabling it changes own voice by 1–3 points on both episodes.
- The reference clusters are CAM++ too, which favours the CAM++ bank. ERes2NetV2 is a different
  network and still reaches 60–65%, so the gain is not only agreement with the reference.
- Nothing here was checked against the picture. The remaining wrong voices are mostly two cast members
  sharing one voice (ep09: 22 of the 26 points), not voices learned from music (4 points).
- The bank leaves 5–29% of cast speech with no voice; with `speaker_attribution_required` those lines
  wait and are skipped. A fallback (nearest voice within a turn, or above a low floor) is not measured.

## The voice model's own speaker vector is not enough

The Qwen3-TTS speaker encoder (`extract_speaker_embedding`, the vector the app already computes for
cloning) on the same 294 segments of at least 1 s:

| Embedder | Same/different-actor AUC | EER |
| --- | --- | --- |
| CAM++ (3D-Speaker) | 0.952 | 11% |
| ERes2NetV2 (3D-Speaker) | 0.953 | 11% |
| Qwen3-TTS speaker encoder | 0.825 | 25% |

Its vectors all sit near 0.95 similarity. Recognition needs a separate speaker-verification model.
On one CPU thread (sherpa-onnx), CAM++ takes 42 ms per segment (mean 2.8 s) and ERes2NetV2 245 ms, so
CAM++ fits the live path.

## Proposal for the app (not built)

1. Replace the DSP tracker's identity decision with this bank: a speaker-verification embedding per
   VAD segment, the rules above, and the first 3 s of each voice as its locked reference. The tracker
   protocol (`SpeakerTracker`, `.assign(speaker, span)`) stays the same.
2. No cap of 4 speakers.
3. After the session, offer to save the bank as the series' actor database (consented, 0700/0600). The
   next episode then recognizes returning actors from their first line (SessionTracks result in
   `RESEARCH.md` section 5), with no 3 s wait at all.
4. The Swift side has no ONNX runtime: the embedding needs either an `embed` request in the Python
   clone worker (which only runs in clone mode) or a Core ML conversion.
5. The embedding model needs a manifest entry with its verified hash and license before it can be used.

Data: `.local/live-dub/runs/tracker-ep08/` and `tracker-ep09/` (labels, scores, embeddings; 0700/0600).

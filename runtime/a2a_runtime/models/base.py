"""Stage adapter contracts for the portable driver (IMPLEMENTATION_PLAN.md section 4.2).

The driver talks to recognizers, translators, and synthesizers through these duck-typed
shapes; worker-backed adapters (a2a_runtime.workers) and fixtures both satisfy them.

Recognizer:   accept(AudioFrame16k), request_finalize(), drain_hypotheses() -> [ASRHypothesis],
              reset(), failure -> str | None
Translator:   translate(TranslationRequest) -> TranslationCandidate (blocking; the driver
              runs it on a worker thread), or submit()/poll() for worker-backed adapters
Synthesizer:  capabilities, prepare(VoiceProfile) -> PreparedVoice,
              synthesize(unit, text, target_seconds) -> iterator of SynthesizedAudio
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.types import PCMFormat


@dataclass(frozen=True)
class SynthesisCapabilities:
    audio_output_streaming: bool
    incremental_text_input: bool
    continuation_across_units: bool
    # False for plumbing adapters that do not preserve the source speaker (a system voice).
    preserves_speaker_identity: bool


@dataclass(frozen=True)
class SynthesizedAudio:
    """One block of synthesized mono or interleaved PCM in the model's native format."""

    samples: np.ndarray
    format: PCMFormat

    @property
    def frames(self) -> int:
        return len(self.samples) // max(1, self.format.channel_count)


class AdapterError(RuntimeError):
    pass

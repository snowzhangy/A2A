"""RNNoise worker and delayed-bed integration, using only synthetic audio."""

import sys
import time
from pathlib import Path

import numpy as np
import pytest

from a2a_runtime import REPO_ROOT
from a2a_runtime.core.types import SourceSpan
from a2a_runtime.media.background_bed import BackgroundBed

PYTHON = REPO_ROOT / "runtime" / "separator" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
WORKER = REPO_ROOT / "runtime" / "a2a_runtime" / "workers" / "separator_worker.py"


def test_driver_restores_actor_into_held_bed(tmp_path):
    from types import SimpleNamespace

    from a2a_runtime.cli import parse
    from a2a_runtime.core import pipeline as p
    from a2a_runtime.driver import Driver
    from a2a_runtime.models.fixtures import DictionaryTranslator, NullRecognizer, ToneSynthesizer

    options = parse(["--input", "unused.wav", "--clock", "host", "--asr", "audio", "--mt", "hy-local",
                     "--tts", "clone", "--clone-fake", "--clone-mode", "xvector", "--background",
                     "--fluent", "--presentation-delay-ms", "4000", "--trace", str(tmp_path / "trace.jsonl")])
    driver = Driver(options, NullRecognizer(), DictionaryTranslator({}), ToneSynthesizer(),
                    separator=SimpleNamespace(latency_seconds=0.01))
    span = SourceSpan(0, 480)
    driver.voice_history = np.ones(480, dtype=np.float32) * 0.3
    driver.bed.push(np.zeros(480, dtype=np.float32))
    driver.execute([p.RestoreOriginalVoice(span)])
    restored = driver.bed.drain(4 * 48_000)
    assert np.allclose(restored, 0.3) and driver.stats["voice_restored_units"] == 1
    driver.trace.close()


def test_bed_delay_and_original_voice_replacement():
    bed = BackgroundBed(delay_frames=960, max_hold_frames=2_000)
    bed.push(np.ones(960, dtype=np.float32) * 0.2)
    assert len(bed.drain(960)) == 0
    assert bed.replace(SourceSpan(0, 480), np.ones(480, dtype=np.float32)) == 480
    out = bed.drain(1_920)
    assert len(out) == 960
    assert np.allclose(out[:480], 1.0) and np.allclose(out[480:], 0.2)
    assert bed.replace(SourceSpan(0, 480), np.ones(480, dtype=np.float32)) == 0


def test_late_restoration_puts_the_rest_of_the_line_at_its_own_source_time():
    # The stall rule restores a long line after its start has left the bed. The voice is the
    # whole line's; the part that goes back must be the part that belongs there, not its start.
    bed = BackgroundBed(delay_frames=0, max_hold_frames=4_000)
    bed.push(np.zeros(960, dtype=np.float32))
    assert len(bed.drain(300)) == 300
    voice = np.arange(960, dtype=np.float32) / 960
    assert bed.replace(SourceSpan(0, 960), voice) == 660
    assert np.allclose(bed.drain(960), voice[300:])
    # A span that starts inside the bed and a voice shorter than its span are still bounded.
    bed.push(np.zeros(960, dtype=np.float32))
    assert bed.replace(SourceSpan(1_200, 1_800), np.ones(300, dtype=np.float32)) == 300
    out = bed.drain(1_920)
    assert np.allclose(out[240:540], 1.0) and np.allclose(out[:240], 0) and np.allclose(out[540:], 0)


def test_whisper_path_feeds_separator_and_buffers_partial_frames(tmp_path):
    from a2a_runtime.cli import parse
    from a2a_runtime.driver import Driver
    from a2a_runtime.models.fixtures import DictionaryTranslator, NullRecognizer, ToneSynthesizer

    class Separator:
        latency_seconds = 0
        failure = None

        def __init__(self):
            self.inputs = []

        def separate(self, samples):
            self.inputs.append(samples.copy())

        def drain(self):
            if not self.inputs:
                return []
            data, self.inputs = self.inputs, []
            return [(block, np.zeros_like(block)) for block in data]

    options = parse(["--input", "unused.wav", "--clock", "host", "--asr", "whisper", "--background",
                     "--trace", str(tmp_path / "trace.jsonl")])
    separator = Separator()
    driver = Driver(options, NullRecognizer(), DictionaryTranslator({}), ToneSynthesizer(), separator=separator)
    try:
        driver.step(np.full(240, 0.1, dtype=np.float32), 240)
        assert len(driver.voice_history) == 0
        driver.step(np.full(240, 0.2, dtype=np.float32), 240)
        voice = driver.voice_history_slice(SourceSpan(0, 480))
        assert voice is not None and np.allclose(voice[:240], 0.1) and np.allclose(voice[240:], 0.2)
        assert not driver.background_failed
    finally:
        driver.trace.close()


@pytest.mark.skipif(not PYTHON.exists(), reason="separator worker environment not installed")
def test_rnnoise_worker_streams_aligned_voice_and_bed():
    from a2a_runtime.workers.separator import RNNoiseSeparator

    separator = RNNoiseSeparator([str(PYTHON), str(WORKER)])
    try:
        t = np.arange(1_920, dtype=np.float32) / 48_000
        separator.separate((0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        blocks = []
        for _ in range(100):
            blocks += separator.drain()
            if len(blocks) >= 4:
                break
            time.sleep(0.005)
        assert separator.failure is None
        assert len(blocks) == 4
        for voice, bed in blocks:
            assert voice.shape == bed.shape == (480,)
            assert np.isfinite(voice).all() and np.isfinite(bed).all()
            assert np.max(np.abs(voice)) <= 1 and np.max(np.abs(bed)) <= 1
        assert np.max(np.abs(blocks[0][0])) == 0
        separator.reset()
    finally:
        separator.shutdown()

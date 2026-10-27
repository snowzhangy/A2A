"""The media fixture must not overlay delayed and undelayed stereo channels."""
import importlib.util
from pathlib import Path
import shutil
import wave

import numpy as np
import pytest

from a2a_harness.audio import read_wav


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="media preparation requires ffmpeg")
def test_stereo_source_is_silent_until_lead_then_plays_once(tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts/live_source_test.py"
    spec = importlib.util.spec_from_file_location("live_source_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rate = 48_000
    source = tmp_path / "stereo.wav"
    # Both channels are nonzero immediately, so a delay of only one channel leaks.
    frames = np.tile(np.array([6_000, 12_000], dtype="<i2"), (4_800, 1))
    with wave.open(str(source), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(frames.tobytes())
    output = tmp_path / "prepared.wav"
    module.prepare_reference(source, output, seconds=0.1, lead=0.05, tail=0.05)
    pcm, actual_rate = read_wav(output)
    assert actual_rate == rate
    assert len(pcm) == 9_600
    assert np.all(pcm[:2_400] == 0)
    assert np.all(pcm[2_400:7_200] > 0.1)
    assert np.all(pcm[7_200:] == 0)

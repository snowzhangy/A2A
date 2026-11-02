"""Media DSP: resampler accuracy and streaming invariance, WAV round trips, signal helpers."""

from __future__ import annotations

import numpy as np

from a2a_runtime.media import signal, wav
from a2a_runtime.media.resampler import StreamingResampler


def test_identity_rate_passes_through():
    r = StreamingResampler(48_000, 48_000)
    x = np.arange(10, dtype=np.float32)
    assert np.array_equal(r.process(x), x) and len(r.flush()) == 0


def test_streaming_matches_whole_buffer_and_preserves_count():
    x = signal.fidelity_probe(48_000, 1.0)
    whole = StreamingResampler.resample(x, 48_000, 16_000)
    r = StreamingResampler(48_000, 16_000)
    parts = [r.process(x[i:i + 960]) for i in range(0, len(x), 960)] + [r.flush()]
    streamed = np.concatenate(parts)
    assert len(whole) == len(streamed) == 16_000
    assert np.max(np.abs(whole - streamed)) < 1e-6


def test_downsampled_tone_keeps_frequency_and_level():
    t = np.arange(48_000) / 48_000
    x = (0.5 * np.sin(2 * np.pi * 1_000 * t)).astype(np.float32)
    y = StreamingResampler.resample(x, 48_000, 16_000)
    ref = (0.5 * np.sin(2 * np.pi * 1_000 * np.arange(len(y)) / 16_000)).astype(np.float32)
    lag, corr = signal.best_lag(ref, y, range(2_000, 6_000), max_lag=64)
    assert corr > 0.9999
    assert abs(signal.dbfs(signal.rms(y[2_000:14_000])) - signal.dbfs(signal.rms(x))) < 0.05


def test_upsampling_rejects_images_and_aliases():
    # 24 kHz TTS to the 48 kHz bus: a 10 kHz tone must not create a 14 kHz image.
    t = np.arange(24_000) / 24_000
    x = (0.5 * np.sin(2 * np.pi * 10_000 * t)).astype(np.float32)
    y = StreamingResampler.resample(x, 24_000, 48_000)[2_000:46_000]
    spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1 / 48_000)
    tone = spectrum[np.argmin(np.abs(freqs - 10_000))]
    image = spectrum[np.argmin(np.abs(freqs - 14_000))]
    assert 20 * np.log10(image / tone) < -60


def test_wav_round_trip_float_and_pcm16(tmp_path):
    x = signal.fidelity_probe(24_000, 0.5)
    path = tmp_path / "x.wav"
    wav.write(wav.WAVAudio(24_000, 1, x), path)
    back = wav.read(path)
    assert back.sample_rate == 24_000 and back.channels == 1 and np.array_equal(back.samples, x)
    pcm16 = (np.clip(x, -1, 1) * 32767).astype("<i2")
    import struct
    data = (b"RIFF" + struct.pack("<I", 36 + pcm16.nbytes) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24_000, 48_000, 2, 16)
            + b"data" + struct.pack("<I", pcm16.nbytes) + pcm16.tobytes())
    back16 = wav.decode(data)
    assert np.max(np.abs(back16.samples - x)) < 1e-4


def test_stream_writer_patches_header(tmp_path):
    path = tmp_path / "s.wav"
    w = wav.WAVStreamWriter(path, 48_000, 2)
    w.append(np.ones(960, np.float32))
    w.append(np.zeros(0, np.float32))
    w.close()
    back = wav.read(path)
    assert back.channels == 2 and back.frame_count == 480 and np.all(back.mono() == 1)


def test_best_lag_finds_offset_in_long_signals():
    ref = signal.fidelity_probe(48_000, 3.0)
    sig = np.concatenate([np.zeros(12_345, np.float32), ref])
    lag, corr = signal.best_lag(ref, sig, range(1_000, 49_000), max_lag=48_000)
    assert lag == 12_345 and corr > 0.999999

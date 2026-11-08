"""System-voice plumbing synthesizer: casting (ported from VoiceCastingTests in
SpeakerTrackingTests.swift), pacing, and the real Windows voices end to end."""

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from a2a_runtime import REPO_ROOT
from a2a_runtime.core.types import ReferenceQuality, SpeakerID, VoiceProfile
from a2a_runtime.media import wav
from a2a_runtime.models.system_voice import InstalledVoice, SystemVoiceSynthesizer, VoiceCasting

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows speech")


def voices():
    return [InstalledVoice("MSTTS_enUS_ZiraM", "Microsoft Zira", "en-US", "female"),
            InstalledVoice("MSTTS_enUS_DavidM", "Microsoft David", "en-US", "male"),
            InstalledVoice("MSTTS_enGB_HazelM", "Microsoft Hazel", "en-GB", "female"),
            InstalledVoice("MSTTS_enUS_MarkM", "Microsoft Mark", "en-US", "male"),
            InstalledVoice("MSTTS_enUS_Unknown", "Microsoft Ana", "en-US", None),
            InstalledVoice("MSTTS_zhCN_HuihuiM", "Microsoft Huihui", "zh-CN", "female"),
            InstalledVoice("MSTTS_zhHK_TracyM", "Microsoft Tracy", "zh-HK", "female"),
            InstalledVoice("MSTTS_zhTW_HanHanM", "Microsoft HanHan", "zh-TW", "female"),
            InstalledVoice("MSTTS_zhCN_KangkangM", "Microsoft Kangkang", "zh-CN", "male")]


def test_candidates_keep_the_language_and_exclude_cantonese_taiwanese_and_unknown_gender():
    english = VoiceCasting(VoiceCasting.candidates_from(voices(), "en-US")).candidates
    assert [c.name for c in english] == ["David", "Mark", "Zira", "Hazel"]  # exact locale first
    mandarin = VoiceCasting(VoiceCasting.candidates_from(voices(), "zh-CN")).candidates
    assert sorted(c.name for c in mandarin) == ["Huihui", "Kangkang"]


def test_speakers_get_distinct_voices_matched_to_their_pitch():
    casting = VoiceCasting(VoiceCasting.candidates_from(voices(), "en-US"))
    low, high, high2 = casting.cast(120), casting.cast(230), casting.cast(250)
    assert casting.roles[low].identifier.endswith("DavidM")
    assert casting.roles[high].identifier.endswith("ZiraM")
    assert casting.roles[high2].identifier.endswith("HazelM")
    assert len({r.identifier for r in casting.roles}) == 3
    # The female pool is exhausted: the next female speaker shares a voice at another pitch.
    high3 = casting.cast(260)
    assert casting.roles[high3].pitch_multiplier != 1


def test_ambiguous_pitch_goes_to_the_less_cast_gender():
    casting = VoiceCasting(VoiceCasting.candidates_from(voices(), "en-US"))
    casting.cast(220)
    assert casting.roles[casting.cast(185)].identifier.endswith("DavidM")


def test_no_candidates_casts_nothing():
    assert VoiceCasting([]).cast(120) is None


def test_speed_fits_the_source_within_the_cap_and_never_slows_down():
    speed = SystemVoiceSynthesizer.speed
    assert speed("Okay.", 2.0, 0.18, 0.055) == 1.0
    assert speed("x" * 40, 1.8, 0.18, 0.055) == pytest.approx((40 * 0.055 + 0.18) / 1.95)
    assert speed("x" * 200, 1.0, 0.18, 0.055) == SystemVoiceSynthesizer.MAX_SPEED


def profile(speaker: str, pitch: float | None) -> VoiceProfile:
    return VoiceProfile(SpeakerID(speaker), 1, np.zeros(0, np.float32), "", 48_000, 1,
                        ReferenceQuality(0, float("nan"), 0, False), pitch_hz=pitch)


@WINDOWS_ONLY
def test_windows_voices_render_cast_speakers_and_pace():
    synth = SystemVoiceSynthesizer("en-US", paced=True)
    try:
        if len(synth.casting.candidates) < 2:
            pytest.skip("needs two installed en-US voices")
        low, high = synth.prepare(profile("S1", 120)), synth.prepare(profile("S2", 230))
        assert synth.voice_name(low) != synth.voice_name(high)
        assert synth.prepare(profile("S1", 120)).handle == low.handle  # cast once per speaker
        text = "We do not agree with this proposal."
        natural = list(synth.synthesize(1, text, low))
        assert natural and all(c.frames == c.format.sample_rate // 10 for c in natural[:-1])
        natural_s = sum(c.frames for c in natural) / natural[0].format.sample_rate
        paced = list(synth.synthesize(2, text, low, target_seconds=natural_s / 1.6))
        paced_s = sum(c.frames for c in paced) / paced[0].format.sample_rate
        # Faster natively, capped at 1.3x, with trailing silence cut to the post-roll.
        assert natural_s / 1.45 < paced_s < natural_s / 1.1
        audio = np.concatenate([c.samples for c in paced])
        trailing = len(audio) - 1 - np.flatnonzero(np.abs(audio) >= synth.SILENCE_LEVEL)[-1]
        assert trailing <= int(0.18 * paced[0].format.sample_rate)
    finally:
        synth.close()


@WINDOWS_ONLY
def test_pipeline_translates_a_file_with_the_system_voice(tmp_path):
    rate = 48_000
    t = np.arange(rate * 3) / rate
    source = (0.2 * np.sin(2 * np.pi * 180 * t) * (t > 0.3) * (t < 1.8)).astype(np.float32)
    wav.write(wav.WAVAudio(rate, 1, source), tmp_path / "source.wav")
    script = tmp_path / "asr.json"
    script.write_text(json.dumps({"hypotheses": [
        {"at_sample": int(1.9 * 16_000), "utt": 1, "text": "我们下周开会。", "final": True}]}, ensure_ascii=False),
        encoding="utf-8")
    table = tmp_path / "mt.json"
    table.write_text(json.dumps({"我们下周开会。": "We will meet next week."}, ensure_ascii=False), encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "a2a_runtime.cli", "--input", str(tmp_path / "source.wav"),
                           "--asr", "scripted", "--asr-script", str(script), "--mt-dictionary", str(table),
                           "--tts", "system", "--clock", "host", "--trace", str(tmp_path / "trace.jsonl"),
                           "--out", str(tmp_path / "out.wav"), "--tail-seconds", "4"],
                          cwd=REPO_ROOT / "runtime", capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ, "PYTHONUTF8": "1"})
    assert done.returncode == 0, done.stderr
    assert "system voice ready" in done.stderr
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["ev"] for e in events]
    assert "tts.first_chunk" in kinds and "play.start" in kinds and "error" not in kinds
    out = wav.read(tmp_path / "out.wav").mono()
    assert np.max(np.abs(out)) > 0.05  # the voice was heard
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["tts_preserves_speaker_identity"] is False

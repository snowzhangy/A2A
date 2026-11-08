"""Mandarin demo clip and video for the live-translation plumbing test.

Speaks a short meeting script with zh_CN system voices (`say` on macOS, the SAPI OneCore
voices on Windows through WinRT), spaces the sentences so English playback can keep up, and writes:

    clip.wav        48 kHz mono float32 source audio
    video.mp4       the same audio over an ffmpeg test pattern (for a real player app)
    expected.json   per-sentence source text, onset/offset, and English keywords

The voice is synthetic, so the clip may be used freely in tests. It proves plumbing and
timing only: system TTS is not a meeting corpus.

    uv run --project harness python -m a2a_harness.zh_demo --out .local/fixtures/zh-demo

`--voices A,B` alternates voices by sentence for a two-speaker dialogue (Windows default:
Huihui and Kangkang, a female and a male voice).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from . import CANONICAL_RATE
from .audio import read_wav, write_wav

# (Mandarin sentence, English keyword groups). Each group is a set of alternatives; a
# translation passes a sentence when every group has at least one match.
SCRIPT: list[tuple[str, list[list[str]]]] = [
    ("大家好，今天我们讨论下个季度的计划。", [["discuss", "talk"], ["quarter"], ["plan"]]),
    ("我们下周开会。", [["meeting", "meet"], ["next week"]]),
    ("这个方案我们不同意。", [["disagree", "not agree", "don't agree", "do not agree"], ["plan", "proposal", "scheme"]]),
    ("预算需要增加百分之二十。", [["budget"], ["20", "twenty"], ["percent", "%"]]),
    ("请在星期五之前把报告发给我。", [["report"], ["friday"]]),
    ("谢谢大家，会议到此结束。", [["thank"], ["meeting"]]),
]


def speak(text: str, voice: str, rate_wpm: int | None, workdir: Path) -> np.ndarray:
    out = workdir / "s.wav"
    if sys.platform == "win32":
        speak_windows([(text, voice, out)], rate_wpm, workdir)
    else:
        cmd = ["say", "-v", voice, "--file-format=WAVE", f"--data-format=LEF32@{CANONICAL_RATE}", "-o", str(out)]
        if rate_wpm:
            cmd += ["-r", str(rate_wpm)]
        subprocess.run(cmd + [text], check=True)
    samples, rate = read_wav(out)
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if rate != CANONICAL_RATE:
        if CANONICAL_RATE % rate:
            raise RuntimeError(f"system voice produced {rate} Hz, not a divisor of {CANONICAL_RATE}")
        samples = _upsample(samples.astype(np.float64), CANONICAL_RATE // rate)
    return samples.astype(np.float32)


def _upsample(x: np.ndarray, factor: int) -> np.ndarray:
    """Band-limited integer upsampling by spectral zero padding (fixtures only)."""
    pad = 1024
    y = np.fft.irfft(np.fft.rfft(np.pad(x, pad)), (len(x) + 2 * pad) * factor) * factor
    return y[pad * factor:(pad + len(x)) * factor]


# WinRT Windows.Media.SpeechSynthesis through PowerShell: the native API of the OneCore voices.
# SAPI COM accepts OneCore tokens but silently renders Kangkang and Yaoyao with one fallback
# voice (byte-identical output), so it cannot make a two-speaker dialogue.
_WINRT_SCRIPT = r"""
$jobs = Get-Content -Raw -Encoding UTF8 $args[0] | ConvertFrom-Json
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $asTask.MakeGenericMethod($type).Invoke($null, @($op)).GetAwaiter().GetResult() }
$voices = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices
foreach ($job in $jobs) {
  $voice = $voices | Where-Object { $_.DisplayName -like ('*' + $job.voice + '*') } | Select-Object -First 1
  if (-not $voice) { [Console]::Error.WriteLine('voice not installed: ' + $job.voice); exit 3 }
  $synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
  $synth.Voice = $voice
  $synth.Options.SpeakingRate = [double]$job.rate
  $stream = Await ($synth.SynthesizeTextToStreamAsync($job.text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
  $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
  $null = Await ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
  $bytes = New-Object byte[] ([int]$stream.Size)
  $reader.ReadBytes($bytes)
  [IO.File]::WriteAllBytes($job.path, $bytes)
}
"""


def speak_windows(jobs: list[tuple[str, str, Path]], rate_wpm: int | None, workdir: Path) -> None:
    # SpeakingRate is a multiplier of the voice's default pace (about 180 wpm), 0.5-6.
    rate = 1.0 if not rate_wpm else max(0.5, min(6.0, rate_wpm / 180))
    spec = workdir / "jobs.json"
    spec.write_text(json.dumps([{"text": t, "voice": v, "path": str(o), "rate": rate} for t, v, o in jobs],
                               ensure_ascii=False), encoding="utf-8")
    script = workdir / "speak.ps1"
    script.write_text(_WINRT_SCRIPT, encoding="utf-8-sig")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(spec)],
                   check=True, capture_output=True)


def build(out: Path, voice: str, gap_s: float, lead_s: float, rate_wpm: int | None, video: bool,
          ffmpeg: str | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    pieces = [np.zeros(int(lead_s * CANONICAL_RATE), dtype=np.float32)]
    cursor = len(pieces[0])
    sentences = []
    with tempfile.TemporaryDirectory() as tmp:
        voices = [v.strip() for v in voice.split(",") if v.strip()]
        for i, (text, keywords) in enumerate(SCRIPT):
            speaker = voices[i % len(voices)]
            audio = speak(text, speaker, rate_wpm, Path(tmp)).astype(np.float32)
            sentences.append({"index": i, "text": text, "keywords": keywords, "voice": speaker,
                              "onset_s": cursor / CANONICAL_RATE,
                              "offset_s": (cursor + len(audio)) / CANONICAL_RATE})
            gap = np.zeros(int(gap_s * CANONICAL_RATE), dtype=np.float32)
            pieces += [audio, gap]
            cursor += len(audio) + len(gap)
    clip = np.concatenate(pieces)
    write_wav(out / "clip.wav", clip, CANONICAL_RATE)
    meta = {"voice": voice, "rate": CANONICAL_RATE, "duration_s": len(clip) / CANONICAL_RATE,
            "gap_s": gap_s, "synthetic": True, "sentences": sentences}
    if video:
        ffmpeg = ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found; pass --ffmpeg PATH or --no-video")
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size=960x540:rate=30:duration={meta['duration_s']:.3f}",
                        "-i", str(out / "clip.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out / "video.mp4")], check=True)
        meta["video"] = "video.mp4"
    (out / "expected.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--voice", "--voices", dest="voice", default="Huihui,Kangkang" if sys.platform == "win32" else "Tingting",
                    help="voice name, or comma-separated names alternating by sentence")
    ap.add_argument("--gap", type=float, default=3.0, help="silence after each sentence, seconds")
    ap.add_argument("--lead", type=float, default=1.5, help="silence before the first sentence, seconds")
    ap.add_argument("--rate-wpm", type=int, default=None)
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg executable (default: on PATH)")
    args = ap.parse_args()
    meta = build(args.out, args.voice, args.gap, args.lead, args.rate_wpm, not args.no_video, args.ffmpeg)
    print(json.dumps({"out": str(args.out), "duration_s": round(meta["duration_s"], 2),
                      "sentences": len(meta["sentences"]), "video": meta.get("video")}))


if __name__ == "__main__":
    main()

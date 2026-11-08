"""System-voice plumbing synthesizer for Windows: the counterpart of SystemVoiceSynthesizer
in A2AModels/AppleAdapters.swift (ADR-0007).

It does not preserve the source speaker, so it can never satisfy an identity gate. Speech
comes from WinRT Windows.Media.SpeechSynthesis, the native API of the OneCore voices (SAPI
COM renders some OneCore voices identically), through one resident PowerShell process so
nothing is installed. With a target duration (fluent playout) the voice speaks faster
natively (SpeakingRate) instead of being time-stretched, and its trailing silence is cut to
a pause that fits the unit's ending. A profile that carries the speaker's pitch is cast its
own installed voice (`VoiceCasting`), so each speaker in a dialogue sounds different.
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import threading
import wave
from dataclasses import dataclass

import numpy as np

from ..core.types import PCMFormat, PreparedVoice, VoiceProfile
from .base import AdapterError, SynthesisCapabilities, SynthesizedAudio

# Requests are JSON lines on stdin; each reply is one line, "ok <base64 WAV>" or "err <message>".
_WORKER = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) { $asTask.MakeGenericMethod($type).Invoke($null, @($op)).GetAwaiter().GetResult() }
$voices = @{}
$list = foreach ($v in [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices) {
    $voices[$v.Id] = $v
    @{ id = $v.Id; name = $v.DisplayName; language = $v.Language; gender = "$($v.Gender)".ToLower() }
}
[Console]::Out.WriteLine('voices ' + (ConvertTo-Json -Compress -InputObject @($list)))
[Console]::Out.Flush()
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$synth.Options.AppendedSilence = [Windows.Media.SpeechSynthesis.SpeechAppendedSilence]::Min
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try {
        $r = ConvertFrom-Json $line
        $synth.Voice = $voices[$r.voice]
        $synth.Options.SpeakingRate = [double]$r.rate
        $synth.Options.AudioPitch = [double]$r.pitch
        $stream = Await ($synth.SynthesizeTextToStreamAsync($r.text)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
        $reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
        $null = Await ($reader.LoadAsync([uint32]$stream.Size)) ([uint32])
        $bytes = New-Object byte[] ([int]$stream.Size)
        $reader.ReadBytes($bytes)
        $stream.Dispose()
        [Console]::Out.WriteLine('ok ' + [Convert]::ToBase64String($bytes))
    } catch {
        [Console]::Out.WriteLine('err ' + ($_.Exception.Message -replace '\s+', ' '))
    }
    [Console]::Out.Flush()
}
"""


@dataclass(frozen=True)
class InstalledVoice:
    identifier: str
    name: str
    language: str
    gender: str | None  # "female", "male", or None when the system does not say


@dataclass(frozen=True)
class Candidate:
    identifier: str
    name: str
    language: str
    gender: str
    rank: int  # lower is better: exact locale first


@dataclass(frozen=True)
class Role:
    identifier: str
    pitch_multiplier: float  # distinguishes speakers who share a voice once a pool runs out


class VoiceCasting:
    """Each tracked speaker gets a distinct installed voice of a plausible gender for their
    pitch: VoiceCasting.swift. Pure; the synthesizer lists the installed voices."""

    SPLIT_HZ = 180.0
    AMBIGUOUS_HZ = (165.0, 195.0)
    SHARED_PITCH = (0.88, 1.12, 0.8, 1.2)

    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = sorted(candidates, key=lambda c: (c.rank, c.name, c.identifier))
        self.roles: list[Role] = []
        self._genders: list[str] = []

    @staticmethod
    def candidates_from(voices: list[InstalledVoice], locale: str) -> list[Candidate]:
        """Voices for a target locale such as "en-US" or "zh-CN". Mandarin excludes zh-HK
        and zh-TW (Cantonese and Taiwanese voices)."""
        language = locale[:2].lower()
        out = []
        for v in voices:
            lang = v.language.replace("_", "-")
            if not lang.lower().startswith(language) or v.gender not in ("female", "male"):
                continue
            if language == "zh" and lang.lower().endswith(("hk", "tw")):
                continue
            base = v.name.split(" ")[-1] if v.name.startswith("Microsoft ") else v.name.split(" ")[0]
            out.append(Candidate(v.identifier, base, lang, v.gender, 0 if lang == locale else 1))
        return out

    def cast(self, pitch_hz: float) -> int | None:
        """Casts the next speaker; returns the role index (stable for the session)."""
        if not self.candidates:
            return None
        males = self._genders.count("male")
        females = len(self._genders) - males
        gender = "male" if pitch_hz < self.SPLIT_HZ else "female"
        if self.AMBIGUOUS_HZ[0] <= pitch_hz <= self.AMBIGUOUS_HZ[1] and males != females:
            gender = "male" if males < females else "female"
        pool = [c for c in self.candidates if c.gender == gender] or self.candidates
        taken = [r.identifier for r in self.roles]
        fresh = next((c for c in pool if c.identifier not in taken), None)
        if fresh is not None:
            role = Role(fresh.identifier, 1.0)
        else:
            # Every voice of that gender is cast: reuse the least-used one at another pitch.
            counts = [taken.count(c.identifier) for c in pool]
            least = min(counts)
            role = Role(pool[counts.index(least)].identifier, self.SHARED_PITCH[(least - 1) % len(self.SHARED_PITCH)])
        self.roles.append(role)
        self._genders.append(gender)
        return len(self.roles) - 1


class SystemVoiceSynthesizer:
    name = "system"
    # Beyond this, faster speech costs more naturalness than the latency it saves; fluent
    # catch-up skipping handles the rest of the debt (as on macOS).
    MAX_SPEED = 1.3
    # Below this a 16-bit sample is silence (the synthesizer pads with exact zeros).
    SILENCE_LEVEL = 1e-4
    capabilities = SynthesisCapabilities(audio_output_streaming=True, incremental_text_input=False,
                                         continuation_across_units=False, preserves_speaker_identity=False)

    def __init__(self, language: str = "en-US", paced: bool = False, timeout: float = 30.0) -> None:
        if sys.platform != "win32":
            raise SystemExit("a2a-pipeline (portable): --tts system uses Windows speech; on macOS use apple/ "
                             "a2a-pipeline --tts system")
        self.language = language
        self.paced = paced
        self.timeout = timeout
        mandarin = language.lower().startswith("zh")
        self.max_seconds_per_character = 0.6 if mandarin else 0.2
        # Initial estimates only; completed paced units learn each voice's actual cadence.
        self.initial_seconds_per_character = 0.22 if mandarin else 0.055
        self.warm_up_text = "好的，我们开始吧。" if mandarin else "Okay, let's begin."
        self.seconds_per_character: dict[str, float] = {}
        self._lock = threading.Lock()
        self._state = threading.Lock()
        script = base64.b64encode(_WORKER.encode("utf-16-le")).decode("ascii")
        self._proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                       "-EncodedCommand", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        header = self._read_line()
        if not header.startswith("voices "):
            self.close()
            raise AdapterError(f"Windows speech did not start: {header[:200]}")
        installed = [InstalledVoice(v["id"], v["name"], v["language"], v["gender"] if v["gender"] in ("female", "male")
                                    else None) for v in json.loads(header[7:])]
        self.installed = installed
        lang = language.lower()
        default = next((v for v in installed if v.language.lower() == lang), None) or next(
            (v for v in installed if v.language.lower()[:2] == lang[:2]), None)
        if default is None:
            self.close()
            raise SystemExit(f"a2a-pipeline: no Windows voice for {language} is installed (Settings > Time & language "
                             "> Speech > Manage voices)")
        self.default_voice = default
        self.casting = VoiceCasting(VoiceCasting.candidates_from(installed, language))
        self.roles: dict[str, int] = {}  # speaker -> role index; a PreparedVoice handle is the index plus one
        self._warmed: set[str] = set()
        self._warm_up(self.default_voice.identifier, 1.0)

    def _read_line(self) -> str:
        result: list[bytes] = []
        reader = threading.Thread(target=lambda: result.append(self._proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(self.timeout)
        if not result or not result[0]:
            raise AdapterError("Windows speech stopped responding" if reader.is_alive() else "Windows speech exited")
        return result[0].decode("utf-8").rstrip("\r\n")

    def _render(self, identifier: str, text: str, rate: float, pitch: float) -> tuple[np.ndarray, int]:
        request = json.dumps({"voice": identifier, "text": text, "rate": round(rate, 4), "pitch": round(pitch, 4)},
                             ensure_ascii=False)
        with self._lock:
            if self._proc.poll() is not None:
                raise AdapterError("Windows speech exited")
            self._proc.stdin.write(request.encode("utf-8") + b"\n")
            self._proc.stdin.flush()
            reply = self._read_line()
        if not reply.startswith("ok "):
            raise AdapterError(f"Windows speech failed: {reply[4:] if reply.startswith('err ') else reply[:200]}")
        with wave.open(io.BytesIO(base64.b64decode(reply[3:])), "rb") as w:
            if w.getsampwidth() != 2:
                raise AdapterError(f"unexpected {8 * w.getsampwidth()}-bit speech")
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32_768
            rate_hz, channels = w.getframerate(), w.getnchannels()
        return (pcm.reshape(-1, channels).mean(axis=1) if channels > 1 else pcm), rate_hz

    def _warm_up(self, identifier: str, pitch: float) -> None:
        # The first utterance of a voice loads it; later ones start faster.
        if identifier in self._warmed:
            return
        self._warmed.add(identifier)
        try:
            self._render(identifier, self.warm_up_text, 1.0, pitch)
        except AdapterError:
            pass

    def prepare(self, profile: VoiceProfile) -> PreparedVoice:
        """With the speaker's pitch, casts (once per speaker) and warms that speaker's voice."""
        handle = 0
        if profile.pitch_hz is not None:
            speaker = profile.speaker_id.raw_value
            with self._state:
                role = self.roles.get(speaker)
                if role is None:
                    role = self.casting.cast(profile.pitch_hz)
                    if role is not None:
                        self.roles[speaker] = role
            if role is not None:
                handle = role + 1
        voice = PreparedVoice(profile.speaker_id, profile.version, handle)
        if handle:
            identifier, pitch = self.role(voice)
            self._warm_up(identifier, pitch)
        return voice

    def role(self, voice: PreparedVoice) -> tuple[str, float]:
        """Voice identifier and pitch for a prepared voice; handle 0 is the language's default."""
        if 0 < voice.handle <= len(self.casting.roles):
            r = self.casting.roles[voice.handle - 1]
            return r.identifier, r.pitch_multiplier
        return self.default_voice.identifier, 1.0

    def describe(self) -> str:
        """The default voice and how many installed voices a dialogue can be cast from."""
        return (f"{self.default_voice.name} ({self.default_voice.language}), "
                f"{len(self.casting.candidates)} castable voices")

    def voice_name(self, voice: PreparedVoice) -> str | None:
        identifier, _ = self.role(voice)
        return next((v.name for v in self.installed if v.identifier == identifier), None)

    @classmethod
    def speed(cls, text: str, target_seconds: float, post_roll_seconds: float, seconds_per_character: float) -> float:
        """Speed-up that fits the text into its source duration, within MAX_SPEED. Small
        overruns are tolerated so short acknowledgements keep their natural cadence."""
        natural = len(text) * seconds_per_character + post_roll_seconds
        return min(cls.MAX_SPEED, max(1.0, natural / (max(0.5, target_seconds) + 0.15)))

    def synthesize(self, unit: int, text: str, voice: PreparedVoice, target_seconds: float | None = None):
        identifier, pitch = self.role(voice)
        # A sentence keeps a sentence-length pause; a clause flows into the next one.
        post_roll = 0.18 if text[-1:] in ".!?。！？" else 0.09
        paced = self.paced and target_seconds is not None and np.isfinite(target_seconds) and target_seconds > 0
        initial = self.initial_seconds_per_character
        speed = 1.0
        if paced:
            speed = self.speed(text, target_seconds, post_roll, self.seconds_per_character.get(identifier, initial))
        pcm, rate = self._render(identifier, text, speed, pitch)
        voiced = np.flatnonzero(np.abs(pcm) >= self.SILENCE_LEVEL)
        if paced and len(voiced):
            # Learn this voice's unhurried cadence from the voiced span.
            observed = (voiced[-1] + 1 - voiced[0]) / rate * speed / max(1, len(text))
            observed = min(self.max_seconds_per_character, max(0.02, observed))
            self.seconds_per_character[identifier] = 0.7 * self.seconds_per_character.get(identifier, initial) \
                + 0.3 * observed
            pcm = pcm[:min(len(pcm), voiced[-1] + 1 + int(post_roll * rate))]
        fmt = PCMFormat(rate, 1)
        frames = max(1, rate // 10)
        for offset in range(0, len(pcm), frames):
            yield SynthesizedAudio(np.ascontiguousarray(pcm[offset:offset + frames]), fmt)

    def close(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()

    def __del__(self) -> None:
        self.close()

"""a2a-pipeline (portable): WAV or paced stdin in → translated WAV + trace out, or a live
Windows app → translated playback. Flags match apple/Tools/a2a-pipeline where they apply.

  --input clip.wav | --stdin [--rate 48000]       source audio (mono; stdin is f32le)
  --tap-exe NAME | --tap-pid N [--seconds S]      live (Windows): capture the app's process
                                                  tree; translated-only playback ducks it
  --output-device DEV                             render endpoint (name substring or ID)
  --duck-db D                                     source level while tapped (default -60)
  --asr scripted|whisper|audio [--asr-script x.json] [--asr-model ID]
  --mt dictionary|llm|hy|hy-local [--hy-server URL] [--hy-worker EXE --hy-model GGUF]
  --no-hy-stream                                  close spans before local translation instead of streaming
  --tts tone|system|clone [--clone-model omnivoice|qwen|chatterbox] [--clone-backend torch|mlx]
  --vad energy  --device auto|cuda|mps|cpu
  --asr-device/--mt-device/--tts-device DEVICE    override the device per stage
  --presentation-delay-ms D                       with --fluent: each phrase starts at its source
                                                  start + D and the original bus is delayed by D
  --no-turn-fill                                  with a picture delay: never slow a short line
  --quality-first | --adaptive-prebuffer          complete phrases / measured streaming reserve
  --max-unit-seconds S, --speaker-carry[-pause|-piece] S, --no-shorten, --no-filler-silence
  --glossary terms.json, --pronounce words.json   as in the Swift driver
  Everything else (--trace, --out, --stems, --fluent, --speakers, --translation-only,
  --playback, --control-stdin, --events-stdout, health thresholds) as in the Swift driver.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import REPO_ROOT
from .core.direction import TranslationDirection
from .core.pipeline import PlaybackMode
from .driver import Driver, PlaybackControls, Source, log


def fail(message: str) -> None:
    sys.stderr.write(f"a2a-pipeline: {message}\n")
    raise SystemExit(2)


CLONE_MODELS = ("qwen", "omnivoice", "chatterbox")
# audio8 is an Apple/MLX model in the shared manifest; naming it here keeps this message
# aligned with the Swift tool that the harness also drives.
CLONE_MODELS_LISTED = ("qwen", "audio8", "omnivoice", "chatterbox")


def clone_model(value: str) -> str:
    """argparse type: reject an unknown voice model with the tool's standard usage error."""
    if value not in CLONE_MODELS_LISTED:
        raise argparse.ArgumentTypeError("--clone-model is qwen, audio8, omnivoice, or chatterbox")
    if value not in CLONE_MODELS:
        raise argparse.ArgumentTypeError("audio8 needs Apple silicon and MLX; use qwen, omnivoice, or chatterbox")
    return value


def presentation_delay(value: str) -> int:
    """argparse type: the Swift driver's bound and message."""
    try:
        ms = int(value)
    except ValueError:
        ms = -1
    if not 0 <= ms <= 30_000:
        raise argparse.ArgumentTypeError("--presentation-delay-ms takes 0–30000")
    return ms


def capture_gap(value: str) -> tuple[float, float]:
    try:
        at, seconds = (float(x) for x in value.split(":"))
    except ValueError:
        at, seconds = -1.0, 0.0
    if not (at >= 0 and 0 < seconds <= 600):
        raise argparse.ArgumentTypeError("--simulate-capture-gap takes AT:SECONDS")
    return at, seconds


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="a2a-pipeline", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    a = p.add_argument
    a("--input"), a("--stdin", action="store_true"), a("--rate", type=int, default=48_000)
    a("--clock"), a("--pace")
    a("--asr", default="scripted"), a("--asr-script"), a("--asr-model", default="faster-whisper-large-v3")
    a("--mt", default="dictionary"), a("--mt-dictionary"), a("--mt-model", default="qwen3-4b-instruct-2507")
    a("--hy-server", default="http://127.0.0.1:8082"), a("--hy-worker"), a("--hy-model")
    a("--no-hy-stream", dest="hy_stream", action="store_false")
    a("--tts", default="tone")
    a("--direction", default="zh-en")
    a("--profile-ready-ms", type=int, default=0)
    a("--clone-mode", default="icl")
    a("--clone-delivery", choices=("pinned", "line"), default="pinned")
    a("--clone-model", type=clone_model, default=None)
    a("--clone-backend", default=None)
    a("--voice-memory"), a("--voice-ref"), a("--voice-ref-text")
    a("--clone-python"), a("--clone-worker"), a("--clone-fake", action="store_true")
    a("--reference-transcriber")
    a("--dual-clone", action="store_true")
    a("--trace", default="trace.jsonl"), a("--out"), a("--stems")
    a("--session", default=uuid.uuid4().hex[:8])
    a("--research-text", action="store_true")
    a("--tail-seconds", type=float, default=15.0)
    a("--vad-threshold-db", type=float, default=-40.0)
    a("--vad", default="energy")
    a("--device", default="auto")
    a("--asr-device"), a("--mt-device"), a("--tts-device")
    a("--tap-bundle"), a("--tap-pid", type=int), a("--tap-exe"), a("--tap-global", action="store_true")
    a("--output-device"), a("--duck-db", type=float, default=-60.0)
    a("--seconds", type=float)
    a("--source-out"), a("--translated-out"), a("--rendered-out")
    a("--max-debt-seconds", type=float), a("--max-source-lag-seconds", type=float), a("--max-unit-keys", type=int)
    a("--max-unit-seconds", type=float), a("--no-word-cuts", dest="word_cuts", action="store_false")
    a("--speaker-carry", type=float), a("--speaker-carry-pause", type=float), a("--speaker-carry-piece", type=float)
    a("--no-shorten", dest="shorten", action="store_false")
    a("--no-filler-silence", dest="filler_silence", action="store_false")
    a("--glossary"), a("--pronounce")
    a("--captions-only", action="store_true"), a("--translation-only", action="store_true")
    a("--events-stdout", action="store_true")
    a("--playback", default="translated")
    a("--control-stdin", action="store_true")
    a("--fluent", action="store_true")
    a("--adaptive-prebuffer", action="store_true"), a("--quality-first", action="store_true")
    a("--presentation-delay-ms", type=presentation_delay, default=0)
    a("--no-turn-fill", dest="turn_fill", action="store_false")
    a("--no-sentence-units", dest="sentence_units", action="store_false")
    a("--no-passthrough", dest="passthrough", action="store_false")
    a("--no-reference-isolation", dest="reference_isolation", action="store_false")
    a("--lid-model", default="faster-whisper-large-v3")
    a("--background", action="store_true"), a("--background-out")
    a("--simulate-capture-gap", type=capture_gap, dest="capture_gap")
    a("--speakers", default="single")
    a("--parent-pid", type=int)
    o = p.parse_args(argv)
    validate(o)
    return o


def validate(o: argparse.Namespace) -> None:
    try:
        o.direction = TranslationDirection(o.direction)
    except ValueError:
        fail("--direction is zh-en or en-zh")
    # The same messages as the Swift driver: harness tests assert on them.
    if o.playback not in ("original", "translated"):
        fail("--playback is original or translated")
    o.playback = PlaybackMode(o.playback)
    if o.clone_mode not in ("icl", "xvector"):
        fail("--clone-mode is icl or xvector")
    if o.clone_backend is not None and o.clone_backend not in ("torch", "mlx"):
        fail("--clone-backend is torch or mlx")
    if o.speakers not in ("single", "auto"):
        fail("--speakers is single or auto")
    if o.vad not in ("energy", "silero"):
        fail("--vad is energy or silero")
    if o.vad == "silero":
        fail("--vad silero is not implemented in the portable runtime; use --vad energy")
    if o.device not in ("auto", "cuda", "mps", "cpu"):
        fail("--device is auto, cuda, mps, or cpu")
    for stage in ("asr", "mt", "tts"):
        selected = getattr(o, f"{stage}_device")
        if selected is not None and selected not in ("auto", "cuda", "mps", "cpu"):
            fail(f"--{stage}-device is auto, cuda, mps, or cpu")
        setattr(o, f"{stage}_device", selected or o.device)
    if o.quality_first:
        o.fluent = True
    if o.background_out and not o.background:
        fail("--background-out requires --background")
    # Slot playout holds PCM until its slot, which only fluent playout does.
    if o.presentation_delay_ms > 0 and not o.fluent:
        fail("--presentation-delay-ms requires --fluent")
    if o.tap_bundle:
        fail("--tap-bundle uses macOS process taps: run apple/ a2a-pipeline on macOS, or --tap-exe/--tap-pid on Windows")
    if sum(bool(x) for x in (o.tap_exe, o.tap_pid is not None, o.tap_global)) > 1:
        fail("choose one of --tap-global, --tap-exe or --tap-pid")
    o.is_tap = o.tap_exe is not None or o.tap_pid is not None or o.tap_global
    if o.stdin or o.is_tap:
        o.clock = o.clock or "host"
        o.pace = o.pace or "realtime"
    o.clock = o.clock or "virtual"
    o.pace = o.pace or "asap"
    if o.clock not in ("virtual", "host") or o.pace not in ("realtime", "asap"):
        fail("invalid adapter, clock, or pace")
    if sum(bool(x) for x in (o.stdin, o.input is not None, o.is_tap)) != 1:
        fail("pass exactly one of --input, --stdin, or --tap-exe/--tap-pid")
    if o.is_tap and o.clock != "host":
        fail("--tap needs --clock host")
    if o.capture_gap is not None and (o.input is None or o.clock != "virtual"):
        fail("--simulate-capture-gap needs --input and --clock virtual")
    if o.asr == "apple" or o.mt == "apple":
        fail("Apple speech and translation run in the Swift driver (apple/); use --asr whisper / --mt llm here")
    if o.mt == "context":
        fail("--mt context is not implemented in the portable runtime; use --mt llm or --mt hy-local")
    if o.asr not in ("scripted", "whisper", "audio") or o.mt not in ("dictionary", "llm", "hy", "hy-local"):
        fail("invalid adapter: --asr scripted|whisper|audio, --mt dictionary|llm|hy|hy-local")
    if o.tts not in ("tone", "system", "clone"):
        fail("invalid adapter: --tts tone|system|clone")
    if o.mt in ("hy", "hy-local") and o.asr != "audio":
        fail("--mt hy and --mt hy-local translate source audio; use --asr audio")
    if o.asr == "audio" and (o.mt not in ("hy", "hy-local")
                              or (o.tts == "clone" and o.clone_mode != "xvector") or o.voice_memory):
        fail("--asr audio requires --mt hy or hy-local, audio-only reference conditioning for clone, and no voice memory")
    if o.mt != "hy-local" and (o.hy_worker or o.hy_model):
        fail("--hy-worker and --hy-model need --mt hy-local")
    if o.clone_delivery != "pinned" and o.tts != "clone":
        fail("--clone-delivery conditions cloned voices; use it with --tts clone")
    if o.speakers == "auto" and o.voice_ref:
        fail("--voice-ref fixes one voice; --speakers auto learns one per speaker")
    if not (1 <= o.rate <= 192_000 and 0 <= o.tail_seconds <= 120 and o.profile_ready_ms >= 0
            and -120 <= o.vad_threshold_db <= 0
            and (o.seconds is None or 0 < o.seconds <= 86_400)
            and (o.max_debt_seconds is None or 0 < o.max_debt_seconds <= 60)
            and (o.max_source_lag_seconds is None or 0 < o.max_source_lag_seconds <= 120)
            and (o.max_unit_keys is None or 4 <= o.max_unit_keys <= 200)
            and (o.max_unit_seconds is None or 0 <= o.max_unit_seconds <= 60)
            and (o.speaker_carry is None or 0 <= o.speaker_carry <= 60)
            and (o.speaker_carry_pause is None or 0 <= o.speaker_carry_pause <= 60)
            and (o.speaker_carry_piece is None or 0 < o.speaker_carry_piece <= 30)
            and -120 <= o.duck_db <= -20):
        fail("invalid duration, threshold, duck level, or profile delay")
    if o.captions_only and o.translation_only:
        fail("--captions-only and --translation-only are mutually exclusive")
    if o.control_stdin and o.stdin:
        fail("--control-stdin cannot share stdin with audio")
    if o.captions_only and o.playback is PlaybackMode.ORIGINAL:
        fail("playback selection requires the audio mixer, not --captions-only")
    clone_flags = (o.voice_ref, o.voice_ref_text, o.clone_python, o.clone_worker, o.clone_fake, o.clone_model,
                   o.clone_backend, o.voice_memory, o.reference_transcriber)
    if o.tts != "clone" and (any(clone_flags) or o.dual_clone):
        fail("--voice-ref and --clone-* options need --tts clone")
    # Two resident workers overlap the next phrase's synthesis with the current one's
    # playout, which only slot playout (fluent, host clock) needs and can use.
    if o.dual_clone and (o.tts != "clone" or not o.fluent or o.clock != "host"):
        fail("--dual-clone requires --tts clone --fluent --clock host")
    if o.tts == "system" and sys.platform != "win32":
        fail("--tts system uses Windows speech; on macOS use apple/ a2a-pipeline --tts system")
    o.clone_model = o.clone_model or ("omnivoice" if sys.platform != "darwin" else "qwen")
    o.clone_backend = o.clone_backend or ("mlx" if sys.platform == "darwin" else "torch")
    if o.tts == "clone" and not o.clone_fake and o.clone_backend == "torch" and o.clone_model != "omnivoice":
        # Qwen's TorchVoiceWorker path also starts a reference transcriber; --dual-clone starts
        # two of them, so its torch backend is left to the MLX path until that is measured.
        fail("the torch voice backend supports only --clone-model omnivoice"
             + (" (--dual-clone needs --clone-backend mlx)" if o.dual_clone else ""))
    if o.tts == "clone" and o.clone_backend == "mlx" and sys.platform != "darwin" and not o.clone_fake:
        fail("the MLX voice backend requires macOS on Apple silicon")
    if o.voice_ref_text and not o.voice_ref:
        fail("--voice-ref-text needs --voice-ref")
    if o.voice_ref and o.clone_mode == "icl" and not o.voice_ref_text:
        fail("--clone-mode icl needs --voice-ref-text with the reference's exact transcript (or use --clone-mode xvector)")
    if o.is_tap and (o.out or o.stems):
        fail("--out/--stems render offline; with --tap use --translated-out")
    if o.clock == "virtual":
        if o.stdin:
            fail("--clock virtual needs --input (stdin is paced by the harness in host time)")
        if o.asr != "scripted" or o.mt != "dictionary" or o.tts != "tone":
            fail("--clock virtual only models fixture adapters; real adapters need --clock host")
    if o.asr == "scripted" and not o.asr_script:
        fail("--asr scripted needs --asr-script")


# MARK: Adapters


def clone_worker_command(o) -> list[str]:
    """The TTS worker command: explicit paths, or research/tts_worker.py in this repository
    under the interpreter of its backend (the research env for MLX, this env for torch)."""
    worker = o.clone_worker or str(REPO_ROOT / "research" / "tts_worker.py")
    if not Path(worker).exists():
        fail("cannot find research/tts_worker.py; pass --clone-worker")
    if o.clone_python:
        python = o.clone_python
    elif o.clone_fake or o.clone_backend == "torch":
        python = sys.executable
    else:
        bindir = "Scripts" if sys.platform == "win32" else "bin"
        python = str(REPO_ROOT / "research" / ".venv" / bindir / ("python.exe" if sys.platform == "win32" else "python"))
    if not Path(python).exists():
        fail("no Python for the voice worker; run `uv sync --project research --locked` or pass --clone-python")
    args = [python, worker, "--model", o.clone_model, "--backend", o.clone_backend]
    if o.reference_transcriber:
        args += ["--reference-transcriber", o.reference_transcriber,
                 "--reference-locale", o.direction.source.speech_locale_identifier]
    if o.tts_device != "auto":
        args += ["--device", o.tts_device]
    if o.clone_fake:
        args.append("--fake")
    if o.fluent and o.clone_model == "qwen":
        args += ["--temperature", "0.6"]
    if o.quality_first or not o.shorten:
        args.append("--longer-speech-budget")
    if o.research_text:
        args.append("--research-text")
    return args


def make_recognizer(o, stop_event=None):
    if o.asr == "audio":
        from .models.fixtures import NullRecognizer

        return NullRecognizer()
    if o.asr == "scripted":
        from .models.fixtures import ScriptedRecognizer, load_asr_script

        return ScriptedRecognizer(load_asr_script(o.asr_script))
    from .workers.speech import WhisperRecognizer

    log(f"starting the speech recognition worker ({o.asr_model}, device {o.asr_device})")
    r = WhisperRecognizer(model=o.asr_model, language=o.direction.source, device=o.asr_device,
                          research_text=o.research_text, fluent=o.fluent, stop_event=stop_event)
    log(f"recognizer ready: {r.describe()}")
    return r


# GPU memory an OmniVoice worker needs next to recognition and translation: it held 2.2-2.6 GB
# resident in live runs, and its warm-up (reference encoding, CUDA graph capture) needs more.
# With 1.7 GB left after it loaded, a warm-up on the reference machine ran at 100% GPU for nine
# minutes without finishing (2 November). Reserving this much makes translation choose int8
# (+50-150 ms per clause on an RTX 4090) instead of starving the voice.
VOICE_WORKER_BYTES = int(4.5 * 1024**3)


def voice_reserve_bytes(o) -> int:
    """What the voice workers that load after translation will need on the same GPU."""
    if o.tts != "clone" or o.clone_fake or o.tts_device == "cpu":
        return 0
    return VOICE_WORKER_BYTES * (2 if o.dual_clone else 1)


def make_translator(o, stop_event=None, gates=None):
    if o.mt == "dictionary":
        from .models.fixtures import DictionaryTranslator

        return DictionaryTranslator.load(o.mt_dictionary)
    if o.mt in ("hy", "hy-local"):
        from .workers.hy import HyLocalTranslator, HySimulTranslator

        if o.mt == "hy":
            log(f"Hy-SimulMT server selected: source audio is sent to {o.hy_server}")
            return HySimulTranslator(o.hy_server, o.direction.value)
        worker = Path(o.hy_worker) if o.hy_worker else REPO_ROOT / ".local" / "hy-simulmt" / "bin" / (
            "hy-worker.exe" if sys.platform == "win32" else "hy-worker")
        if not worker.is_file():
            fail("Hy-SimulMT worker failed: no worker; build it with scripts/build_hy_local.py or pass --hy-worker")
        if o.hy_model:
            model = Path(o.hy_model)
        else:
            from .workers.model_paths import installed_model

            model = installed_model("hy-simulmt-30b-a3b-q4km-gguf", "speech_translation") / (
                "Hy-SimulMT-30B-A3B-Q4_K_M.gguf")
        if not model.is_file():
            fail("Hy-SimulMT worker failed: no model; fetch hy-simulmt-30b-a3b-q4km-gguf or pass --hy-model")
        command = ([sys.executable, str(worker)] if worker.suffix == ".py" else [str(worker)])
        if gates is not None and not gates.wait(gates.asr_resident, stop_event):
            from .models.base import AdapterError

            raise AdapterError("worker startup canceled")
        log(f"starting Hy-SimulMT worker ({model.name})")
        translator = HyLocalTranslator(command + ["--model", str(model)], o.direction.value, stop_event=stop_event)
        info = translator.info
        log(f"Hy-SimulMT worker ready: {info.model}, load {info.load_seconds:.1f} s, "
            f"warm-up {info.warmup_seconds:.1f} s")
        return translator
    from .workers.speech import LLMTranslator

    log(f"starting the translation worker ({o.mt_model}, device {o.mt_device})")
    t = LLMTranslator(model=o.mt_model, direction=o.direction, device=o.mt_device, research_text=o.research_text,
                      stop_event=stop_event, reserve_bytes=voice_reserve_bytes(o),
                      load_gate=gates.asr_resident if gates else None, sized_event=gates.mt_sized if gates else None)
    log(f"translator ready: {t.describe()}")
    return t


def make_synthesizer(o, stop_event=None, gates=None):
    if o.tts == "tone":
        from .models.fixtures import ToneSynthesizer

        return ToneSynthesizer()
    if o.tts == "system":
        from .models.system_voice import SystemVoiceSynthesizer

        log("starting the Windows system voice (plumbing; speaker identity is not preserved)")
        synth = SystemVoiceSynthesizer(language=o.direction.target.speech_locale_identifier, paced=o.fluent)
        log(f"system voice ready: {synth.describe()}")
        return synth
    from .workers.clone import CloneVoiceSynthesizer

    command = clone_worker_command(o)
    factory = CloneVoiceSynthesizer
    if o.dual_clone:
        from .workers.dual_clone import DualCloneVoiceSynthesizer, free_gpu_bytes

        refusal = DualCloneVoiceSynthesizer.admit(free_gpu_bytes())
        if refusal:
            log(refusal)
        else:
            factory = DualCloneVoiceSynthesizer
    log(f"starting the {o.clone_model} voice worker ({o.clone_backend}, {o.clone_mode})")
    gate = {"load_gate": gates.mt_sized} if gates is not None and factory is CloneVoiceSynthesizer else {}
    clone = factory(command, conditioning=o.clone_mode, target_language=o.direction.target, stop_event=stop_event,
                    **gate)
    info = clone.info
    log(f"voice worker ready: {info.model}, load {info.load_seconds:.1f} s, warm-up {info.warmup_seconds:.1f} s"
        + (f", {info.device}" if info.device else ""))
    return clone


@dataclass(frozen=True)
class StageGates:
    """The order in which stages that start together may claim GPU memory.

    Each stage starts its worker at once, so imports, tokenizers, and weight hashes overlap
    (5-40 s of them for torch alone on Windows). GPU memory is still claimed in the order of a
    one-by-one load, because each later stage sizes itself against what the earlier ones hold:
    recognition first (the latency-critical stage), then translation, which leaves
    `voice_reserve_bytes` for the voice, then the voice."""

    asr_resident: threading.Event  # recognition holds its GPU memory, or needs none
    mt_sized: threading.Event  # translation has read free memory and chosen its device and precision

    @staticmethod
    def wait(event: threading.Event, stop_event=None) -> bool:
        """Waits for `event`; False when `stop_event` ends start-up first."""
        while not event.wait(0.1):
            if stop_event is not None and stop_event.is_set():
                return False
        return True


class _StartupCancel:
    """The stop event for loading stages: set by a user stop or by another stage's failure,
    so a failed stage ends the others' start-up without reporting the session as stopped."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.failed = threading.Event()

    def is_set(self) -> bool:
        return self.stop.is_set() or self.failed.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.failed.wait(timeout) or self.stop.is_set()


def load_stages(o, stop: threading.Event, adapters: list, makers=None):
    """Starts recognition, translation, and the voice together and returns them once all are
    ready (about 97 s one after another on the reference PC). GPU memory is claimed in order
    (`StageGates`). Two voice workers measure what the others left before they start, so they
    still start after them. Every stage that loaded is appended to `adapters` for shutdown,
    also when another failed."""
    gates = StageGates(threading.Event(), threading.Event())
    cancel = _StartupCancel(stop)
    make_asr, make_mt, make_tts = makers or (make_recognizer, make_translator, make_synthesizer)
    results: dict[str, object] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(name, make, done=None):
        try:
            value = make()
        except BaseException as error:  # fail() raises SystemExit
            with lock:
                errors.append(error)
            cancel.failed.set()
            return
        finally:
            if done is not None:
                done.set()  # later stages proceed (or see the cancellation)
        with lock:
            results[name] = value
            if value is not None:
                adapters.append(value)

    jobs = [("asr", lambda: make_asr(o, cancel), gates.asr_resident),
            ("mt", lambda: make_mt(o, cancel, gates), gates.mt_sized)]
    synth = ("tts", lambda: make_tts(o, cancel, None if o.dual_clone else gates))
    if not o.dual_clone:
        jobs.append(synth)
    threads = [threading.Thread(target=run, args=job, name=f"a2a.load.{job[0]}", daemon=True) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        # Bounded joins keep the main thread able to run the Ctrl-C handler.
        while thread.is_alive():
            thread.join(0.1)
    if o.dual_clone and not errors and not stop.is_set():
        run(*synth)
    if errors:
        raise errors[0]  # the first failure; the rest are its cancellations
    return results.get("asr"), results.get("mt"), results.get("tts")


def make_separator(o, stop_event=None):
    if not o.background:
        return None
    from .workers.separator import RNNoiseSeparator

    bindir = "Scripts" if sys.platform == "win32" else "bin"
    python = REPO_ROOT / "runtime" / "separator" / ".venv" / bindir / (
        "python.exe" if sys.platform == "win32" else "python")
    if not python.is_file():
        fail("background separation needs `uv sync --project runtime/separator --locked`")
    log("starting the RNNoise separator (experimental; soundtrack quality is unqualified)")
    return RNNoiseSeparator([str(python), str(REPO_ROOT / "runtime" / "a2a_runtime" / "workers" / "separator_worker.py")])


def make_lid(o, stop_event=None):
    from .workers.lid import WhisperLanguageIdentifier

    log(f"starting language identification ({o.lid_model}, different backend from the Mac ECAPA adapter)")
    return WhisperLanguageIdentifier(o.direction.target, model=o.lid_model, device=o.asr_device)


def open_tap(o, stop_event=None):
    from .audio import windows as win
    from .audio.live import LiveTap

    win.restore_stranded()
    deadline_tries = 100
    while True:
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            source = win.system_audio() if o.tap_global else win.resolve(pid=o.tap_pid, exe=o.tap_exe)
            break
        except win.SourceError as error:
            deadline_tries -= 1
            if deadline_tries <= 0:
                fail(str(error))
            import time

            if stop_event is not None:
                stop_event.wait(0.2)
            else:
                time.sleep(0.2)
    # The original bus is delayed only to follow a delayed picture (ADR-0012); otherwise by the
    # short jitter reserve between the separate capture and render streams.
    delay_ms = max(30.0, float(o.presentation_delay_ms))
    tap = LiveTap(source, render=not o.captions_only, silence=not o.captions_only, duck_db=o.duck_db,
                  output_device=o.output_device, parent_pid=o.parent_pid,
                  rendered_ring_seconds=4 if o.rendered_out else 0, original_delay_ms=delay_ms,
                  max_delay_ms=delay_ms + 1_000 if o.presentation_delay_ms > 0 else 200)
    return tap


def main(argv: list[str] | None = None) -> int:
    o = parse(argv)
    stop = threading.Event()
    controls = PlaybackControls(stop, allow_playback=not o.captions_only) if o.control_stdin else None
    adapters = []
    tap = None
    try:
        def request_stop(*_):
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        # Models load and warm before any tap exists, so a slow or failed load never leaves the
        # source ducked, and Ctrl-C during start-up simply exits. The stages load together;
        # recognition is the latency-critical stage, so it has the first claim on GPU memory.
        recognizer, translator, synthesizer = load_stages(o, stop, adapters)
        if stop.is_set():
            return 0
        separator = make_separator(o, stop)
        if separator is not None:
            adapters.append(separator)
        restores_voice = (o.passthrough and o.asr == "audio" and o.background and o.tts == "clone"
                          and o.presentation_delay_ms >= 4_000)
        lid = make_lid(o, stop) if restores_voice else None
        if lid is not None:
            adapters.append(lid)
        if stop.is_set():
            return 0
        if o.is_tap:
            tap = open_tap(o, stop)
            if stop.is_set():
                return 0
        source = Source(o, live=tap)
        source.stop_requested = stop

        driver = Driver(o, recognizer, translator, synthesizer, live=tap, controls=controls,
                        separator=separator, lid=lid)
        if tap is not None:
            if stop.is_set():
                log("stopped during start-up")
                return 0
            tap.open()
            log(f"tapping {tap.source.label}, output {tap.output_device}"
                + ("" if o.captions_only else f", source ducked to {o.duck_db:.0f} dB"))
            log("live: translating; Ctrl-C stops and restores the app's audio")
        driver.run(source)
        return 1 if driver.runtime_failed else 0
    except SystemExit:
        raise
    except Exception as error:
        if stop.is_set():
            log("stopped")
            return 0
        if os.environ.get("A2A_TRACEBACK") == "1":
            import traceback

            traceback.print_exc()
        log(f"{type(error).__name__}: {error}")
        return 2
    finally:
        if tap is not None:
            tap.stop()
        for adapter in adapters:
            if hasattr(adapter, "shutdown"):
                try:
                    adapter.shutdown()
                except Exception:
                    pass


def entry() -> None:
    """Console entry point. Ends the process explicitly once outputs are flushed: daemon
    threads blocked in worker-pipe or stdin reads must not race interpreter finalization."""
    # Summaries, captions, and events are UTF-8 JSON on every platform, as the Swift driver writes.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        code = main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 2)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    entry()

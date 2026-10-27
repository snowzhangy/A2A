"""a2a-pipeline (portable): WAV or paced stdin in → translated WAV + trace out, or a live
Windows app → translated playback. Flags match apple/Tools/a2a-pipeline where they apply.

  --input clip.wav | --stdin [--rate 48000]       source audio (mono; stdin is f32le)
  --tap-exe NAME | --tap-pid N [--seconds S]      live (Windows): capture the app's process
                                                  tree; translated-only playback ducks it
  --output-device DEV                             render endpoint (name substring or ID)
  --duck-db D                                     source level while tapped (default -60)
  --asr scripted|whisper [--asr-script x.json] [--asr-model ID]
  --mt dictionary|llm [--mt-dictionary x.json] [--mt-model ID]
  --tts tone|clone [--clone-model omnivoice|qwen|chatterbox] [--clone-backend torch|mlx]
  --vad energy  --device auto|cuda|mps|cpu
  --asr-device/--mt-device/--tts-device DEVICE    override the device per stage
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
from pathlib import Path

from . import REPO_ROOT
from .core.direction import TranslationDirection
from .core.pipeline import PlaybackMode
from .driver import Driver, PlaybackControls, Source, log


def fail(message: str) -> None:
    sys.stderr.write(f"a2a-pipeline: {message}\n")
    raise SystemExit(2)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="a2a-pipeline", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    a = p.add_argument
    a("--input"), a("--stdin", action="store_true"), a("--rate", type=int, default=48_000)
    a("--clock"), a("--pace")
    a("--asr", default="scripted"), a("--asr-script"), a("--asr-model", default="faster-whisper-large-v3")
    a("--mt", default="dictionary"), a("--mt-dictionary"), a("--mt-model", default="qwen3-4b-instruct-2507")
    a("--tts", default="tone")
    a("--direction", default="zh-en")
    a("--profile-ready-ms", type=int, default=0)
    a("--clone-mode", default="icl")
    a("--clone-model", default=None)
    a("--clone-backend", default=None)
    a("--voice-memory"), a("--voice-ref"), a("--voice-ref-text")
    a("--clone-python"), a("--clone-worker"), a("--clone-fake", action="store_true")
    a("--trace", default="trace.jsonl"), a("--out"), a("--stems")
    a("--session", default=uuid.uuid4().hex[:8])
    a("--research-text", action="store_true")
    a("--tail-seconds", type=float, default=15.0)
    a("--vad-threshold-db", type=float, default=-40.0)
    a("--vad", default="energy")
    a("--device", default="auto")
    a("--asr-device"), a("--mt-device"), a("--tts-device")
    a("--tap-bundle"), a("--tap-pid", type=int), a("--tap-exe")
    a("--output-device"), a("--duck-db", type=float, default=-60.0)
    a("--seconds", type=float)
    a("--source-out"), a("--translated-out"), a("--rendered-out")
    a("--max-debt-seconds", type=float), a("--max-source-lag-seconds", type=float), a("--max-unit-keys", type=int)
    a("--captions-only", action="store_true"), a("--translation-only", action="store_true")
    a("--events-stdout", action="store_true")
    a("--playback", default="translated")
    a("--control-stdin", action="store_true")
    a("--fluent", action="store_true")
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
    if o.clone_model is not None and o.clone_model not in ("qwen", "omnivoice", "chatterbox"):
        fail("--clone-model is qwen, omnivoice, or chatterbox")
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
    if o.tap_bundle:
        fail("--tap-bundle uses macOS process taps: run apple/ a2a-pipeline on macOS, or --tap-exe/--tap-pid on Windows")
    o.is_tap = o.tap_exe is not None or o.tap_pid is not None
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
    if o.asr == "apple" or o.mt == "apple":
        fail("Apple speech and translation run in the Swift driver (apple/); use --asr whisper / --mt llm here")
    if o.asr not in ("scripted", "whisper") or o.mt not in ("dictionary", "llm") or o.tts not in ("tone", "system", "clone"):
        fail("invalid adapter: --asr scripted|whisper, --mt dictionary|llm, --tts tone|system|clone")
    if o.speakers == "auto" and o.voice_ref:
        fail("--voice-ref fixes one voice; --speakers auto learns one per speaker")
    if not (1 <= o.rate <= 192_000 and 0 <= o.tail_seconds <= 120 and o.profile_ready_ms >= 0
            and -120 <= o.vad_threshold_db <= 0
            and (o.seconds is None or 0 < o.seconds <= 86_400)
            and (o.max_debt_seconds is None or 0 < o.max_debt_seconds <= 60)
            and (o.max_source_lag_seconds is None or 0 < o.max_source_lag_seconds <= 120)
            and (o.max_unit_keys is None or 4 <= o.max_unit_keys <= 200)
            and -120 <= o.duck_db <= -20):
        fail("invalid duration, threshold, duck level, or profile delay")
    if o.tap_exe and o.tap_pid:
        fail("choose --tap-exe or --tap-pid")
    if o.captions_only and o.translation_only:
        fail("--captions-only and --translation-only are mutually exclusive")
    if o.control_stdin and o.stdin:
        fail("--control-stdin cannot share stdin with audio")
    if o.captions_only and o.playback is PlaybackMode.ORIGINAL:
        fail("playback selection requires the audio mixer, not --captions-only")
    clone_flags = (o.voice_ref, o.voice_ref_text, o.clone_python, o.clone_worker, o.clone_fake, o.clone_model,
                   o.clone_backend, o.voice_memory)
    if o.tts != "clone" and any(clone_flags):
        fail("--voice-ref and --clone-* options need --tts clone")
    if o.tts == "system":
        fail("--tts system is not implemented in the portable runtime; use --tts clone or tone")
    if o.voice_memory:
        fail("--voice-memory is not available in the portable runtime yet (see AGENTS.md)")
    o.clone_model = o.clone_model or ("omnivoice" if sys.platform != "darwin" else "qwen")
    o.clone_backend = o.clone_backend or ("mlx" if sys.platform == "darwin" else "torch")
    if o.clone_model != "qwen" and o.clone_mode == "xvector":
        fail("--clone-mode xvector requires --clone-model qwen")
    if o.tts == "clone" and not o.clone_fake and o.clone_backend == "torch" and o.clone_model != "omnivoice":
        fail("the torch voice backend supports only --clone-model omnivoice")
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
    if o.tts_device != "auto":
        args += ["--device", o.tts_device]
    if o.clone_fake:
        args.append("--fake")
    if o.fluent and o.clone_model == "qwen":
        args += ["--temperature", "0.6"]
    if o.research_text:
        args.append("--research-text")
    return args


def make_recognizer(o, stop_event=None):
    if o.asr == "scripted":
        from .models.fixtures import ScriptedRecognizer, load_asr_script

        return ScriptedRecognizer(load_asr_script(o.asr_script))
    from .workers.speech import WhisperRecognizer

    log(f"starting the speech recognition worker ({o.asr_model}, device {o.asr_device})")
    r = WhisperRecognizer(model=o.asr_model, language=o.direction.source, device=o.asr_device,
                          research_text=o.research_text, fluent=o.fluent, stop_event=stop_event)
    log(f"recognizer ready: {r.describe()}")
    return r


def make_translator(o, stop_event=None):
    if o.mt == "dictionary":
        from .models.fixtures import DictionaryTranslator

        return DictionaryTranslator.load(o.mt_dictionary)
    from .workers.speech import LLMTranslator

    log(f"starting the translation worker ({o.mt_model}, device {o.mt_device})")
    t = LLMTranslator(model=o.mt_model, direction=o.direction, device=o.mt_device, research_text=o.research_text,
                      stop_event=stop_event)
    log(f"translator ready: {t.describe()}")
    return t


def make_synthesizer(o, stop_event=None):
    if o.tts == "tone":
        from .models.fixtures import ToneSynthesizer

        return ToneSynthesizer()
    if o.tts == "system":
        from .models.system_voice import SystemVoiceSynthesizer

        return SystemVoiceSynthesizer(language=o.direction.target.speech_locale_identifier, paced=o.fluent)
    from .workers.clone import CloneVoiceSynthesizer

    log(f"starting the {o.clone_model} voice worker ({o.clone_backend}, {o.clone_mode})")
    clone = CloneVoiceSynthesizer(clone_worker_command(o), conditioning=o.clone_mode, target_language=o.direction.target,
                                  stop_event=stop_event)
    info = clone.info
    log(f"voice worker ready: {info.model}, load {info.load_seconds:.1f} s, warm-up {info.warmup_seconds:.1f} s"
        + (f", {info.device}" if info.device else ""))
    return clone


def open_tap(o, stop_event=None):
    from .audio import windows as win
    from .audio.live import LiveTap

    win.restore_stranded()
    deadline_tries = 100
    while True:
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            source = win.resolve(pid=o.tap_pid, exe=o.tap_exe)
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
    tap = LiveTap(source, render=not o.captions_only, silence=not o.captions_only, duck_db=o.duck_db,
                  output_device=o.output_device, parent_pid=o.parent_pid,
                  rendered_ring_seconds=4 if o.rendered_out else 0)
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
        # source ducked, and Ctrl-C during start-up simply exits.
        synthesizer = make_synthesizer(o, stop)
        adapters.append(synthesizer)
        if stop.is_set():
            return 0
        translator = make_translator(o, stop)
        adapters.append(translator)
        if stop.is_set():
            return 0
        recognizer = make_recognizer(o, stop)
        adapters.append(recognizer)
        if stop.is_set():
            return 0
        if o.is_tap:
            tap = open_tap(o, stop)
            if stop.is_set():
                return 0
        source = Source(o, live=tap)
        source.stop_requested = stop

        driver = Driver(o, recognizer, translator, synthesizer, live=tap, controls=controls)
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

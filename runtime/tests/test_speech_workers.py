"""Exercise worker scheduling, finalization, and framed transport without model weights."""

import io
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from a2a_runtime.core.direction import TranslationDirection, TranslationLanguage
from a2a_runtime.core.types import AudioFrame16k, SourceSpan, SpeechEnd, SpeechStart, TranslationRequest, UnitID
from a2a_runtime.models.base import AdapterError
from a2a_runtime.workers import protocol, speech
from a2a_runtime.workers.speech_worker import asr_auto_device, free_after_reserve, mt_auto_cuda, mt_ct2_compute


class FakeWorker:
    def __init__(self, argv, on_event, on_exit):
        self.on_event = on_event
        self.sent = []
        on_event({"ev": "ready", "device": "fake"}, b"")

    def send(self, header, payload=b""):
        self.sent.append((header, payload))
        return True

    def shutdown(self):
        pass

    def reply(self, **event):
        self.on_event(event, b"")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(speech, "installed_model", lambda *args: "local-model")
    monkeypatch.setattr(speech, "WorkerProcess", FakeWorker)


def test_asr_final_is_queued_after_inflight_partial_and_preserves_source_time(fake):
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    recognizer.on_vad_event(SpeechStart(48_000))
    recognizer.accept(AudioFrame16k(SourceSpan(48_000, 96_000), np.zeros(16_000, np.float32)))
    partial_id = recognizer._worker.sent[0][0]["id"]
    recognizer.on_vad_event(SpeechEnd(96_000))
    recognizer.request_finalize()
    assert recognizer.pending
    recognizer._worker.reply(ev="result", id=partial_id, words=[{"text": "你好", "start": .2, "end": .5}])
    final_id = recognizer._worker.sent[1][0]["id"]
    recognizer._worker.reply(ev="result", id=final_id, words=[{"text": "你好", "start": .2, "end": .5}])
    partial, final = recognizer.drain_hypotheses()
    assert partial.utterance == final.utterance == 1
    assert not partial.is_final and final.is_final
    assert final.tokens[0].span == SourceSpan(57_600, 72_000)
    assert final.audio_frontier == 96_000
    assert not recognizer.pending


def test_asr_queue_overflow_drops_the_oldest_final_and_keeps_running(fake):
    # The first second's partial is in flight (the fake never replies), so six finals meet a
    # MAX_FINALS queue and the two oldest are dropped. A recognizer that falls behind loses
    # (and reports) utterances instead of ending the session.
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    for i in range(6):
        recognizer.on_vad_event(SpeechStart(i * 48_000))
        recognizer.accept(AudioFrame16k(SourceSpan(i * 48_000, (i + 1) * 48_000), np.zeros(16_000, np.float32)))
        recognizer.on_vad_event(SpeechEnd((i + 1) * 48_000))
        recognizer.request_finalize()
    assert recognizer.failure is None
    assert recognizer.dropped_finals == 2


def test_continuous_speech_splits_at_the_quietest_point_instead_of_failing(fake):
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    recognizer.on_vad_event(SpeechStart(0))
    rate = 16_000
    loud = (0.2 * np.sin(2 * np.pi * 220 * np.arange(rate) / rate)).astype(np.float32)
    quiet_at = 19
    for second in range(21):
        block = loud.copy()
        if second == quiet_at:
            block[4_000:6_000] = 0  # a short pause inside the last three seconds
        recognizer.accept(AudioFrame16k(SourceSpan(second * 48_000, (second + 1) * 48_000), block))
    assert recognizer.failure is None and recognizer.forced_splits == 1
    final = next(m for m in reversed(recognizer._worker.sent) if m[0].get("final")) if any(
        m[0].get("final") for m in recognizer._worker.sent) else None
    queued = [j for j in recognizer._finals] + ([recognizer._active] if recognizer._active else [])
    split = next(j for j in queued if j["final"])
    cut_samples = len(split["audio"]) // 4
    # The cut lands inside the pause (19.25–19.375 s), within the 100 ms analysis window.
    assert abs(cut_samples - (quiet_at * rate + 5_000)) <= 1_600
    assert split["frontier"] == cut_samples * 3
    assert final is None or final[0]["final"]


def test_short_utterance_finalizes_without_a_prior_partial(fake):
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    recognizer.on_vad_event(SpeechStart(0))
    recognizer.accept(AudioFrame16k(SourceSpan(0, 12_000), np.zeros(4_000, np.float32)))
    assert not recognizer._worker.sent
    recognizer.on_vad_event(SpeechEnd(12_000))
    assert recognizer.pending and len(recognizer._worker.sent) == 1
    job_id = recognizer._worker.sent[0][0]["id"]
    recognizer._worker.reply(ev="result", id=job_id, words=[{"text": "好", "start": 0, "end": .2}])
    assert recognizer.drain_hypotheses()[0].is_final


def test_translation_worker_returns_completion_and_rejects_prefix(fake):
    translator = speech.LLMTranslator("mt", TranslationDirection.CHINESE_TO_ENGLISH)
    request = TranslationRequest(UnitID(1), (), (), "你好")
    result = []
    t = threading.Thread(target=lambda: result.append(translator.translate(request, timeout=3)))
    t.start()
    for _ in range(1000):
        if translator._worker.sent:
            break
        threading.Event().wait(.001)
    job_id = translator._worker.sent[0][0]["id"]
    translator._worker.reply(ev="result", id=job_id, text="Hello", complete=True)
    t.join(3)
    assert result[0].text == "Hello" and result[0].complete
    with pytest.raises(AdapterError, match="prefix"):
        translator.translate(TranslationRequest(UnitID(2), (), (), "你好", "Hi"))


def _wait_sent(translator, count):
    for _ in range(3000):
        if len([m for m in translator._worker.sent if m[0].get("op") == "translate"]) >= count:
            return
        threading.Event().wait(.001)
    raise AssertionError("request not sent")


def test_a_cancelled_translation_reply_cannot_break_the_next_request(fake):
    # Live trace regression: catch-up cancels a unit mid-translation, the driver starts the
    # next unit at once, and the cancelled unit's reply then arrives first.
    translator = speech.LLMTranslator("mt", TranslationDirection.CHINESE_TO_ENGLISH)
    first, second = [], []
    t1 = threading.Thread(target=lambda: first.append(_capture(translator, TranslationRequest(UnitID(8), (), (), "谢谢大家,"))))
    t1.start()
    _wait_sent(translator, 1)
    translator.cancel_unit(UnitID(8))
    t1.join(3)
    t2 = threading.Thread(target=lambda: second.append(translator.translate(TranslationRequest(UnitID(9), (), (), "会议到此结束。"), timeout=3)))
    t2.start()
    _wait_sent(translator, 2)
    ids = [m[0]["id"] for m in translator._worker.sent if m[0].get("op") == "translate"]
    assert {"op": "cancel", "id": ids[0]} in [m[0] for m in translator._worker.sent]
    translator._worker.reply(ev="result", id=ids[0], text="Thank you all,", complete=True)  # late, unwanted
    translator._worker.reply(ev="result", id=ids[1], text="The meeting is over.", complete=True)
    t2.join(3)
    assert translator.failure is None
    assert isinstance(first[0], AdapterError) and "canceled" in str(first[0])
    assert second[0].text == "The meeting is over."


def _capture(translator, request):
    try:
        return translator.translate(request, timeout=3)
    except AdapterError as error:
        return error


def test_transport_handles_short_writes_and_rejects_oversize():
    class ShortWriter(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])

    target = ShortWriter()
    encoded = protocol.encode({"op": "x"}, b"abc")
    protocol._write_all(target, encoded)
    assert protocol.read_frame(io.BytesIO(target.getvalue())) == ({"op": "x"}, b"abc")
    with pytest.raises(ValueError, match="limits"):
        protocol.encode({"x": "a" * protocol.MAX_HEADER})


def test_recognition_auto_respects_free_gpu_memory():
    model = 3 * 1024**3
    assert asr_auto_device(True, 8 * 1024**3, model) == ("cuda", None)
    # int8_float16 weights take about 55% of the float16 file, plus 1 GiB of headroom.
    assert asr_auto_device(True, 3 * 1024**3, model) == ("cuda", None)
    device, reason = asr_auto_device(True, 2 * 1024**3, model)
    assert device == "cpu" and "insufficient free GPU memory" in reason
    assert asr_auto_device(False, None, model) == ("cpu", None)
    assert asr_auto_device(True, None, model) == ("cuda", None)


def test_translation_auto_respects_free_gpu_memory():
    model = 8 * 1024**3
    assert mt_auto_cuda(12 * 1024**3, model) == ("cuda", None)
    device, reason = mt_auto_cuda(9 * 1024**3, model)
    assert device == "cpu" and "insufficient free GPU memory" in reason
    assert mt_auto_cuda(None, model) == ("cuda", None)


def test_cli_stage_device_overrides():
    from a2a_runtime.cli import clone_worker_command, parse

    options = parse(["--input", "clip.wav", "--asr-script", "script.json", "--device", "cuda", "--asr-device", "cpu"])
    assert (options.asr_device, options.mt_device, options.tts_device) == ("cpu", "cuda", "cuda")
    options = parse(["--input", "clip.wav", "--clock", "host", "--asr-script", "script.json", "--tts", "clone",
                     "--clone-fake", "--device", "cuda", "--tts-device", "cpu"])
    command = clone_worker_command(options)
    assert command[command.index("--device") + 1] == "cpu"


def test_tail_limit_fails_when_recognition_has_not_finished():
    from a2a_runtime.driver import Driver

    driver = Driver.__new__(Driver)
    driver.recognizer = SimpleNamespace(pending=True)
    driver.runtime_failed = False
    driver.check_recognition_tail(99, 100)
    assert not driver.runtime_failed
    driver.check_recognition_tail(100, 100)
    assert driver.runtime_failed


def test_translation_leaves_room_for_the_voice_that_loads_after_it():
    # 13 GB free after recognition, as on the reference machine with other apps holding 9 GB:
    # bf16 weights fit on their own, but not next to the voice worker's reserve, so they go int8.
    from a2a_runtime.cli import VOICE_WORKER_BYTES

    model = int(8.3 * 1024**3)
    free = 13 * 1024**3
    assert mt_ct2_compute(free, model) == ("bfloat16", None)
    compute, note = mt_ct2_compute(free_after_reserve(free, VOICE_WORKER_BYTES), model)
    assert compute == "int8_bfloat16" and "int8" in note
    assert free_after_reserve(None, 3 * 1024**3) is None
    assert free_after_reserve(2 * 1024**3, 3 * 1024**3) == 0


def test_only_a_gpu_voice_worker_reserves_memory():
    from a2a_runtime.cli import VOICE_WORKER_BYTES, parse, voice_reserve_bytes

    base = ["--input", "x.wav", "--clock", "host", "--asr", "scripted", "--asr-script", "a.json"]
    assert voice_reserve_bytes(parse(base + ["--tts", "clone"])) == VOICE_WORKER_BYTES
    assert voice_reserve_bytes(parse(base + ["--tts", "clone", "--fluent", "--dual-clone"])) == 2 * VOICE_WORKER_BYTES
    assert voice_reserve_bytes(parse(base + ["--tts", "clone", "--tts-device", "cpu"])) == 0
    assert voice_reserve_bytes(parse(base + ["--tts", "tone"])) == 0


def test_translation_sizes_itself_only_once_recognition_holds_its_memory(monkeypatch):
    # Started beside recognition, translation must not read free GPU memory before
    # recognition has claimed its own, or it would size itself for memory that is taken.
    seen = []

    class GatedWorker(FakeWorker):
        def __init__(self, argv, on_event, on_exit):
            seen.append(argv)
            self.on_event = on_event
            self.sent = []
            instance.append(self)

        def send(self, header, payload=b""):
            self.sent.append(header)
            if header.get("op") == "load":  # the worker sizes itself, then loads
                self.on_event({"ev": "sized"}, b"")
                self.on_event({"ev": "ready", "device": "cuda"}, b"")
            return True

    instance = []
    monkeypatch.setattr(speech, "installed_model", lambda *args: "local-model")
    monkeypatch.setattr(speech, "WorkerProcess", GatedWorker)
    monkeypatch.setattr(speech, "derived_translations", lambda model: ("bf16-dir", "int8-dir"))
    recognition_resident, sized = threading.Event(), threading.Event()
    result = []
    loader = threading.Thread(target=lambda: result.append(speech.LLMTranslator(
        "m", TranslationDirection.CHINESE_TO_ENGLISH, reserve_bytes=4608 * 1024**2,
        load_gate=recognition_resident, sized_event=sized)))
    loader.start()
    time.sleep(0.3)
    assert instance and instance[0].sent == [], "translation sized itself before recognition was resident"
    assert not sized.is_set()
    recognition_resident.set()
    loader.join(5)
    assert result and instance[0].sent == [{"op": "load"}] and sized.is_set()
    argv = seen[0]
    assert "--wait-load" in argv and argv[argv.index("--reserve-mb") + 1] == "4608"
    assert argv[argv.index("--derived") + 1] == "bf16-dir" and argv[argv.index("--derived-int8") + 1] == "int8-dir"


def test_voice_worker_waits_for_the_host_before_loading(tmp_path):
    # The real worker protocol with the dependency-free backend: started early, it becomes
    # ready only after the load frame.
    from a2a_runtime import REPO_ROOT
    from a2a_runtime.workers.clone import CloneVoiceSynthesizer

    gate, result = threading.Event(), []
    command = [sys.executable, str(REPO_ROOT / "research" / "tts_worker.py"), "--fake", "--model", "omnivoice"]
    loader = threading.Thread(target=lambda: result.append(CloneVoiceSynthesizer(command, load_gate=gate)))
    loader.start()
    try:
        time.sleep(2.0)
        assert not result, "the voice loaded before translation had sized itself"
        gate.set()
        loader.join(20)
        assert result and result[0].info is not None and result[0].info.load_seconds < 1.5
    finally:
        gate.set()
        loader.join(20)
        for synthesizer in result:
            synthesizer.shutdown()


def test_int8_compute_loads_the_stored_int8_conversion():
    from a2a_runtime.workers.speech_worker import ct2_artifact

    assert ct2_artifact("bfloat16", "bf16", "int8") == "bf16"
    assert ct2_artifact("int8_bfloat16", "bf16", "int8") == "int8"
    assert ct2_artifact("int8", "bf16", "int8") == "int8"  # CPU
    assert ct2_artifact("int8_bfloat16", "bf16", None) == "bf16"  # quantized at load, as before


def test_derived_conversions_are_found_by_stored_quantization():
    from a2a_runtime.models import derive

    doc = {"derived": [{"id": "a", "from": "m", "tool": "ctranslate2", "options": ["--quantization", "bfloat16"]},
                       {"id": "b", "from": "m", "tool": "ctranslate2", "options": ["--quantization", "int8_bfloat16"]}]}
    assert derive.for_source("m", "ctranslate2", doc)["id"] == "a"
    assert derive.for_source("m", "ctranslate2", doc, "bfloat16")["id"] == "a"
    assert derive.for_source("m", "ctranslate2", doc, "int8_bfloat16")["id"] == "b"
    assert derive.for_source("m", "ctranslate2", doc, "int8_float16") is None
    manifest = derive._manifest()
    assert derive.for_source("qwen3-4b-instruct-2507", "ctranslate2", manifest)["id"] == "qwen3-4b-instruct-2507-ct2"
    assert derive.for_source("qwen3-4b-instruct-2507", "ctranslate2", manifest,
                             "int8_bfloat16")["id"] == "qwen3-4b-instruct-2507-ct2-int8"


def _stage_options(*extra):
    from a2a_runtime.cli import parse

    return parse(["--input", "x.wav", "--clock", "host", "--asr", "scripted", "--asr-script", "a.json", *extra])


def test_stages_load_together_and_all_are_kept_for_shutdown():
    from a2a_runtime.cli import load_stages

    started, release = [], threading.Event()

    def maker(name):
        def make(o, cancel, budget=None):
            started.append(name)
            assert release.wait(5), "stages did not start together"
            return name
        return make

    def release_when_all_started():
        while len(started) < 3:
            threading.Event().wait(0.01)
        release.set()

    threading.Thread(target=release_when_all_started, daemon=True).start()
    adapters = []
    stages = load_stages(_stage_options(), threading.Event(), adapters, (maker("asr"), maker("mt"), maker("tts")))
    assert stages == ("asr", "mt", "tts") and sorted(adapters) == ["asr", "mt", "tts"]


def test_a_failed_stage_cancels_the_others_and_reports_its_own_error():
    from a2a_runtime.cli import load_stages

    stop = threading.Event()

    def failing(o, cancel, budget=None):
        raise AdapterError("recognition model missing")

    def waiting(o, cancel, budget=None):
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise AdapterError("worker startup canceled")

    def loaded(o, cancel, budget=None):
        return "voice"

    adapters = []
    with pytest.raises(AdapterError, match="recognition model missing"):
        load_stages(_stage_options(), stop, adapters, (failing, waiting, loaded))
    assert not stop.is_set(), "a failure is not a user stop"
    assert adapters == ["voice"], "a stage that loaded is still shut down"


def test_a_user_stop_cancels_every_loading_stage():
    from a2a_runtime.cli import load_stages

    stop = threading.Event()

    def waiting(o, cancel, budget=None):
        stop.set()
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise AdapterError("worker startup canceled")

    with pytest.raises(AdapterError, match="canceled"):
        load_stages(_stage_options(), stop, [], (waiting, waiting, waiting))


def test_stages_claim_gpu_memory_in_order():
    # All three start at once; translation passes its gate only after recognition is
    # resident, and the voice only after translation has sized itself.
    from a2a_runtime.cli import load_stages

    order, lock = [], threading.Lock()

    def note(event):
        with lock:
            order.append(event)

    def asr(o, cancel):
        note("asr started")
        time.sleep(0.2)
        note("asr resident")
        return "asr"

    def mt(o, cancel, gates):
        note("mt started")
        assert gates.wait(gates.asr_resident, cancel)
        note("mt sizing")
        time.sleep(0.1)
        note("mt sized")  # a translator without a `sized` report counts as sized once ready
        return "mt"

    def tts(o, cancel, gates):
        note("tts started")
        assert gates.wait(gates.mt_sized, cancel)
        note("tts loading")
        return "tts"

    assert load_stages(_stage_options(), threading.Event(), [], (asr, mt, tts)) == ("asr", "mt", "tts")
    assert {"asr started", "mt started", "tts started"} == set(order[:3])
    assert order[3:] == ["asr resident", "mt sizing", "mt sized", "tts loading"]


def test_two_voice_workers_still_load_after_recognition_and_translation():
    # Their admission measures what recognition and translation left on the GPU.
    from a2a_runtime.cli import load_stages

    order = []

    def maker(name):
        def make(o, cancel, budget=None):
            order.append(name)
            time.sleep(0.05)
            order.append(name + " ready")
            return name
        return make

    load_stages(_stage_options("--tts", "clone", "--fluent", "--dual-clone"), threading.Event(), [],
                (maker("asr"), maker("mt"), maker("tts")))
    assert order[-2:] == ["tts", "tts ready"] and order.index("tts") > max(order.index("asr ready"), order.index("mt ready"))

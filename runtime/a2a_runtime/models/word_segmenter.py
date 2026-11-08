"""Windows' on-device word segmenter, used to keep commit cuts and shortened targets off Han
words: the counterpart of apple/Sources/A2AModels/NaturalLanguageWordSegmenter.swift.

The recognizers emit one key per Chinese character, so a policy that only sees keys cuts 讨论
in half. `Windows.Data.Text.WordsSegmenter` segments with the system's language data: nothing
is downloaded, nothing leaves the process, and it is a text-analysis service of the OS rather
than a speech model, so it stays out of models/manifest.yaml. It is called in-process through
the WinRT ABI with ctypes (no PowerShell, no build step).
"""

from __future__ import annotations

import ctypes as C
import sys
import threading

from ..core.text import graphemes

# Interface IDs and vtable slots from windows.data.text.h (Windows SDK 10.0.26100.0). Every
# WinRT interface starts with IInspectable's six methods (QueryInterface ... GetTrustLevel).
_IID_WORDS_SEGMENTER_FACTORY = "{E6977274-FC35-455C-8BFB-6D7F4653CA97}"
_CLASS = "Windows.Data.Text.WordsSegmenter"
_RELEASE = 2
_FACTORY_CREATE_WITH_LANGUAGE = 6
_SEGMENTER_GET_TOKENS = 8
_VECTOR_GET_AT = 6
_VECTOR_GET_SIZE = 7
_SEGMENT_GET_SOURCE_TEXT_SEGMENT = 7
_RO_INIT_MULTITHREADED = 1


class _GUID(C.Structure):
    _fields_ = [("Data1", C.c_uint32), ("Data2", C.c_uint16), ("Data3", C.c_uint16), ("Data4", C.c_ubyte * 8)]


class _TextSegment(C.Structure):
    _fields_ = [("StartPosition", C.c_uint32), ("Length", C.c_uint32)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    C.oledll.ole32.CLSIDFromString(C.c_wchar_p(text), C.byref(g))
    return g


def _method(obj: C.c_void_p, slot: int, *argtypes):
    vtable = C.cast(C.cast(obj, C.POINTER(C.c_void_p))[0], C.POINTER(C.c_void_p))
    return C.WINFUNCTYPE(C.c_long, C.c_void_p, *argtypes)(vtable[slot])


def _release(obj: C.c_void_p) -> None:
    if obj:
        C.WINFUNCTYPE(C.c_ulong, C.c_void_p)(C.cast(C.cast(obj, C.POINTER(C.c_void_p))[0],
                                                    C.POINTER(C.c_void_p))[_RELEASE])(obj)


class _HString:
    def __init__(self, text: str) -> None:
        self.h = C.c_void_p()
        units = len(text.encode("utf-16-le")) // 2
        _check(C.windll.combase.WindowsCreateString(C.c_wchar_p(text), C.c_uint32(units), C.byref(self.h)))

    def __enter__(self):
        return self.h

    def __exit__(self, *_):
        C.windll.combase.WindowsDeleteString(self.h)


def _check(hr: int) -> None:
    if hr < 0:
        raise OSError(f"WinRT call failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


class WindowsWordSegmenter:
    """`splits_word(text, offset)`: True when the character offset lies strictly inside a word."""

    def __init__(self, language: str) -> None:
        if sys.platform != "win32":
            raise OSError("Windows.Data.Text is available on Windows only")
        self.language = language
        self._thread_ready: set[int] = set()
        self._factory = None
        self._cache: tuple[str, list[tuple[int, int]] | None] | None = None
        self._segmenter = self._create()

    def _ensure_thread(self) -> None:
        # S_FALSE (already initialized) and RPC_E_CHANGED_MODE (an STA thread) both still allow
        # this agile object to be used; only a hard failure is an error.
        ident = threading.get_ident()
        if ident not in self._thread_ready:
            hr = C.windll.combase.RoInitialize(C.c_int(_RO_INIT_MULTITHREADED))
            if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:
                _check(hr)
            self._thread_ready.add(ident)

    def _create(self) -> C.c_void_p:
        self._ensure_thread()
        factory = C.c_void_p()
        iid = _guid(_IID_WORDS_SEGMENTER_FACTORY)
        with _HString(_CLASS) as name:
            _check(C.windll.combase.RoGetActivationFactory(name, C.byref(iid), C.byref(factory)))
        segmenter = C.c_void_p()
        try:
            with _HString(self.language) as language:
                create = _method(factory, _FACTORY_CREATE_WITH_LANGUAGE, C.c_void_p, C.POINTER(C.c_void_p))
                _check(create(factory, language, C.byref(segmenter)))
        finally:
            _release(factory)
        return segmenter

    def tokens(self, text: str) -> list[tuple[int, int]] | None:
        """Word ranges as character (grapheme) offsets, or None when nothing was segmented."""
        if self._cache is not None and self._cache[0] == text:
            return self._cache[1]
        self._ensure_thread()
        clusters = graphemes(text)
        # UTF-16 offset of each character boundary, to map WinRT positions back to characters.
        boundary = {}
        units = 0
        for i, ch in enumerate(clusters):
            boundary[units] = i
            units += len(ch.encode("utf-16-le")) // 2
        boundary[units] = len(clusters)
        vector = C.c_void_p()
        out: list[tuple[int, int]] = []
        with _HString(text) as h:
            get_tokens = _method(self._segmenter, _SEGMENTER_GET_TOKENS, C.c_void_p, C.POINTER(C.c_void_p))
            _check(get_tokens(self._segmenter, h, C.byref(vector)))
        try:
            size = C.c_uint32()
            _check(_method(vector, _VECTOR_GET_SIZE, C.POINTER(C.c_uint32))(vector, C.byref(size)))
            get_at = _method(vector, _VECTOR_GET_AT, C.c_uint32, C.POINTER(C.c_void_p))
            for i in range(size.value):
                segment = C.c_void_p()
                _check(get_at(vector, i, C.byref(segment)))
                try:
                    span = _TextSegment()
                    _check(_method(segment, _SEGMENT_GET_SOURCE_TEXT_SEGMENT, C.POINTER(_TextSegment))(
                        segment, C.byref(span)))
                finally:
                    _release(segment)
                start, end = boundary.get(span.StartPosition), boundary.get(span.StartPosition + span.Length)
                if start is not None and end is not None and end > start:
                    out.append((start, end))
        finally:
            _release(vector)
        result = out or None
        self._cache = (text, result)
        return result

    def splits_word(self, text: str, offset: int) -> bool:
        tokens = self.tokens(text)
        return bool(tokens) and any(start < offset < end for start, end in tokens)

    def close(self) -> None:
        segmenter, self._segmenter = self._segmenter, None
        _release(segmenter)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def available(language: str) -> bool:
        """True when the system can segment this language on this machine."""
        try:
            probe = WindowsWordSegmenter(language)
            sample = "word boundary here" if language == "en" else "我们讨论这个问题"
            return len(probe.tokens(sample) or []) > 1
        except OSError:
            return False

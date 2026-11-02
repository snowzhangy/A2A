"""Language choices shared by the reducer and model drivers. Port of
apple/Sources/A2ACore/TranslationDirection.swift."""

from __future__ import annotations

from enum import Enum


class TranslationLanguage(str, Enum):
    MANDARIN = "mandarin"
    ENGLISH = "english"

    @property
    def display_name(self) -> str:
        return "Mandarin" if self is TranslationLanguage.MANDARIN else "English"

    @property
    def speech_locale_identifier(self) -> str:
        return "zh-CN" if self is TranslationLanguage.MANDARIN else "en-US"

    @property
    def translation_identifier(self) -> str:
        return "zh-Hans" if self is TranslationLanguage.MANDARIN else "en"

    @property
    def clone_language(self) -> str:
        return "chinese" if self is TranslationLanguage.MANDARIN else "english"

    @property
    def whisper_code(self) -> str:
        return "zh" if self is TranslationLanguage.MANDARIN else "en"

    @property
    def warm_up_text(self) -> str:
        return "好的，我们开始吧。" if self is TranslationLanguage.MANDARIN else "Okay, let's begin."


class TranslationDirection(str, Enum):
    CHINESE_TO_ENGLISH = "zh-en"
    ENGLISH_TO_CHINESE = "en-zh"

    @property
    def source(self) -> TranslationLanguage:
        return TranslationLanguage.MANDARIN if self is TranslationDirection.CHINESE_TO_ENGLISH else TranslationLanguage.ENGLISH

    @property
    def target(self) -> TranslationLanguage:
        return TranslationLanguage.ENGLISH if self is TranslationDirection.CHINESE_TO_ENGLISH else TranslationLanguage.MANDARIN

    @property
    def label(self) -> str:
        return f"{self.source.display_name} → {self.target.display_name}"

    @property
    def trace_pair(self) -> str:
        return f"{self.source.speech_locale_identifier}>{self.target.speech_locale_identifier}"

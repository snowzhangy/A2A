"""Portable policy core: a line-for-line port of apple/Sources/A2ACore (ADR-0007).

Pure: no I/O, no audio devices, no models. Clocks and events are injected. Both
implementations must reproduce apple/Tests/A2ACoreTests/Golden byte for byte.
"""

from .commit_policy import CommitPolicy, CommitPolicyConfig, CommitReason, SourceUnit
from .direction import TranslationDirection, TranslationLanguage
from .pipeline import *  # noqa: F401,F403  (inputs, effects, SessionPipeline, PipelineConfig)
from .pipeline import PipelineConfig, PlaybackMode, SessionPipeline
from .playout_gate import PlayoutGate
from .reference_policy import ReferencePlan, ReferencePolicy, ReferencePolicyConfig, Rejected
from .speech_evidence import SpeechEvidence
from .state_machine import BusGains, HealthThresholds, SessionState
from .trace import TraceEvent, TraceKind
from .types import *  # noqa: F401,F403
from .validator import TranslationValidator

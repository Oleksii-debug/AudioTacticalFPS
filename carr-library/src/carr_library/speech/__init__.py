"""Local, explicitly selected speech engines and immutable reading derivatives."""

from .local import (
    MAX_TEXT_CHARACTERS, RenderCancelled, SpeechError, SpeechRequest, SpeechResult,
    SpeechUnavailable, Voice, discover_voices, engine_fingerprint, render_speech,
)
from .preparation import (
    NarrationDerivative, Pronunciation, ReadingEdit, ReadingSpan,
    apply_reading_edits, prepare_reading,
)
from .player import LocalPlayer, PlayerError, PlayerUnavailable

__all__ = [
    "MAX_TEXT_CHARACTERS", "RenderCancelled", "SpeechError", "SpeechRequest",
    "SpeechResult", "SpeechUnavailable", "Voice", "discover_voices",
    "engine_fingerprint", "render_speech", "NarrationDerivative",
    "Pronunciation", "ReadingEdit", "ReadingSpan", "apply_reading_edits",
    "prepare_reading",
    "LocalPlayer", "PlayerError", "PlayerUnavailable",
]

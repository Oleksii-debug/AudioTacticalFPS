"""Read-only snapshots from the current audio and OCR inventory services.

The canonical selection remains in ``catalog``. These records describe one
explicit local enumeration, never a second registry or language qualification.
No helper, model, voice or catalogue is downloaded here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from .catalog import build_capability_matrix


@dataclass(frozen=True)
class InstalledVoiceInventory:
    voices: tuple
    notices: tuple[str, ...]
    enumerated_at: str


@dataclass(frozen=True)
class InstalledOcrInventory:
    identifiers: tuple[str, ...]
    text_models: tuple[tuple[str, str], ...]
    enumerated_at: str
    profile: str = 'installed'


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Local inventory inspection cancelled.')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def collect_installed_voices(library=None, *, registry=None, cancelled=lambda: False):
    """Use the production VoiceRegistry, optionally already configured by its owner.

    The current voice API bounds its own adapter probes but does not accept a
    whole-registry cancellation callback. Cancellation is therefore checked
    before and after enumeration, and the result is discarded on cancellation.
    This function never synthesizes text or reads a document/library database.
    """
    from ..audio.engines import VoiceRegistry, default_registry
    _check(cancelled)
    registry = default_registry() if registry is None else registry
    if not isinstance(registry, VoiceRegistry):
        raise ValueError('Use the existing configured local voice registry.')
    _check(cancelled)
    voices = tuple(registry.voices(refresh=True))
    _check(cancelled)
    # Validate the actual immutable audio record and every declared language;
    # no binding is guessed merely because enumeration returned a voice.
    build_capability_matrix(voices=voices)
    notices = []
    for index, notice in enumerate(registry.notices):
        if index >= 1000 or type(notice) is not dict:
            raise ValueError('The local voice notices exceed their metadata limits.')
        engine, reason, available = (notice.get('engine'), notice.get('reason', ''),
                                     notice.get('available'))
        if (not isinstance(engine, str) or not 0 < len(engine) <= 256
                or not isinstance(reason, str) or len(reason) > 4000
                or type(available) is not bool):
            raise ValueError('The local voice notice is not a bounded adapter declaration.')
        notices.append(engine + ': ' + ('Available' if available else reason or 'Unavailable'))
    _check(cancelled)
    return InstalledVoiceInventory(voices, tuple(notices), _now())


def collect_installed_ocr(*, cancelled=lambda: False):
    """Reuse the current selected Tesseract inventory worker, not another probe.

    The existing picker owns runtime verification, deadlines, output limits and
    cancellation. Its catalogue supplies labels and distinguishes scripts and
    auxiliary data; these identifiers are not BCP47 language qualifications.
    """
    from ..ui.ocr_selection import discover_installed_models, selectable_models
    _check(cancelled)
    identifiers = discover_installed_models(cancelled=cancelled)
    _check(cancelled)
    rows = selectable_models('installed', identifiers)
    _check(cancelled)
    return InstalledOcrInventory(tuple(identifiers), rows, _now())

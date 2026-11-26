"""Explicit non-secret configuration of the existing local speech adapters."""
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
import json

from .jobs import atomic_json, checked_directory
from .model_manifest import LocalModelManifest
from ..ingest.writer_guard import exclusive_writer
from ..reader.narration_text import decode_json, read_document

FILENAME = 'audio-engine-settings.json'
MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class EngineSettings:
    ffmpeg: str | None = None
    piper_executable: str | None = None
    piper_manifest: str | None = None
    dordiai_manifest: str | None = None

    def __post_init__(self):
        for value in asdict(self).values():
            if value is not None and (not isinstance(value, str) or not value or len(value) > 8192
                    or any(ord(char) < 32 for char in value) or value.startswith(('//', '\\\\'))
                    or not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())):
                raise ValueError('Local engine settings require absolute local file paths.')
        if bool(self.piper_executable) != bool(self.piper_manifest):
            raise ValueError('Piper requires both its reviewed executable and model manifest.')

    def to_dict(self):
        return {'kind': 'research-local-engine-settings', 'version': 1, **asdict(self)}


def validate_settings_bytes(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('Local engine settings exceed their size limit.')
    value = decode_json(raw.decode('utf-8'))
    fields = {'kind', 'version', 'ffmpeg', 'piper_executable', 'piper_manifest', 'dordiai_manifest'}
    if (type(value) is not dict or set(value) != fields or value['kind'] != 'research-local-engine-settings'
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('Unsupported local engine settings; executable paths are never imported from projects.')
    return EngineSettings(**{key: value[key] for key in fields - {'kind', 'version'}})


def load_settings(library_root):
    path = Path(library_root) / FILENAME
    if not path.exists() and not path.is_symlink():
        return EngineSettings()
    return validate_settings_bytes(read_document(path, byte_budget=MAX_BYTES))


def _verify_selected(settings):
    from ..speech._io import local_file
    for value in asdict(settings).values():
        if value is not None:
            local_file(value)
    for engine, value in (('piper', settings.piper_manifest), ('dordiai-voices', settings.dordiai_manifest)):
        if value is not None:
            LocalModelManifest.load(value, engine=engine)


def save_settings(library_root, settings):
    if not isinstance(settings, EngineSettings):
        raise ValueError('Validated local engine settings are required.')
    _verify_selected(settings)
    root = checked_directory(library_root, create=True)
    with exclusive_writer(root):
        # Fail closed on unexpected/hard-linked state, preserving its last bytes.
        load_settings(root)
        atomic_json(root / FILENAME, settings.to_dict())
    return root / FILENAME


def configured_registry(library_root, *, ffmpeg=None):
    from .engines import default_registry
    settings = load_settings(library_root)
    selected = asdict(settings)
    if ffmpeg is not None:
        selected['ffmpeg'] = str(ffmpeg)
    _verify_selected(EngineSettings(**selected))
    return default_registry(**selected)

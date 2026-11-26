"""Bind an existing local OCR engine to a verified, explicitly installed profile.

This adapter does not discover an executable, download a model or change the
process environment. The base runtime still owns executable/DLL validation;
the model store owns the selected profile's model and licence bytes.
"""
from dataclasses import dataclass

from .ocr_languages import model_ids
from .ocr_model_store import ModelSelection, ModelStoreError, OcrModelStore
from .ocr_runtime import OcrRuntime, OcrRuntimeError


_INFORMATIONAL = frozenset((
    '--version', '--list-langs', '--help', '--help-extra', '--help-psm',
    '--help-oem', '--print-parameters',
))


def _store_call(operation):
    """Keep model-store failures at the OCR boundary without leaking local paths."""
    try:
        return operation()
    except (InterruptedError, TimeoutError):
        raise
    except ModelStoreError as exc:
        raise OcrRuntimeError(str(exc)) from None
    except (OSError, ValueError):
        raise OcrRuntimeError('The selected OCR model profile is unavailable or invalid.') from None


@dataclass(frozen=True)
class ManagedOcrRuntime:
    """An engine plus one immutable selection; neither authority replaces the other."""

    runtime: OcrRuntime
    selection: ModelSelection
    managed = True

    @property
    def executable(self):
        return self.runtime.executable

    @property
    def root(self):
        return self.runtime.root

    @property
    def target(self):
        return self.runtime.target

    @property
    def portable(self):
        return self.runtime.portable

    @property
    def manifest_sha256(self):
        return self.runtime.manifest_sha256

    @property
    def files_sha256(self):
        return self.runtime.files_sha256

    @property
    def tessdata(self):
        return self.selection.directory

    @property
    def languages(self):
        return self.selection.languages

    def command(self, argv):
        """Use selected models and LSTM, preserving explicit per-call staging/OSD.

        An explicit model directory is reserved for a caller's separately
        verified staging directory. Informational queries never need an engine
        mode. OSD's explicit ``--oem 0`` is preserved because it is not LSTM.
        The caller's argument list and global TESSDATA_PREFIX remain unchanged.
        """
        command = list(argv)
        if not command:
            raise OcrRuntimeError('An OCR command must include its executable.')
        has_directory = any(arg == '--tessdata-dir' or arg.startswith('--tessdata-dir=')
                            for arg in command[1:])
        if '--version' not in command and not has_directory:
            command[1:1] = ['--tessdata-dir', str(self.tessdata)]
        has_mode = any(arg == '--oem' or arg.startswith('--oem=') for arg in command[1:])
        if (len(argv) >= 3 and not _INFORMATIONAL.intersection(command)
                and not has_mode):
            command[1:1] = ['--oem', '1']
        return command

    def fingerprint(self):
        """Retain the base manifest identity and add path-free selected-model proof."""
        return {**self.runtime.fingerprint(), 'managed_models': self.selection.fingerprint()}

    def require_languages(self, languages, orientation=False):
        try:
            wanted = set(model_ids(languages)) | ({'osd'} if orientation else set())
        except ValueError:
            raise OcrRuntimeError('Invalid OCR language selection.') from None
        if not wanted <= set(self.languages):
            raise OcrRuntimeError('The selected OCR profile lacks the requested language or orientation model.')

    def assert_unchanged(self, remaining=lambda: None):
        self.runtime.assert_unchanged(remaining)
        _store_call(lambda: self.selection.assert_unchanged(remaining))


def bind_model_profile(runtime, profile, languages, *, orientation=False,
                       remaining=lambda: None):
    """Return the installed runtime unchanged, or bind a verified local profile."""
    if profile == 'installed':
        return runtime
    if runtime is None:
        raise OcrRuntimeError('No local OCR engine is available for the selected model profile.')
    selection = _store_call(lambda: OcrModelStore().select(
        profile, languages, orientation=orientation, remaining=remaining))
    result = ManagedOcrRuntime(runtime, selection)
    result.require_languages(languages, orientation)
    return result

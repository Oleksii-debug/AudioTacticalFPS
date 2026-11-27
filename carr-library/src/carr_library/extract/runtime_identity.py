"""Local extraction fingerprint for journal planning; not a hermetic attestation."""
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
import importlib.metadata as metadata
import re
import shutil
import subprocess
import tempfile
import time

from .pinned_tessdata import POLICY, stage_pinned_models
from . import ocr_languages, ocr_runtime
from .ocr_process import run_ocr


class IdentityError(ValueError):
    pass


def _hash(path, limit=256 * 1024 * 1024, remaining=lambda: None):
    h = sha256()
    count = 0
    path = Path(path)
    if not path.is_file():
        raise IdentityError('runtime file unavailable')
    with path.open('rb') as f:
        while chunk := f.read(1024 * 1024):
            remaining()
            count += len(chunk)
            if count > limit:
                raise IdentityError('runtime file exceeds fingerprint limit')
            h.update(chunk)
    return h.hexdigest()


def _source_hashes():
    from . import language_manager
    from ..catalog import streaming_pdf
    root = Path(__file__).parent
    names = ('page_ocr.py', 'orientation.py', 'ocr_orientation_bridge.py',
             'pinned_tessdata.py', 'runtime_identity.py', 'ocr_languages.py',
             'model_catalog.py', 'ocr_model_store.py', 'managed_ocr.py',
             'ocr_model_catalog.py', 'ocr-model-catalog.json', 'ocr_process.py')
    return {**{name: _hash(root / name) for name in names},
            'streaming_pdf.py': _hash(streaming_pdf.__file__),
            'ocr_runtime.py': _hash(ocr_runtime.__file__),
            'language_manager.py': _hash(language_manager.__file__)}


def runtime_identity(config):
    """Return path-free JSON-compatible identity for a trusted local installation.

    Missing engine is explicit. A present but broken engine or invalid model
    fails closed. Unpinned defaults are fingerprinted, not declared authentic.
    Caller must compare before resume, and after extraction before checkpoint.
    """
    settings = asdict(config)
    # The binding is private job configuration; the fingerprint retains its
    # manifest identity without disclosing a local model installation path.
    settings.pop('model_pack_path', None)
    identity = dict(schema=1, config=settings, source_sha256=_source_hashes(),
                    packages={p: metadata.version(p) for p in ('pypdf', 'pypdfium2', 'Pillow')})
    deadline = time.monotonic() + config.timeout

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise IdentityError('OCR fingerprint timed out')
        return seconds

    try:
        ocr_languages.model_ids(config.languages)
        pack_path = getattr(config, 'model_pack_path', None)
        profile = getattr(config, 'model_profile', 'installed')
        profile_options = {'model_profile': profile} if profile != 'installed' else {}
        runtime = ocr_runtime.discover_ocr_runtime('eng' if pack_path else config.languages,
                                                  orientation=False if pack_path else config.orientation,
                                                  remaining=remaining, **profile_options)
        runtime = ocr_runtime.bind_model_pack(runtime, pack_path, getattr(config, 'model_pack_sha256', None),
                                              languages=config.languages, orientation=config.orientation,
                                              remaining=remaining)
        if runtime is None:
            identity['ocr'] = {'state': 'missing'}
            return identity
        exe = runtime.executable

        def run(arg):
            return run_ocr(runtime, [exe, arg], remaining=remaining).stdout.decode('utf-8')

        before = _hash(exe, remaining=remaining)
        version = run('--version').splitlines()[0]
        if not re.fullmatch(r'tesseract [0-9][A-Za-z0-9.\-+~]*', version):
            raise IdentityError('unexpected OCR version')
        report = run('--list-langs')
        if config.model_policy == POLICY:
            with tempfile.TemporaryDirectory() as tmp:
                _, base = stage_pinned_models(report, config.languages, Path(tmp) / 'base', remaining)
                models = dict(base['model_sha256'])
                if config.orientation:
                    _, osd = stage_pinned_models(report, 'osd', Path(tmp) / 'osd', remaining, osd=True)
                    models.update(osd['model_sha256'])
        else:
            match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.MULTILINE)
            if not match:
                raise IdentityError('cannot identify local model directory')
            selected = config.languages.split('+') + (['osd'] if config.orientation else [])
            models = {}
            for lang in selected:
                remaining()
                if not ocr_languages.valid_model_code(lang):
                    raise IdentityError('invalid model name')
                models[lang] = _hash(Path(match[1]) / (lang + '.traineddata'), remaining=remaining)
        if before != _hash(exe, remaining=remaining):
            raise IdentityError('OCR executable changed during fingerprint')
        runtime.assert_unchanged(remaining)
        remaining()
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        raise IdentityError('OCR runtime fingerprint unavailable') from None
    identity['ocr'] = dict(state='present', version=version, executable_sha256=before,
                           model_sha256=models, policy=config.model_policy)
    if runtime.portable:
        identity['ocr']['portable'] = runtime.fingerprint()
    if pack_path:
        identity['ocr']['model_pack'] = runtime.pack.fingerprint()
    if getattr(runtime, 'managed', False):
        identity['ocr']['managed_models'] = runtime.selection.fingerprint()
    return identity

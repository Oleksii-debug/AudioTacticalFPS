"""Discover real installed Tesseract languages; install explicit offline model packs.

No network, language-count promises, or script-as-language classification.
An independently supplied manifest binds local model bytes, not OCR correctness.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from ..durable_state import atomic_json, local_path, regular, read_json, sync_directory, _unique
from .ocr_runtime import discover_ocr_runtime, OcrRuntimeError, NON_READING_MODELS

CODE = re.compile(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,79}')
NAMES = {'eng': 'English', 'ukr': 'Ukrainian', 'slk': 'Slovak', 'ces': 'Czech',
         'deu': 'German', 'fra': 'French', 'spa': 'Spanish', 'pol': 'Polish',
         'hun': 'Hungarian', 'ita': 'Italian', 'por': 'Portuguese', 'rus': 'Russian',
         'ara': 'Arabic', 'heb': 'Hebrew', 'jpn': 'Japanese', 'kor': 'Korean',
         'chi_sim': 'Chinese, simplified', 'chi_tra': 'Chinese, traditional',
         'osd': 'Orientation and script detection (not a reading language)',
         'equ': 'Equation detection (not a reading language)'}


@dataclass(frozen=True)
class LanguageModel:
    code: str
    name: str
    kind: str


def installed_languages(runtime=None, *, timeout=15):
    runtime = runtime or discover_ocr_runtime()
    if runtime is None:
        raise OcrRuntimeError('Install or select a local Tesseract runtime first.')
    if runtime.portable:
        codes = runtime.languages
    else:
        with tempfile.TemporaryFile() as output:
            subprocess.run(runtime.command([runtime.executable, '--list-langs']),
                           stdout=output, stderr=subprocess.STDOUT, check=True,
                           timeout=timeout, cwd=runtime.root,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if output.tell() > 256 * 1024:
                raise OcrRuntimeError('The OCR model list exceeds its output budget.')
            output.seek(0)
            report = output.read().decode('utf-8', 'strict')
        codes = tuple(line.strip() for line in report.splitlines() if CODE.fullmatch(line.strip()))
    models = []
    for code in sorted(set(codes)):
        if not CODE.fullmatch(code):
            raise OcrRuntimeError('Unexpected installed OCR model identifier.')
        kind = ('orientation' if code == 'osd' else 'equation' if code == 'equ'
                else 'script' if code.startswith('script/') else 'language')
        models.append(LanguageModel(code, NAMES.get(code, code.replace('script/', 'Script: ')), kind))
    if not models:
        raise OcrRuntimeError('The OCR runtime has no installed language models.')
    return tuple(models)


def language_selection(codes, available, *, max_models=8):
    codes = tuple(codes)
    if not 1 <= len(codes) <= max_models or len(set(codes)) != len(codes):
        raise ValueError(f'Choose 1 to {max_models} distinct models for one OCR pass.')
    supported = {model.code for model in available
                 if model.kind in ('language', 'script') and model.code not in NON_READING_MODELS}
    if any(code not in supported for code in codes):
        raise ValueError('Choose installed reading languages or scripts; special detectors are separate.')
    return '+'.join(codes)


def save_preset(path, name, codes, *, available=None, dpi=300, orientation=False,
                automatic_languages=False):
    from .page_ocr import Config
    config = Config(languages=language_selection(codes, available or installed_languages()),
                    dpi=dpi, orientation=orientation, automatic_languages=automatic_languages)
    if not isinstance(name, str) or not name.strip() or len(name) > 256:
        raise ValueError('Enter a preset name.')
    path = local_path(path)
    presets = read_json(path) if path.exists() else {'schema': 1, 'presets': {}}
    if set(presets) != {'schema', 'presets'} or presets['schema'] != 1 or not isinstance(presets['presets'], dict):
        raise ValueError('Unsupported OCR preset file.')
    presets['presets'][name] = vars(config)
    from .ocr_languages import validate_presets_bytes
    from ..durable_state import encode
    validate_presets_bytes(encode(presets))
    atomic_json(path, presets, replace=path.exists())
    return config


def install_offline_pack(source, destination, *, expected_manifest_sha256, max_bytes=4 * 1024**3,
                         cancelled=lambda: False):
    """Copy a reviewed pack into a NEW tessdata directory, with no silent overwrite.

    Manifest schema 1: {schema, upstream, license, models: {code: {size,sha256}}}.
    Select this directory through a separately configured Tesseract installation.
    An app-managed portable runtime must be rebuilt with its complete manifest.
    """
    source = local_path(source, exists=True)
    destination = local_path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    regular(source, directory=True)
    manifest_path = source / 'models.json'
    manifest_limit = 2 * 1024 * 1024
    if regular(manifest_path).st_size > manifest_limit:
        raise ValueError('OCR model manifest exceeds its size budget.')
    with manifest_path.open('rb') as stream:
        raw = stream.read(manifest_limit + 1)
    if len(raw) > manifest_limit:
        raise ValueError('OCR model manifest grew beyond its size budget.')
    if not re.fullmatch('[0-9a-f]{64}', expected_manifest_sha256 or '') or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError('The independently supplied model manifest checksum does not match.')
    # Parse the approved snapshot; reopening the pathname could admit a replacement.
    manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite state number.')))
    if (set(manifest) != {'schema', 'upstream', 'license', 'models'} or manifest['schema'] != 1
            or not isinstance(manifest['upstream'], str) or not manifest['upstream']
            or not isinstance(manifest['license'], str) or not manifest['license']
            or not isinstance(manifest['models'], dict) or not 1 <= len(manifest['models']) <= 1024):
        raise ValueError('Unsupported offline OCR model manifest.')
    total = 0
    for code, item in manifest['models'].items():
        if (not CODE.fullmatch(code) or not isinstance(item, dict) or set(item) != {'size', 'sha256'}
                or type(item['size']) is not int or not 0 < item['size'] <= 512 * 1024**2
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid OCR model identity.')
        total += item['size']
    if total > max_bytes:
        raise ValueError('OCR pack exceeds the chosen disk budget.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ocr-pack-', dir=destination.parent) as temp:
        stage = Path(temp) / 'tessdata'
        stage.mkdir()
        for code, item in sorted(manifest['models'].items()):
            incoming = local_path(source / (code + '.traineddata'), root=source, exists=True)
            if regular(incoming).st_size != item['size']:
                raise ValueError('OCR model size mismatch.')
            target = stage / (code + '.traineddata')
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = sha256(), 0
            with incoming.open('rb') as read, target.open('xb') as write:
                while block := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('OCR pack installation cancelled.')
                    size += len(block)
                    if size > item['size']:
                        raise ValueError('OCR model changed while installing.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
            if size != item['size'] or digest.hexdigest() != item['sha256']:
                raise ValueError('OCR model checksum mismatch.')
        atomic_json(stage / 'models.json', manifest)
        # Exclusive destination creation prevents replacing a user's model tree.
        destination.mkdir(mode=0o700)
        try:
            for path in stage.rglob('*'):
                relative = path.relative_to(stage)
                if path.is_dir():
                    (destination / relative).mkdir(exist_ok=True)
                else:
                    (destination / relative).parent.mkdir(parents=True, exist_ok=True)
                    os.link(path, destination / relative)
            sync_directory(destination)
        except BaseException:
            # A partial directory is explicitly marked; it is never reported ready.
            atomic_json(destination / 'INSTALL-INCOMPLETE.json', {'schema': 1, 'status': 'incomplete'})
            raise
    return {'models': len(manifest['models']), 'size_bytes': total,
            'manifest_sha256': expected_manifest_sha256, 'path': str(destination)}

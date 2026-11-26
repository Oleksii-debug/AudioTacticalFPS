"""Local Tesseract model discovery and explicit, ordered OCR preferences.

The engine and portable inventory are always selected by ``ocr_runtime``.
Discovery does not download models or infer a document's language. A verified
bundle checksum is an integrity check, not a claim about publisher authenticity
or recognition quality. System models are labelled as such and fingerprinted
by the existing ingestion pipeline before any page can be published.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unicodedata
from uuid import uuid4

MAX_MODELS = 1024
MAX_SELECTED_MODELS = 8
MAX_REPORT_BYTES = 128 * 1024
PRESET_FILE = 'ocr-language-presets.json'
FILENAME = PRESET_FILE
MAX_PRESETS = 64
MAX_PRESET_BYTES = 64 * 1024
_MODEL = re.compile(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,63}\Z')
_AUXILIARY = {'osd', 'equ'}
_NAMES = {
    'ara': 'Arabic', 'bel': 'Belarusian', 'ben': 'Bengali', 'bul': 'Bulgarian',
    'cat': 'Catalan', 'ces': 'Czech', 'chi_sim': 'Chinese, simplified',
    'chi_sim_vert': 'Chinese, simplified vertical', 'chi_tra': 'Chinese, traditional',
    'chi_tra_vert': 'Chinese, traditional vertical', 'cym': 'Welsh', 'dan': 'Danish',
    'deu': 'German', 'deu_latf': 'German, Fraktur', 'ell': 'Greek', 'eng': 'English',
    'enm': 'Middle English', 'epo': 'Esperanto', 'est': 'Estonian', 'eus': 'Basque',
    'fas': 'Persian', 'fin': 'Finnish', 'fra': 'French', 'gle': 'Irish',
    'grc': 'Ancient Greek', 'heb': 'Hebrew', 'hin': 'Hindi', 'hrv': 'Croatian',
    'hun': 'Hungarian', 'hye': 'Armenian', 'ind': 'Indonesian', 'isl': 'Icelandic',
    'ita': 'Italian', 'jpn': 'Japanese', 'jpn_vert': 'Japanese, vertical',
    'kat': 'Georgian', 'kaz': 'Kazakh', 'kor': 'Korean', 'kor_vert': 'Korean, vertical',
    'lat': 'Latin', 'lav': 'Latvian', 'lit': 'Lithuanian', 'mar': 'Marathi',
    'mkd': 'Macedonian', 'msa': 'Malay', 'nld': 'Dutch', 'nor': 'Norwegian',
    'pol': 'Polish', 'por': 'Portuguese', 'ron': 'Romanian', 'rus': 'Russian',
    'slk': 'Slovak', 'slv': 'Slovenian', 'spa': 'Spanish', 'sqi': 'Albanian',
    'srp': 'Serbian', 'srp_latn': 'Serbian, Latin', 'swe': 'Swedish', 'tam': 'Tamil',
    'tel': 'Telugu', 'tha': 'Thai', 'tur': 'Turkish', 'ukr': 'Ukrainian',
    'urd': 'Urdu', 'vie': 'Vietnamese', 'yid': 'Yiddish',
    'osd': 'Orientation and script detection, not language recognition',
    'equ': 'Legacy equation detection, not language recognition',
}


class OcrLanguageError(ValueError):
    """An unavailable or unsafe model choice cannot start recognition."""


def valid_model_code(value):
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None:
        return False
    # These names address devices rather than traineddata files on Windows.
    name = value.rsplit('/', 1)[-1].casefold()
    return name not in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                        *(f'lpt{i}' for i in range(1, 10))}


def split_languages(value):
    """Accept only bounded CLI data, including official script/Name selectors."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SELECTED_MODELS * 72:
        raise OcrLanguageError('Choose at least one text recognition language or script.')
    codes = tuple(value.split('+'))
    if len(codes) > MAX_SELECTED_MODELS:
        raise OcrLanguageError(f'Choose at most {MAX_SELECTED_MODELS} models for one OCR pass.')
    if (any(not valid_model_code(code) for code in codes)
            or len({code.casefold() for code in codes}) != len(codes)):
        raise OcrLanguageError('Invalid or repeated text recognition model.')
    if any(code.casefold() in _AUXILIARY for code in codes):
        raise OcrLanguageError('OSD and equation detection are auxiliary models, not recognition languages.')
    return codes


def model_label(code):
    if code.startswith('script/'):
        return f'{code[7:].replace("_", " ")} script [{code}]'
    name = _NAMES.get(code)
    return f'{name} [{code}]' if name else f'Installed model [{code}]'


@dataclass(frozen=True)
class LanguageCatalog:
    codes: tuple[str, ...]
    engine_version: str = ''
    integrity: str = 'unavailable'
    manifest_sha256: str | None = None

    @property
    def selectable(self):
        return tuple(code for code in self.codes if code.casefold() not in _AUXILIARY)

    def matching(self, query=''):
        query = query.strip().casefold()
        return tuple(code for code in self.selectable if not query
                     or query in model_label(code).casefold())

    def validate(self, value, *, orientation=False, model_policy='default'):
        from .pinned_tessdata import MODELS, POLICY
        selected = split_languages(value)
        missing = tuple(code for code in selected if code not in self.selectable)
        if missing:
            raise OcrLanguageError('These OCR models are not installed: ' + ', '.join(missing) + '.')
        if type(orientation) is not bool or (orientation and 'osd' not in self.codes):
            raise OcrLanguageError('Orientation checking needs an installed OSD model; turn it off or install the model separately.')
        if model_policy not in ('default', POLICY):
            raise OcrLanguageError('Unsupported text recognition model profile.')
        if model_policy == POLICY and any(code not in MODELS for code in selected):
            raise OcrLanguageError('The pinned fast profile currently verifies only English, Ukrainian and Slovak.')
        return selected


def parse_language_report(report):
    """Read the documented --list-langs response, retaining recognition order."""
    if not isinstance(report, str) or len(report.encode('utf-8')) > MAX_REPORT_BYTES:
        raise OcrLanguageError('The OCR engine returned an oversized model inventory.')
    lines = tuple(line.strip() for line in report.splitlines() if line.strip())
    if not lines:
        raise OcrLanguageError('The OCR engine returned no model inventory.')
    header = re.fullmatch(r'List of available languages in "[^"\r\n]+" \(([0-9]{1,5})\):', lines[0])
    if header is None:
        raise OcrLanguageError('The OCR engine returned an unsupported model inventory.')
    codes = lines[1:]
    if (int(header[1]) != len(codes) or len(codes) > MAX_MODELS
            or any(not valid_model_code(code) for code in codes)
            or len({code.casefold() for code in codes}) != len(codes)):
        raise OcrLanguageError('The OCR engine returned an invalid model inventory.')
    return codes


def _capture(runtime, option, remaining):
    # A file-backed capture prevents a malformed native inventory from filling
    # Python's memory. The runtime remains bounded by the shared deadline.
    remaining()
    argv = runtime.command([runtime.executable, option])
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        with subprocess.Popen(argv, stdout=output, stderr=errors, cwd=runtime.root,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
            try:
                while True:
                    seconds = remaining()
                    if any(os.fstat(stream.fileno()).st_size > MAX_REPORT_BYTES for stream in (output, errors)):
                        raise OcrLanguageError('The OCR engine returned oversized output while checking installed models.')
                    try:
                        result = process.wait(timeout=min(seconds, 0.05))
                    except subprocess.TimeoutExpired:
                        continue
                    break
                remaining()
                if any(os.fstat(stream.fileno()).st_size > MAX_REPORT_BYTES for stream in (output, errors)):
                    raise OcrLanguageError('The OCR engine returned oversized output while checking installed models.')
                if result:
                    raise subprocess.CalledProcessError(result, argv)
                output.seek(0)
                raw = output.read(MAX_REPORT_BYTES + 1)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise
    return raw.decode('utf-8')


def discover_languages(*, timeout=10, cancelled=lambda: False,
                       model_pack_path=None, model_pack_sha256=None):
    """Inspect only the existing selected runtime; never download or fall back.

    Returned names mean installed inventory, not that every model has passed
    a recognition-quality test. Invalid explicitly selected bundles propagate
    their existing hard error and cannot become a system-runtime fallback.
    """
    from .ocr_runtime import bind_model_pack, discover_ocr_runtime
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise OcrLanguageError('Model discovery timeout must be between 0 and 30 seconds.')
    deadline = time.monotonic() + timeout

    def remaining():
        if cancelled():
            raise OcrLanguageError('Checking installed OCR models was cancelled.')
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise OcrLanguageError('Discovering installed OCR models timed out.')
        return seconds

    remaining()
    runtime = discover_ocr_runtime(remaining=remaining)
    runtime = bind_model_pack(runtime, model_pack_path, model_pack_sha256, remaining=remaining)
    if runtime is None:
        return LanguageCatalog(())
    try:
        version_lines = _capture(runtime, '--version', remaining).splitlines()
        version = version_lines[0] if version_lines else ''
        if not re.fullmatch(r'tesseract [0-9][A-Za-z0-9.\-+~]*', version):
            raise OcrLanguageError('The OCR engine returned an unsupported version.')
        codes = parse_language_report(_capture(runtime, '--list-langs', remaining))
        if (runtime.portable or model_pack_path) and set(codes) != set(runtime.languages):
            raise OcrLanguageError('Installed OCR models do not match the verified portable inventory.')
        runtime.assert_unchanged(remaining)
        remaining()
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise OcrLanguageError('Installed OCR models could not be read. Check the local engine and retry.') from exc
    return LanguageCatalog(codes, version,
                           'verified_model_pack' if model_pack_path else
                           ('portable_sha256' if runtime.portable else 'system_installation'),
                           model_pack_sha256 if model_pack_path else runtime.manifest_sha256)


def _name(value):
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 80
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)):
        raise OcrLanguageError('Use a preset name of 1 to 80 printable characters.')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OcrLanguageError('The saved OCR presets contain a repeated field.')
        result[key] = value
    return result


def validate_presets_bytes(raw):
    """Validate portable preference data without running OCR or opening sources."""
    from .pinned_tessdata import MODELS, POLICY
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PRESET_BYTES:
        raise OcrLanguageError('The saved OCR preset file is too large or empty.')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OcrLanguageError('The saved OCR presets are invalid.') from exc
    if (type(data) is not dict or set(data) != {'version', 'presets'}
            or type(data['version']) is not int or data['version'] not in (1, 2)
            or type(data['presets']) is not list or len(data['presets']) > MAX_PRESETS):
        raise OcrLanguageError('The saved OCR preset format is unsupported.')
    names = set()
    for item in data['presets']:
        fields = {'name', 'languages', 'model_policy', 'orientation'}
        pack_fields = {'model_pack_path', 'model_pack_sha256'}
        if (type(item) is not dict or set(item) not in
                (fields, fields | pack_fields) or data['version'] == 1 and set(item) != fields):
            raise OcrLanguageError('The saved OCR preset fields are invalid.')
        name = _name(item['name']).casefold()
        if name in names:
            raise OcrLanguageError('The saved OCR presets contain repeated names.')
        names.add(name)
        codes = split_languages(item['languages'])
        if (type(item['orientation']) is not bool or item['model_policy'] not in ('default', POLICY)
                or (item['model_policy'] == POLICY and any(code not in MODELS for code in codes))):
            raise OcrLanguageError('The saved OCR preset settings are invalid.')
        if pack_fields <= set(item):
            from .language_manager import validate_pack_selection
            validate_pack_selection(item['model_pack_path'], item['model_pack_sha256'])
            if item['model_pack_path'] is None or item['model_policy'] != 'default':
                raise OcrLanguageError('A saved verified OCR pack needs a path, checksum and installed-model profile.')
    return data


def _preset_path(root):
    from ..ingest.writer_guard import _private_root, _regular_if_present
    root = _private_root(root)
    path = root / PRESET_FILE
    _regular_if_present(path)
    return path


def load_presets(root):
    path = _preset_path(root)
    if not path.exists():
        return ()
    # The filename is application-owned, never selected from model output.
    if path.stat().st_size > MAX_PRESET_BYTES:
        raise OcrLanguageError('The saved OCR preset file is too large.')
    with path.open('rb') as source:
        raw = source.read(MAX_PRESET_BYTES + 1)
    return tuple(validate_presets_bytes(raw)['presets'])


def store_preset(root, name, config, *, replace=False):
    """Save one explicit choice atomically under the library's writer guard."""
    from ..ingest.writer_guard import exclusive_writer
    _name(name)
    item = {'name': name, 'languages': config.languages,
            'orientation': config.orientation, 'model_policy': config.model_policy}
    if getattr(config, 'model_pack_path', None) is not None:
        item.update(model_pack_path=config.model_pack_path, model_pack_sha256=config.model_pack_sha256)
    with exclusive_writer(root) as base:
        presets = list(load_presets(base))
        existing = next((i for i, old in enumerate(presets)
                         if old['name'].casefold() == name.casefold()), None)
        if existing is not None:
            if not replace:
                raise FileExistsError('An OCR preset with this name already exists.')
            presets[existing] = item
        else:
            presets.append(item)
        _write_presets(base, presets)


def delete_preset(root, name):
    from ..ingest.writer_guard import exclusive_writer
    _name(name)
    with exclusive_writer(root) as base:
        presets = list(load_presets(base))
        kept = [row for row in presets if row['name'].casefold() != name.casefold()]
        if len(kept) == len(presets):
            raise OcrLanguageError('That saved OCR preset no longer exists.')
        _write_presets(base, kept)


def _write_presets(root, presets):
    version = 2 if any('model_pack_path' in row for row in presets) else 1
    raw = (json.dumps({'version': version, 'presets': presets}, ensure_ascii=False,
                      sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    validate_presets_bytes(raw)
    path = _preset_path(root)
    temporary = root / ('.ocr-language-presets-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        _preset_path(root)
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)

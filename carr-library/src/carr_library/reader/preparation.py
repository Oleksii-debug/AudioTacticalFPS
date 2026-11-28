"""Explicit, source-bound reading preparation over reviewed optional engines.

Original text is never changed. A preview is tied to the complete input reading
revision and exact selected range; only an explicit apply creates a new reading
revision. Runtime manifests are installation decisions, never book instructions.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import time
import unicodedata

from ..audio.engines import _run, EngineUnavailable
from ..audio.jobs import file_identity, regular_file, verified_reader
from ..audio.model_manifest import LocalModelManifest, offline_environment
from .narration_text import decode_json

MAX_SELECTION = 64 * 1024
MAX_TOKENS = 2048
MAX_DISTINCT = 512
MAX_WORD_BYTES = 128
MAX_RESPONSE = 256 * 1024
MAX_RUNTIME_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_FILES = 384
LANGUAGES = ('en', 'uk', 'sk')
HUNSPELL_VERSION = '1.7.6'
HUNSPELL_REVISION = '15faca15532b34db3106d50f162e5edc2503d6d6'
NUM2WORDS_VERSION = '0.5.14'
BABEL_VERSION = '2.18.0'
WHEELS = {
    'num2words-0.5.14-py3-none-any.whl':
        '1c8e5b00142fc2966fd8d685001e36c4a9911e070d1b120e1beb721fa1edb33d',
    'babel-2.18.0-py3-none-any.whl':
        'e2b422b277c2b9a9630c1d7903c2a00d0830c409c59ac8cae9081c92f1aeba35',
}
_DIGEST = re.compile('[0-9a-f]{64}')


def _digest(text):
    if type(text) is not str or '\x00' in text:
        raise ValueError('Preparation text must be Unicode without NUL.')
    try:
        return sha256(text.encode('utf-8')).hexdigest()
    except UnicodeError:
        raise ValueError('Preparation text contains invalid Unicode.') from None


def _string(value, label, maximum=256, *, empty=False):
    if (type(value) is not str or len(value) > maximum or (not empty and not value)
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)):
        raise ValueError('Invalid preparation ' + label + '.')
    _digest(value)
    return value


@dataclass(frozen=True)
class PreparationContext:
    language: str
    kind: str
    number_mode: str = 'cardinal'
    date_order: str = 'ymd'
    unit_id: str = ''
    unit_symbol: str = ''
    gender: str = 'masculine'
    grammatical_case: str = 'nominative'
    voice_id: str = ''

    def __post_init__(self):
        if self.language not in LANGUAGES or type(self.language) is not str:
            raise ValueError('Choose the explicitly supported preparation context: en, uk or sk.')
        if self.kind not in ('number', 'date', 'unit', 'symbol'):
            raise ValueError('Choose number, date, unit or symbol preparation.')
        if self.number_mode not in ('cardinal', 'ordinal', 'digits', 'year'):
            raise ValueError('Choose an explicit number-reading mode.')
        if self.language == 'sk' and self.number_mode == 'ordinal':
            raise ValueError('The pinned Slovak num2words adapter does not implement ordinal readings.')
        if self.date_order not in ('ymd', 'dmy', 'mdy'):
            raise ValueError('Choose the explicit year/month/day order.')
        if self.gender not in ('masculine', 'feminine'):
            raise ValueError('Choose masculine or feminine number context.')
        if self.grammatical_case not in ('nominative', 'genitive', 'dative', 'accusative',
                                         'instrumental', 'locative'):
            raise ValueError('Choose a supported Ukrainian grammatical case.')
        _string(self.unit_id, 'unit identifier', 80, empty=True)
        _string(self.unit_symbol, 'written unit', 16, empty=True)
        _string(self.voice_id, 'voice identity', 2048, empty=True)
        if self.kind == 'unit':
            if (not re.fullmatch('[a-z]+-[a-z0-9-]+', self.unit_id)
                    or not self.unit_symbol or self.number_mode != 'cardinal'):
                raise ValueError('Unit preparation needs an exact CLDR unit, its written symbol and cardinal mode.')
        elif self.unit_id or self.unit_symbol:
            raise ValueError('Unit fields apply only to explicit unit preparation.')
        if self.kind == 'date' and self.number_mode != 'cardinal':
            raise ValueError('Date preparation uses explicit cardinal day/year readings; review its localized grammar.')
        if self.kind == 'symbol' and self.language != 'en':
            raise ValueError('Unicode symbol names are English only; use a scoped pronunciation for other languages.')
        if self.language != 'uk' and (self.gender != 'masculine' or self.grammatical_case != 'nominative'):
            raise ValueError('Gender and grammatical-case options are supported only by the Ukrainian cardinal adapter.')
        if (self.number_mode != 'cardinal' and
                (self.gender != 'masculine' or self.grammatical_case != 'nominative')):
            raise ValueError('The selected upstream ordinal/year/digit API has no grammatical-case controls.')


@dataclass(frozen=True)
class PreparationRecord:
    """Portable inert provenance: replayable edit, no executable paths."""
    engine: str
    engine_version: str
    manifest_sha256: str
    source_sha256: str
    input_sha256: str
    output_sha256: str
    start: int
    end: int
    before: str
    after: str
    settings: tuple

    def __post_init__(self):
        if self.engine not in ('hunspell', 'reading-preparation'):
            raise ValueError('Unsupported reading-preparation provenance.')
        _string(self.engine_version, 'engine version', 120)
        for value in (self.manifest_sha256, self.source_sha256, self.input_sha256, self.output_sha256):
            if type(value) is not str or not _DIGEST.fullmatch(value):
                raise ValueError('Reading-preparation provenance needs exact content digests.')
        if (type(self.start) is not int or type(self.end) is not int or
                not 0 <= self.start < self.end or self.end - self.start != len(self.before)):
            raise ValueError('Invalid preparation range.')
        for value in (self.before, self.after):
            _digest(value)
            if len(value.encode('utf-8')) > 16 * 1024:
                raise ValueError('A single preparation change exceeds its review budget.')
        if not self.after or self.before == self.after:
            raise ValueError('Preparation provenance must describe an actual nonempty change.')
        if (type(self.settings) is not tuple or not 1 <= len(self.settings) <= 16
                or any(type(row) is not tuple or len(row) != 2 for row in self.settings)):
            raise ValueError('Invalid preparation context inventory.')
        names = set()
        for name, value in self.settings:
            _string(name, 'context field', 64)
            _string(value, 'context value', 2048, empty=True)
            if name in names:
                raise ValueError('Duplicate preparation context field.')
            names.add(name)
        settings = dict(self.settings)
        if self.engine == 'reading-preparation':
            if set(settings) != set(PreparationContext.__dataclass_fields__):
                raise ValueError('Incomplete normalization context provenance.')
            PreparationContext(**settings)
        else:
            if set(settings) != {'language', 'voice_id', 'dictionary_id', 'dictionary_revision', 'dictionary_license'}:
                raise ValueError('Incomplete spell-check dictionary provenance.')
            if settings['language'] not in LANGUAGES:
                raise ValueError('Unsupported spell-check language context.')
            if not _DIGEST.fullmatch(settings['dictionary_revision']):
                raise ValueError('Spell-check dictionary provenance needs an exact revision digest.')

    def validate_revision(self, source_text, previous, current):
        if (self.source_sha256 != _digest(source_text) or
                self.input_sha256 != _digest(previous) or self.output_sha256 != _digest(current)
                or self.end > len(previous) or previous[self.start:self.end] != self.before
                or previous[:self.start] + self.after + previous[self.end:] != current):
            raise ValueError('Preparation provenance does not replay against the saved reading revisions.')


def decode_preparation(value):
    if value is None:
        return None
    fields = set(PreparationRecord.__dataclass_fields__)
    if type(value) is not dict or set(value) != fields or type(value['settings']) not in (list, tuple):
        raise ValueError('Invalid preparation provenance.')
    if any(type(row) not in (list, tuple) or len(row) != 2 for row in value['settings']):
        raise ValueError('Invalid preparation context inventory.')
    return PreparationRecord(**{**value, 'settings': tuple(tuple(row) for row in value['settings'])})


@dataclass(frozen=True)
class PreparationPreview:
    text: str
    record: PreparationRecord

    def description(self):
        value = dict(self.record.settings)
        return (f'{self.record.engine} {self.record.engine_version}; '
                f'language {value["language"]}; reading characters '
                f'[{self.record.start}, {self.record.end}).\n'
                f'Before: {self.record.before}\nAfter: {self.record.after}\n'
                f'Runtime/dictionary manifest SHA256: {self.record.manifest_sha256}\n'
                'Review pronunciation and grammar before applying. Original text is unchanged.')


@dataclass(frozen=True)
class SpellIssue:
    start: int
    end: int
    word: str
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class SpellReport:
    source_sha256: str
    input_sha256: str
    manifest_sha256: str
    engine_version: str
    settings: tuple
    issues: tuple[SpellIssue, ...]
    checked_words: int
    protected_words: int
    skipped_tokens: int


def _deadline(timeout, cancelled):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0.05 <= timeout <= 60 or not callable(cancelled)):
        raise ValueError('Preparation requires a finite 0.05–60 second budget and cancellation callback.')
    end = time.monotonic() + timeout
    def check():
        if cancelled():
            raise InterruptedError('Reading preparation cancelled. No partial edit was applied.')
        if time.monotonic() >= end:
            raise TimeoutError('Reading preparation exceeded its time budget. Original and reading text are unchanged.')
        return False
    def remaining():
        check()
        return max(0.001, end - time.monotonic())
    check()
    return check, remaining


def _selection(project, start, end, *, maximum=MAX_SELECTION):
    from .narration import NarrationProject
    if (type(project) is not NarrationProject or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(project.current_text)):
        raise ValueError('Select a nonempty range in the current reading copy.')
    text = project.current_text[start:end]
    if len(text.encode('utf-8')) > maximum:
        raise ValueError('Choose a smaller reading selection for this operation.')
    return text


def _context_manifest(path, engine, language, *, check):
    manifest = LocalModelManifest.load(path, engine=engine)
    if len(manifest.files) > MAX_RUNTIME_FILES:
        raise ValueError('The selected preparation runtime exceeds its file inventory budget.')
    manifest.verify(cancelled=check, max_asset_bytes=MAX_RUNTIME_BYTES, require_complete=True)
    settings = manifest.settings
    if engine == 'hunspell':
        required = {'dll', 'aff', 'dic', 'language', 'library_version', 'source_revision',
                    'dictionary_id', 'dictionary_revision', 'dictionary_license'}
        if (set(settings) != required or settings['language'] != language
                or settings['library_version'] != HUNSPELL_VERSION
                or settings['source_revision'] != HUNSPELL_REVISION):
            raise ValueError('Choose the reviewed Hunspell 1.7.6 runtime and a dictionary for this exact language.')
        for key in ('dll', 'aff', 'dic'):
            manifest.asset(settings[key])
        if (len({settings[key] for key in ('dll', 'aff', 'dic')}) != 3
                or not settings['aff'].endswith('.aff') or not settings['dic'].endswith('.dic')):
            raise ValueError('Choose distinct Hunspell DLL, AFF and DIC assets.')
        for key in ('dictionary_id', 'dictionary_license'):
            _string(settings[key], key, 256)
        if (type(settings['dictionary_revision']) is not str or
                not _DIGEST.fullmatch(settings['dictionary_revision'])):
            raise ValueError('Pin the reviewed dictionary revision digest.')
    else:
        required = {'packages', 'num2words_version', 'babel_version', 'languages', 'source_wheels'}
        if (set(settings) != required or settings['num2words_version'] != NUM2WORDS_VERSION
                or settings['babel_version'] != BABEL_VERSION or settings['languages'] != list(LANGUAGES)
                or settings['source_wheels'] != WHEELS):
            raise ValueError('Choose the reviewed num2words/Babel optional preparation pack.')
        manifest.directory(settings['packages'])
    return manifest


@contextmanager
def _opened_manifest(manifest, *, check):
    """Hold the existing immutable Windows readers throughout owned execution."""
    with ExitStack() as stack:
        rows = ((manifest.path, manifest.manifest_sha256),) + tuple(
            (manifest.asset(name), digest) for name, digest in manifest.files)
        for path, expected in rows:
            check()
            identity = file_identity(regular_file(path))
            stream = stack.enter_context(verified_reader(path, expected_identity=identity))
            actual = sha256()
            while chunk := stream.read(1024 * 1024):
                check()
                actual.update(chunk)
            if actual.hexdigest() != expected:
                raise ValueError('A preparation runtime asset changed after selection.')
        check()
        yield
        check()


def _call_worker(request, *, check, remaining):
    payload = json.dumps(request, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(payload) > MAX_RESPONSE:
        raise ValueError('Preparation request exceeds its byte budget.')
    try:
        raw = _run([sys.executable, '-I', '-B', '-S',
                    str(Path(__file__).with_name('preparation_worker.py'))],
                   data=payload, cancelled=check, timeout=remaining(), env=offline_environment())
    except EngineUnavailable:
        raise ValueError('The selected preparation worker could not complete; text is unchanged.') from None
    check()
    if len(raw) > MAX_RESPONSE:
        raise ValueError('Preparation response exceeds its byte budget.')
    result = decode_json(raw.decode('utf-8'))
    if type(result) is not dict:
        raise ValueError('Invalid preparation worker response.')
    if set(result) == {'error'}:
        messages = {
            'invalid-selection': 'The selected text is ambiguous or unsupported for this explicit context; it was preserved.',
            'unavailable': 'The selected optional runtime, dictionary encoding or version is unavailable.',
            'work-limit': 'Preparation exceeded its selected work budget; text was preserved.',
            'invalid-request': 'The optional preparation worker rejected its bounded request.',
        }
        raise ValueError(messages.get(result['error'], 'The preparation worker could not complete.')
                         if type(result['error']) is str else 'Invalid preparation worker error.')
    if result.pop('isolated', None) is not True or result.pop('no_site', None) is not True:
        raise ValueError('The optional worker did not confirm isolated execution without host site-packages.')
    return result


def _proposal(project, start, end, after, *, manifest, engine_version, settings):
    before = _selection(project, start, end, maximum=16 * 1024)
    _digest(after)
    if len(after.encode('utf-8')) > 16 * 1024 or not after or before == after:
        raise ValueError('The selected operation proposes no supported reading-text change.')
    text = project.current_text[:start] + after + project.current_text[end:]
    record = PreparationRecord(manifest.engine, engine_version, manifest.manifest_sha256,
        project.original_sha256, _digest(project.current_text), _digest(text),
        start, end, before, after, tuple(sorted(settings.items())))
    record.validate_revision(project.original_text, project.current_text, text)
    return PreparationPreview(text, record)


def prepare_selection(project, start, end, context, manifest_path, *,
                      cancelled=lambda: False, timeout=15.0):
    if type(context) is not PreparationContext:
        raise ValueError('Choose an explicit preparation context.')
    selected = _selection(project, start, end, maximum=512)
    check, remaining = _deadline(timeout, cancelled)
    manifest = _context_manifest(manifest_path, 'reading-preparation', context.language, check=check)
    # An explicit user pronunciation at this selected range always takes priority.
    if _protected_ranges(project, start, end, context.language, context.voice_id,
                         check=check, remaining=remaining):
        raise ValueError('A pronunciation override covers this selection. Review that scoped dictionary rule first.')
    with _opened_manifest(manifest, check=check):
        result = _call_worker(dict(version=1, operation='normalize', selected=selected,
            context=asdict(context), packages=str(manifest.directory(manifest.settings['packages']))),
            check=check, remaining=remaining)
    if (set(result) != {'replacement', 'engine_version', 'module_origins'} or type(result['replacement']) is not str
            or result['engine_version'] != 'num2words 0.5.14; Babel 2.18.0; Unicode ' + unicodedata.unidata_version):
        raise ValueError('Invalid normalization result identity.')
    expected = {'num2words': 'num2words/__init__.py', 'babel': 'babel/__init__.py',
                'babel.dates': 'babel/dates.py', 'babel.numbers': 'babel/numbers.py', 'babel.units': 'babel/units.py'}
    origins = result['module_origins']
    if type(origins) is not dict or set(origins) != set(expected):
        raise ValueError('The normalizer did not report its actual optional package origins.')
    inventory = dict(manifest.files)
    for name, relative in expected.items():
        row = origins[name]
        asset = manifest.settings['packages'] + '/' + relative
        if (type(row) is not dict or set(row) != {'path', 'sha256'} or type(row['path']) is not str
                or Path(row['path']) != manifest.asset(asset) or row['sha256'] != inventory.get(asset)):
            raise ValueError('The normalizer imported code outside its reviewed optional package selection.')
    return _proposal(project, start, end, result['replacement'], manifest=manifest,
                     engine_version=result['engine_version'], settings=asdict(context))


def _tokens(text):
    """Unicode word boundaries only; Hunspell performs all linguistic checking."""
    start = None
    for index in range(len(text) + 1):
        char = text[index] if index < len(text) else ''
        category = unicodedata.category(char) if char else ''
        continues = bool(char) and (category[0] in ('L', 'M', 'N') or char == '_'
                     or (char in ("'", '\u2019', '-') and start is not None
                         and index + 1 < len(text) and unicodedata.category(text[index + 1])[0] == 'L'))
        if continues and start is None:
            start = index
        elif not continues and start is not None:
            yield start, index, text[start:index]
            start = None


def _protected_ranges(project, start, end, language, voice_id, *, check, remaining):
    from .narration import _pattern_matches, validate_pronunciation_rules
    selected = [(index, rule) for index, rule in enumerate(validate_pronunciation_rules(project.rules))
                if (not rule.language_scope or rule.language.casefold() == language.casefold())
                and (not rule.voice_id or rule.voice_id == voice_id)]
    if not selected:
        return ()
    rows = _pattern_matches(project.current_text, selected,
                            timeout=min(1.0, remaining()), cancelled=check)
    return tuple((first, last) for first, last, _, _ in rows if first < end and last > start)


def spellcheck_selection(project, start, end, manifest_path, *, language, voice_id='',
                         cancelled=lambda: False, timeout=15.0):
    if language not in LANGUAGES:
        raise ValueError('Choose the exact dictionary language: en, uk or sk.')
    _string(voice_id, 'voice identity', 2048, empty=True)
    text = _selection(project, start, end)
    check, remaining = _deadline(timeout, cancelled)
    protected = _protected_ranges(project, start, end, language, voice_id, check=check, remaining=remaining)
    words, skipped, covered = [], 0, 0
    for first, last, word in _tokens(text):
        check()
        if len(words) + skipped + covered >= MAX_TOKENS:
            raise ValueError('Choose a smaller selection; spell-check token budget is full.')
        if (len(word.encode('utf-8')) > MAX_WORD_BYTES or not any(c.isalpha() for c in word)
                or any(c.isdigit() or c == '_' or ord(c) > 0xffff for c in word)):
            skipped += 1
        elif any(a <= start + first and b >= start + last for a, b in protected):
            covered += 1
        else:
            words.append((start + first, start + last, word))
    distinct = tuple(dict.fromkeys(word for _, _, word in words))
    if len(distinct) > MAX_DISTINCT:
        raise ValueError('Choose a smaller selection; spell-check distinct-word budget is full.')
    manifest = _context_manifest(manifest_path, 'hunspell', language, check=check)
    with _opened_manifest(manifest, check=check):
        result = _call_worker(dict(version=1, operation='spell', words=distinct,
            dll=str(manifest.asset(manifest.settings['dll'])),
            aff=str(manifest.asset(manifest.settings['aff'])),
            dic=str(manifest.asset(manifest.settings['dic'])),
            library_version=HUNSPELL_VERSION), check=check, remaining=remaining)
    if (set(result) != {'words', 'engine_version'} or result['engine_version'] != HUNSPELL_VERSION
            or type(result['words']) is not list or len(result['words']) != len(distinct)):
        raise ValueError('Invalid Hunspell result inventory.')
    wrong = {}
    for expected, row in zip(distinct, result['words']):
        if (type(row) is not dict or set(row) != {'word', 'correct', 'suggestions'}
                or row['word'] != expected or type(row['correct']) is not bool
                or type(row['suggestions']) is not list or len(row['suggestions']) > 16):
            raise ValueError('Invalid Hunspell word result.')
        for suggestion in row['suggestions']:
            _string(suggestion, 'spelling suggestion', 128)
        if len(set(row['suggestions'])) != len(row['suggestions']) or (row['correct'] and row['suggestions']):
            raise ValueError('Invalid Hunspell suggestion inventory.')
        if not row['correct']:
            wrong[expected] = tuple(row['suggestions'])
    settings = tuple(sorted(dict(language=language, voice_id=voice_id,
        **{key: manifest.settings[key] for key in
           ('dictionary_id', 'dictionary_revision', 'dictionary_license')}).items()))
    return SpellReport(project.original_sha256, _digest(project.current_text), manifest.manifest_sha256,
        HUNSPELL_VERSION, settings,
        tuple(SpellIssue(first, last, word, wrong[word]) for first, last, word in words if word in wrong),
        len(words), covered, skipped)


def spelling_proposal(project, report, index, replacement):
    if (type(report) is not SpellReport or report.source_sha256 != project.original_sha256
            or report.input_sha256 != _digest(project.current_text) or type(index) is not int
            or not 0 <= index < len(report.issues)):
        raise ValueError('The reading text changed; run spell-check again.')
    issue = report.issues[index]
    if replacement not in issue.suggestions or replacement == issue.word:
        raise ValueError('Choose a suggested spelling for this exact occurrence.')
    text = project.current_text[:issue.start] + replacement + project.current_text[issue.end:]
    record = PreparationRecord('hunspell', report.engine_version, report.manifest_sha256,
        report.source_sha256, report.input_sha256, _digest(text), issue.start, issue.end,
        issue.word, replacement, report.settings)
    record.validate_revision(project.original_text, project.current_text, text)
    return PreparationPreview(text, record)


def accepted_word_rule(report, index):
    """Keep personal acceptance in the existing project/language/voice dictionary."""
    from .narration import PronunciationRule
    if type(report) is not SpellReport or type(index) is not int or not 0 <= index < len(report.issues):
        raise ValueError('Choose one reported spelling.')
    context = dict(report.settings)
    word = report.issues[index].word
    return PronunciationRule(word, word, language=context['language'], voice_id=context['voice_id'],
                             language_scope=True)

"""Explicit, byte-bound local Hunspell review through an owned subprocess.

The selected native library is trusted executable code, not sandboxed code.
Nothing is discovered, downloaded, installed, or approved on the user's behalf.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory, gettempdir
import time
from uuid import uuid4
import unicodedata

from ._io import check_cancelled, local_file, text_value
from ..audio.cleanup import pending_cleanup
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending
from ..audio.process import run_process
from ..durable_state import local_path, regular
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest, ResourceTimeout

MAX_REQUEST_BYTES = 8_000_000
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_WORDS = 20_000
MAX_ISSUES = 500
MAX_WORD_LENGTH = 128
MAX_SUGGESTIONS = 10
FILE_BUDGETS = {'library': 64 * 1024 * 1024, 'affix': 16 * 1024 * 1024,
                'dictionary': 128 * 1024 * 1024}


@dataclass(frozen=True)
class SpellingFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class HunspellProfile:
    library: SpellingFile
    affix: SpellingFile
    dictionary: SpellingFile
    language: str
    provenance: str

    @property
    def identity(self):
        return sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class SpellingIssue:
    start: int
    end: int
    word: str
    suggestions: tuple[str, ...]
    reason: str = 'not_in_dictionary'


@dataclass(frozen=True)
class SpellingResult:
    profile: HunspellProfile
    text_sha256: str
    bounds: tuple[int, int]
    words: int
    encoding: str
    issues: tuple[SpellingIssue, ...]


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _review_file(path, role, cancelled):
    check_cancelled(cancelled)
    path = local_file(path)
    before = path.lstat()
    if not 0 < before.st_size <= FILE_BUDGETS[role]:
        raise ValueError(f'The selected Hunspell {role} is empty or exceeds its size budget.')
    digest, count = sha256(), 0
    with path.open('rb') as source:
        if _stamp(os.fstat(source.fileno())) != _stamp(before):
            raise ValueError('A selected Hunspell file changed while opening it.')
        while chunk := source.read(min(1024 * 1024, before.st_size + 1 - count)):
            check_cancelled(cancelled)
            count += len(chunk)
            if count > before.st_size:
                raise ValueError('A selected Hunspell file exceeds its size budget.')
            digest.update(chunk)
        if _stamp(os.fstat(source.fileno())) != _stamp(before):
            raise ValueError('A selected Hunspell file changed while reading it.')
    if count != before.st_size or _stamp(local_file(path).lstat()) != _stamp(before):
        raise ValueError('A selected Hunspell file changed while reading it.')
    return SpellingFile(str(path), digest.hexdigest(), count)


@contextmanager
def _admission(state_root, *, cancelled, guard, timeout, disk_bytes):
    """One existing broker lease; no alternate process or resource owner."""
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not .05 <= timeout <= 300 or not callable(guard)):
        raise ValueError('Spelling needs a finite .05–300 second budget and an owner guard.')
    check_cancelled(cancelled); guard()
    if state_root is None:
        raise ValueError('Select the current library before reviewing a spelling profile.')
    state = Path(state_root).absolute()
    if state.name != 'state':
        raise ValueError('Spelling requires the selected library state directory.')
    library = local_path(state.parent, exists=True)
    state = local_path(state, root=library, exists=True)
    temporary_root = local_path(gettempdir(), exists=True)
    identities = {}
    for path in (library, state, temporary_root):
        info = regular(path, directory=True)
        identities[path] = info.st_dev, info.st_ino
    deadline = time.monotonic() + timeout

    def check():
        check_cancelled(cancelled); guard()
        if time.monotonic() >= deadline:
            raise TimeoutError('The Hunspell review exceeded its time budget; select a smaller scope.')
        for path, identity in identities.items():
            info = regular(local_path(path, exists=True), directory=True)
            if (info.st_dev, info.st_ino) != identity:
                raise ValueError('The selected library or spelling staging directory changed.')
        return False

    check()
    broker = ResourceBroker.for_app(library, cancelled=check)
    request = ResourceRequest(cpu_slots=1, memory_bytes=1024**3,
        disk_bytes=disk_bytes, disk_path=temporary_root)
    try:
        with broker.reserve(request, operation_id='spelling:' + uuid4().hex,
                            cancelled=check, timeout=min(60, timeout),
                            run_timeout=timeout) as lease:
            def admitted_check():
                check(); lease.checkpoint()
                return False
            admitted_check()
            yield lease, admitted_check, temporary_root
            admitted_check()
    except (ResourceCancelled, AudioCancelled) as error:
        raise InterruptedError('Spelling review cancelled; no correction was applied.') from error
    except ResourceTimeout as error:
        raise TimeoutError('The spelling review could not finish within its resource allowance.') from error


def review_hunspell(library, affix, dictionary, *, language, provenance,
                    state_root=None, cancelled=lambda: False, guard=lambda: None, timeout=30):
    """Hash the user's explicit selections without loading executable code."""
    text_value(language, 'Dictionary language', maximum=128)
    text_value(provenance, 'Dictionary and engine provenance / license notice', maximum=4096)
    with _admission(state_root, cancelled=cancelled, guard=guard, timeout=timeout, disk_bytes=0) as (_, check, _):
        files = tuple(_review_file(path, role, check) for path, role in
                      ((library, 'library'), (affix, 'affix'), (dictionary, 'dictionary')))
        if len({item.path for item in files}) != 3:
            raise ValueError('Select distinct library, affix and dictionary files.')
        check()
        return HunspellProfile(*files, language.strip(), provenance.strip())


def _validated_profile(profile):
    if not isinstance(profile, HunspellProfile):
        raise ValueError('Review an explicitly selected Hunspell profile first.')
    text_value(profile.language, 'Dictionary language', maximum=128)
    text_value(profile.provenance, 'Dictionary and engine provenance / license notice', maximum=4096)
    for role in FILE_BUDGETS:
        value = getattr(profile, role)
        if (not isinstance(value, SpellingFile) or type(value.size) is not int
                or not 0 < value.size <= FILE_BUDGETS[role]
                or type(value.sha256) is not str or len(value.sha256) != 64
                or any(c not in '0123456789abcdef' for c in value.sha256)):
            raise ValueError('The reviewed Hunspell file identity is invalid.')
        if str(local_file(value.path)) != value.path:
            raise ValueError('Use an absolute reviewed local Hunspell path.')
    if len({getattr(profile, role).path for role in FILE_BUDGETS}) != 3:
        raise ValueError('Select distinct library, affix and dictionary files.')
    return profile


def word_ranges(text, *, bounds=None, cancelled=lambda: False):
    """Exact Unicode letter/mark ranges; this is not universal word segmentation."""
    text_value(text, 'Reading text', empty=True)
    bounds = (0, len(text)) if bounds is None else bounds
    if (type(bounds) is not tuple or len(bounds) != 2
            or any(type(value) is not int for value in bounds)
            or not 0 <= bounds[0] <= bounds[1] <= len(text)):
        raise ValueError('Choose an existing reading-text scope.')
    start, end = bounds
    words, position = [], start
    while position < end:
        if position % 1024 == 0:
            check_cancelled(cancelled)
        if not unicodedata.category(text[position]).startswith('L'):
            position += 1
            continue
        first = position
        position += 1
        while position < end:
            if position % 1024 == 0:
                check_cancelled(cancelled)
            char = text[position]
            if unicodedata.category(char)[0] in ('L', 'M'):
                position += 1
            elif (char in "'-’‐" and position + 1 < end
                  and unicodedata.category(text[position + 1]).startswith('L')):
                position += 1
            else:
                break
        if len(words) >= MAX_WORDS:
            raise ValueError('Select a smaller spelling scope with at most 20,000 words.')
        words.append((first, position, text[first:position]))
    check_cancelled(cancelled)
    return bounds, tuple(words)


def check_spelling(text, profile, *, bounds=None, state_root=None, cancelled=lambda: False,
                   guard=lambda: None, timeout=30):
    """Check an immutable text/file snapshot under the current library policy."""
    check_cancelled(cancelled); guard()
    profile = _validated_profile(profile)
    bounds, words = word_ranges(text, bounds=bounds, cancelled=cancelled)
    if not words:
        raise ValueError('The selected reading scope contains no letter-based words.')
    unique = tuple(dict.fromkeys(word for _, _, word in words if len(word) <= MAX_WORD_LENGTH))
    stage_budget = sum(getattr(profile, role).size for role in FILE_BUDGETS) + MAX_REQUEST_BYTES
    with _admission(state_root, cancelled=cancelled, guard=guard, timeout=timeout,
                    disk_bytes=stage_budget + 2 * MAX_REPLY_BYTES + 16 * 1024**2) as (lease, check, parent):
        temporary = TemporaryDirectory(prefix='resonance-hunspell-', dir=parent)
        root = Path(temporary.name).absolute()
        try:
            check()
            identity = regular(root, directory=True)
            staging = {'path': str(root), 'device': identity.st_dev, 'inode': identity.st_ino}
            request = json.dumps({'profile': asdict(profile), 'words': unique, 'staging': staging},
                                 ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(request) > MAX_REQUEST_BYTES:
                raise ValueError('The spelling request exceeds its byte allowance.')
            with (root / 'request.json').open('xb') as stream:
                stream.write(request)
            environment = os.environ.copy()
            for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE', '__PYVENV_LAUNCHER__'):
                environment.pop(name, None)
            environment['PYTHONDONTWRITEBYTECODE'] = '1'
            worker = Path(__file__).with_name('spelling_worker.py')
            try:
                result = run_process([str(Path(sys.executable).absolute()), '-I', '-B', '-S', str(worker),
                    sha256(request).hexdigest()], cwd=root, env=environment, cancelled=check,
                    budget=AudioBudget(timeout=timeout, max_output_bytes=stage_budget,
                        max_log_bytes=MAX_REPLY_BYTES, reserve_disk_bytes=16 * 1024**2,
                        max_inputs=1, threads=1, max_staging_files=4),
                    watch=(root,), owned_windows_job=os.name == 'nt', allow_failure=True)
            except AudioCleanupPending as error:
                # This is the existing process owner and lease. Keep every staged
                # DLL/dictionary byte alive until that owner confirms quiescence.
                lease.settle_cleanup(error)
                raise
            check()
            answer = _result(text, profile, bounds, words, unique, request,
                             result.stdout, result.stderr, result.returncode, check)
            for role in FILE_BUDGETS:
                if _review_file(getattr(profile, role).path, role, check) != getattr(profile, role):
                    raise ValueError('A selected Hunspell file changed. Review the profile again.')
            check()
        finally:
            try:
                temporary.cleanup()
            except Exception as error:
                # The retained existing cleanup owner also holds the native
                # resource reservation until this filesystem cleanup completes.
                raise pending_cleanup(temporary.cleanup,
                    message='Private spelling-copy cleanup remains pending.') from error
        check()
        return answer


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate spelling result field.')
        result[key] = value
    return result


def _result(text, profile, bounds, words, unique, request, raw, diagnostics, code, cancelled):
    if diagnostics:
        raise ValueError('Hunspell reported a dictionary or runtime warning. Review the selected files before accepting a result.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_json)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('The selected Hunspell engine did not return a complete review.') from exc
    if code or (type(value) is dict and 'error' in value):
        known = {'changed': 'A selected Hunspell file changed. Review the profile again.',
                 'unavailable': 'The selected Hunspell library could not load its C API. Use a compatible standalone local library.',
                 'dictionary': 'Hunspell could not open the selected dictionary or its declared encoding.',
                 'budget': 'The spelling result exceeds its review budget. Select a smaller scope.',
                 'invalid': 'The selected Hunspell profile or returned suggestions are invalid.'}
        reason = value.get('error') if type(value) is dict else None
        raise ValueError(known.get(reason, 'The selected Hunspell engine did not complete its review.'))
    if (type(value) is not dict or set(value) != {'encoding', 'checked', 'issues', 'request_sha256'}
            or value['request_sha256'] != sha256(request).hexdigest()
            or type(value['checked']) is not int or value['checked'] != len(unique)
            or type(value['issues']) is not list or len(value['issues']) > MAX_ISSUES):
        raise ValueError('The Hunspell worker returned an invalid review.')
    text_value(value['encoding'], 'Hunspell dictionary encoding', maximum=128)
    by_word = {}
    for row in value['issues']:
        if (type(row) is not dict or set(row) != {'index', 'reason', 'suggestions'}
                or type(row['index']) is not int or not 0 <= row['index'] < len(unique)
                or row['reason'] not in ('not_in_dictionary', 'unencodable')
                or type(row['suggestions']) is not list or len(row['suggestions']) > MAX_SUGGESTIONS):
            raise ValueError('The Hunspell worker returned an invalid issue.')
        word = unique[row['index']]
        if word in by_word or (row['reason'] == 'unencodable' and row['suggestions']):
            raise ValueError('The Hunspell worker returned conflicting issues.')
        suggestions = tuple(text_value(item, 'Spelling suggestion', maximum=MAX_WORD_LENGTH)
                            for item in row['suggestions'])
        if len(set(suggestions)) != len(suggestions):
            raise ValueError('The Hunspell worker returned duplicate suggestions.')
        by_word[word] = row['reason'], suggestions
    issues = []
    for first, last, word in words:
        check_cancelled(cancelled)
        reason, suggestions = ('too_long', ()) if len(word) > MAX_WORD_LENGTH else by_word.get(word, ('', ()))
        if reason:
            if len(issues) >= MAX_ISSUES:
                raise ValueError('More than 500 spelling issues occur in this scope. Select a smaller scope.')
            issues.append(SpellingIssue(first, last, word, suggestions, reason))
    return SpellingResult(profile, sha256(text.encode('utf-8')).hexdigest(), bounds, len(words),
                          value['encoding'], tuple(issues))

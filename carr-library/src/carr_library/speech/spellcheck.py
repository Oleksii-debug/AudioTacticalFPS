"""Explicit local Hunspell-pair review; original sources and dictionaries stay read-only."""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import gettempdir
from time import monotonic
from uuid import uuid4

from ..audio.cleanup import retained_temporary_directory
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError
from ..audio.process import run_process
from ..durable_state import local_path, regular
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
from ._io import text_value

MAX_AFF_BYTES = 2 * 1024**2
MAX_DIC_BYTES = 16 * 1024**2
MAX_REPLY = 512 * 1024
BACKEND_VERSION = '0.1.7+carr.1'


@dataclass(frozen=True)
class SpellBudget:
    timeout: float = 30
    memory_bytes: int = 768 * 1024**2
    max_tokens: int = 3000
    max_issues: int = 200
    max_suggestions: int = 8

    def __post_init__(self):
        if type(self.timeout) not in (int, float) or not .05 <= self.timeout <= 120:
            raise ValueError('Spelling timeout must be between 0.05 and 120 seconds.')
        for name, minimum, maximum in (
                ('memory_bytes', 256 * 1024**2, 2 * 1024**3),
                ('max_tokens', 1, 10000), ('max_issues', 1, 1000), ('max_suggestions', 1, 16)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f'Invalid spelling {name} budget.')


def _check(cancelled):
    if not callable(cancelled):
        raise ValueError('A spelling cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Spelling review cancelled; the reading copy is unchanged.')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, maximum, cancelled, *, timeout=5, allow_empty=False):
    """Bound one descriptor and recheck the selected pathname after reading."""
    _check(cancelled)
    path = local_path(path, exists=True)
    before = regular(path)
    if not (0 if allow_empty else 1) <= before.st_size <= maximum:
        raise ValueError('The selected spelling file is empty or exceeds its byte budget.')
    deadline = monotonic() + timeout
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOINHERIT', 0))
    descriptor = os.open(path, flags)
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    chunks, size = [], 0
    with stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _stamp(opened) != _stamp(before)):
            raise ValueError('The selected spelling file changed while opening.')
        while True:
            _check(cancelled)
            if monotonic() >= deadline:
                raise TimeoutError('Reading the spelling file exceeded its time allowance.')
            block = stream.read(min(65536, before.st_size + 1 - size))
            if not block:
                break
            chunks.append(block); size += len(block)
            if size > before.st_size:
                raise ValueError('The selected spelling file changed while reading.')
        if size != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('The selected spelling file changed while reading.')
    if local_path(path, exists=True) != path or _stamp(regular(path)) != _stamp(before):
        raise ValueError('The selected spelling file changed while reading.')
    _check(cancelled)
    return b''.join(chunks), _stamp(before)


@dataclass(frozen=True)
class DictionaryIdentity:
    aff_path: Path
    dic_path: Path
    aff_sha256: str
    dic_sha256: str
    aff_stamp: tuple
    dic_stamp: tuple


def _capture_pair(selected, cancelled):
    path = local_path(selected, exists=True)
    if path.suffix.lower() not in ('.aff', '.dic'):
        raise ValueError('Choose an existing Hunspell .aff or .dic file with its same-name companion.')
    aff, dic = path.with_suffix('.aff'), path.with_suffix('.dic')
    if path.suffix.lower() == '.aff':
        aff = path
    else:
        dic = path
    aff, dic = local_path(aff, exists=True), local_path(dic, exists=True)
    a, astamp = _read(aff, MAX_AFF_BYTES, cancelled)
    d, dstamp = _read(dic, MAX_DIC_BYTES, cancelled)
    if _stamp(regular(aff)) != astamp:
        raise ValueError('The spelling dictionary pair changed while being captured.')
    identity = DictionaryIdentity(aff, dic, sha256(a).hexdigest(), sha256(d).hexdigest(), astamp, dstamp)
    return identity, a, d


def verify_dictionary(identity, *, cancelled=lambda: False):
    if not isinstance(identity, DictionaryIdentity):
        raise ValueError('A captured spelling dictionary is required.')
    current, _, _ = _capture_pair(identity.aff_path, cancelled)
    if current != identity:
        raise ValueError('The spelling dictionary changed. Review the current pair again.')


def _backend(cancelled):
    try:
        distribution = importlib.metadata.distribution('spylls')
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError('Install the reviewed optional spelling component before choosing a dictionary.') from exc
    if distribution.version != BACKEND_VERSION:
        raise ValueError('Spelling requires the reviewed code-only Spylls component (0.1.7+carr.1).')
    manifest_path = Path(__file__).with_name('spellcheck_backend.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = manifest['provenance']['unchanged_python_sha256']
    root = Path(distribution.locate_file('')).resolve(strict=True)
    for relative, expected in files.items():
        raw, _ = _read(root / relative, 256 * 1024, cancelled, allow_empty=True)
        if sha256(raw).hexdigest() != expected:
            raise ValueError('The installed spelling component differs from its reviewed source.')
    return {'root': str(root), 'version': BACKEND_VERSION, 'files': files,
            'wheel_sha256': manifest['sha256']}


@dataclass(frozen=True)
class SpellingIssue:
    start: int
    end: int
    word: str
    suggestions: tuple[str, ...]


@dataclass(frozen=True)
class SpellingReview:
    text_sha256: str
    dictionary: DictionaryIdentity
    issues: tuple[SpellingIssue, ...]
    checked_tokens: int
    total_tokens: int
    complete: bool
    backend_version: str = BACKEND_VERSION
    notice: str = ''


def review_spelling(text, selected_dictionary, *, library_root, cancelled=lambda: False,
                    budget=None, broker=None):
    """Review at most 200,000 characters in an owned, cancellable local child.

    The shared policy reserves memory on all platforms. POSIX additionally
    enforces address-space/CPU/file-size limits in the child. Windows uses the
    existing owned Job Object and deadline; its memory declaration is admission,
    not a claim of a per-process OS memory sandbox.
    """
    budget = budget or SpellBudget()
    if not isinstance(budget, SpellBudget):
        raise ValueError('A spelling budget is required.')
    text_value(text, 'Spelling review scope', maximum=200000)
    _check(cancelled)
    backend = _backend(cancelled)
    root = local_path(library_root, exists=True)
    if broker is None:
        broker = ResourceBroker.for_app(root, cancelled=cancelled)
    temporary_root = local_path(gettempdir(), exists=True)
    request = ResourceRequest(cpu_slots=1, memory_bytes=budget.memory_bytes,
        disk_path=temporary_root, disk_bytes=MAX_AFF_BYTES + MAX_DIC_BYTES + 4 * 1024**2)
    lease = None
    try:
        with ExitStack() as reservations:
            try:
                lease = reservations.enter_context(broker.reserve(request,
                    operation_id='spellcheck:' + uuid4().hex, cancelled=cancelled,
                    timeout=min(30, budget.timeout), run_timeout=budget.timeout + 10))
                stopping = lambda: cancelled() or lease.cancelled()
                _check(stopping)
                identity, aff, dic = _capture_pair(selected_dictionary, stopping)
                with retained_temporary_directory(prefix='carr-spelling-', dir=temporary_root) as name:
                    stage = Path(name)
                    (stage / 'dictionary.aff').write_bytes(aff)
                    (stage / 'dictionary.dic').write_bytes(dic)
                    payload = {'schema': 1, 'text': text, 'budget': asdict(budget), 'backend': backend,
                        'aff_sha256': identity.aff_sha256, 'dic_sha256': identity.dic_sha256}
                    (stage / 'request.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                    worker = Path(__file__).with_name('spell_worker.py').resolve(strict=True)
                    worker_raw, _ = _read(worker, 128 * 1024, stopping)
                    worker_hash = sha256(worker_raw).hexdigest()
                    # Execute the exact installed/source-selected helper, never a
                    # cwd import or a second checkout chosen by the test harness.
                    bootstrap = ('import hashlib,pathlib,runpy,sys; p=pathlib.Path(sys.argv.pop(1)); '
                        'h=sys.argv.pop(1)\n'
                        'if hashlib.sha256(p.read_bytes()).hexdigest()!=h: '
                        "raise ValueError('The spelling worker changed before launch.')\n"
                        "sys.argv[0]=str(p); runpy.run_path(str(p),run_name='__main__')")
                    try:
                        result = run_process([sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap,
                            str(worker), worker_hash, str(stage / 'request.json')],
                            budget=AudioBudget(timeout=budget.timeout, threads=1, max_log_bytes=MAX_REPLY,
                                max_output_bytes=request.disk_bytes, reserve_disk_bytes=0, max_staging_files=4),
                            watch=(stage,), owned_tree=True, cancelled=stopping, cwd=stage)
                    except AudioCleanupPending:
                        raise
                    except AudioCancelled as exc:
                        raise InterruptedError('Spelling review cancelled; the reading copy is unchanged.') from exc
                    except AudioError as exc:
                        raise ValueError('Spelling review could not finish within its worker limits. '
                                         'Check the dictionary or review a smaller passage. ' + str(exc)) from exc
                    _check(stopping)
                    value = json.loads(result.stdout.decode('utf-8'))
                    review = _decode(value, text, identity, budget, backend, worker_hash)
                    verify_dictionary(identity, cancelled=stopping)
            except AudioCleanupPending as exc:
                held = reservations.pop_all()
                exc.cleanup_owner.add_quiescent_callback(held.close)
                raise
    except ResourceCancelled as exc:
        raise InterruptedError('Spelling review cancelled while waiting for shared resources.') from exc
    _check(cancelled)
    notice = ('The shared reservation was released; its metadata awaits cleanup.'
              if lease is not None and lease.cleanup_pending else '')
    return SpellingReview(review.text_sha256, identity, review.issues, review.checked_tokens,
                          review.total_tokens, review.complete, notice=notice)


def _decode(value, text, identity, budget, backend, worker_hash):
    keys = {'schema', 'text_sha256', 'aff_sha256', 'dic_sha256', 'worker_sha256',
            'backend_version', 'backend_wheel_sha256', 'issues', 'checked_tokens', 'total_tokens', 'complete'}
    if (type(value) is not dict or set(value) != keys or type(value['schema']) is not int or value['schema'] != 1
            or value['text_sha256'] != sha256(text.encode('utf-8')).hexdigest()
            or value['aff_sha256'] != identity.aff_sha256 or value['dic_sha256'] != identity.dic_sha256
            or value['worker_sha256'] != worker_hash or value['backend_version'] != BACKEND_VERSION
            or value['backend_wheel_sha256'] != backend['wheel_sha256']
            or type(value['total_tokens']) is not int or not 0 <= value['total_tokens'] <= budget.max_tokens
            or type(value['checked_tokens']) is not int
            or not 0 <= value['checked_tokens'] <= value['total_tokens']
            or type(value['complete']) is not bool
            or value['complete'] != (value['checked_tokens'] == value['total_tokens'])
            or type(value['issues']) is not list or len(value['issues']) > budget.max_issues):
        raise ValueError('The spelling worker returned an invalid or unbound review.')
    previous, issues = 0, []
    for row in value['issues']:
        if (type(row) is not list or len(row) != 4 or type(row[0]) is not int or type(row[1]) is not int
                or not previous <= row[0] < row[1] <= len(text) or text[row[0]:row[1]] != row[2]
                or type(row[3]) is not list or len(row[3]) > budget.max_suggestions):
            raise ValueError('The spelling worker returned invalid reading positions.')
        suggestions = []
        for replacement in row[3]:
            text_value(replacement, 'Spelling suggestion', maximum=128)
            if any(ord(c) < 32 for c in replacement) or replacement in suggestions:
                raise ValueError('The spelling worker returned invalid suggestions.')
            suggestions.append(replacement)
        previous = row[1]
        issues.append(SpellingIssue(row[0], row[1], row[2], tuple(suggestions)))
    return SpellingReview(value['text_sha256'], identity, tuple(issues), value['checked_tokens'],
                          value['total_tokens'], value['complete'])

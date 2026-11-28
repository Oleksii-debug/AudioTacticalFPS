"""Bounded read-only spelling previews from explicitly selected Windows languages."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import uuid

from ..audio.cleanup import AudioCleanupPending
from ..audio.models import AudioBudget, AudioCancelled, AudioError
from ..audio.process import run_process

MAX_TEXT = 50000
MAX_ERRORS = 256
MAX_LANGUAGES = 256
MAX_SUGGESTIONS = 12
MAX_REPLACEMENT = 256
MAX_REQUEST_BYTES = 512 * 1024
MAX_REPLY_BYTES = 1024 * 1024
_LANGUAGE = re.compile(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,7}\Z')
_ERRORS = {
    'not_windows': 'Installed Windows spelling dictionaries require Windows.',
    'unavailable': 'The installed Windows spell checker is unavailable.',
    'unsupported_language': 'This exact language is not supported by an installed spelling dictionary.',
    'invalid_request': 'The spelling request is invalid.',
    'capacity': 'This passage exceeds the spelling review limit; select a shorter passage.',
    'native_failure': 'The installed Windows spell checker could not complete this request.',
}
_BOOTSTRAP = (
    "import hashlib,sys; p,h=sys.argv[1:3]; "
    "f=open(p,'rb'); b=f.read(65537); f.close(); "
    "assert len(b)<=65536 and hashlib.sha256(b).hexdigest()==h,'Spelling worker identity changed'; "
    "sys.argv=[p,*sys.argv[3:]]; "
    "exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
)


class SpellingError(RuntimeError):
    """No correction was applied when checking or validation failed."""


@dataclass(frozen=True)
class SpellingIssue:
    start: int
    end: int
    word: str
    action: int
    replacements: tuple[str, ...]
    suggestions_limited: bool = False


@dataclass(frozen=True)
class SpellingPreview:
    language: str
    text_sha256: str
    issues: tuple[SpellingIssue, ...]


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum
            or (not empty and not value) or '\x00' in value
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('Spelling requires bounded, valid Unicode text without NUL.')
    return value


def _language(value):
    if type(value) is not str or len(value) > 64 or not _LANGUAGE.fullmatch(value):
        raise ValueError('Select one explicit installed spelling language.')
    return value


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SpellingError('The spelling worker returned duplicate fields.')
        value[key] = item
    return value


def _utf16_boundaries(text):
    positions, units = {0: 0}, 0
    for index, character in enumerate(text, 1):
        units += 2 if ord(character) > 0xFFFF else 1
        positions[units] = index
    return positions


def _decode_reply(raw, request):
    if type(raw) is not bytes or not raw or len(raw) > MAX_REPLY_BYTES:
        raise SpellingError('The spelling worker exceeded its reply limit.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise SpellingError('The spelling worker returned invalid data.') from None
    base = {'version', 'request_id', 'operation', 'text_sha256', 'language', 'status'}
    if (type(value) is not dict or value.get('status') not in ('ok', 'error')
            or set(value) != base | ({'result'} if value.get('status') == 'ok' else {'error'})
            or type(value.get('version')) is not int
            or any(value.get(key) != request[key] for key in
                   ('version', 'request_id', 'operation', 'text_sha256', 'language'))):
        raise SpellingError('The spelling reply does not match this exact request.')
    if value['status'] == 'error':
        if type(value['error']) is not str or value['error'] not in _ERRORS:
            raise SpellingError('The spelling worker returned an unknown failure.')
        raise SpellingError(_ERRORS[value['error']])
    result = value['result']
    if request['operation'] == 'languages':
        if type(result) is not list or len(result) > MAX_LANGUAGES:
            raise SpellingError('The installed spelling language list is invalid.')
        try:
            result = tuple(_language(item) for item in result)
        except ValueError:
            raise SpellingError('The installed spelling language list is invalid.') from None
        if len({v.casefold() for v in result}) != len(result):
            raise SpellingError('The installed spelling language list is ambiguous.')
        return tuple(sorted(result, key=str.casefold))
    if type(result) is not list or len(result) > MAX_ERRORS:
        raise SpellingError('The spelling issue list is invalid.')
    positions = _utf16_boundaries(request['text'])
    issues, previous = [], 0
    for row in result:
        if (type(row) is not dict or set(row) !=
                {'start_utf16', 'length_utf16', 'action', 'replacements', 'suggestions_limited'}
                or type(row['start_utf16']) is not int or type(row['length_utf16']) is not int
                or row['length_utf16'] <= 0 or type(row['action']) is not int
                or row['action'] not in (0, 1, 2, 3)
                or type(row['suggestions_limited']) is not bool
                or row['suggestions_limited'] and row['action'] != 1
                or type(row['replacements']) is not list
                or len(row['replacements']) > MAX_SUGGESTIONS):
            raise SpellingError('The spelling issue is invalid.')
        start, end = positions.get(row['start_utf16']), positions.get(
            row['start_utf16'] + row['length_utf16'])
        if start is None or end is None or start < previous or end <= start:
            raise SpellingError('The spelling issue has invalid or overlapping text offsets.')
        try:
            replacements = tuple(_text(v, MAX_REPLACEMENT, empty=True) for v in row['replacements'])
        except ValueError:
            raise SpellingError('The spelling suggestion is invalid.') from None
        if (row['action'] == 0 and replacements or row['action'] == 2 and len(replacements) != 1
                or row['action'] == 3 and replacements != ('',)):
            raise SpellingError('The spelling correction action is invalid.')
        issues.append(SpellingIssue(start, end, request['text'][start:end],
                                    row['action'], tuple(dict.fromkeys(replacements)),
                                    row['suggestions_limited']))
        previous = end
    return SpellingPreview(request['language'], request['text_sha256'], tuple(issues))


class WindowsSpelling:
    """Use one owned, time-bounded worker per review; never mutate dictionaries."""

    def __init__(self, *, timeout=20.0):
        if type(timeout) not in (int, float) or not .05 <= timeout <= 60:
            raise ValueError('Spelling timeout must be between 0.05 and 60 seconds.')
        self.timeout = float(timeout)

    def languages(self, *, cancelled=lambda: False):
        return self._request('languages', '', None, cancelled=cancelled)

    def check(self, text, language, *, cancelled=lambda: False):
        _text(text, MAX_TEXT)
        if not text.strip():
            raise ValueError('Select a nonempty passage for spelling review.')
        return self._request('check', text, _language(language), cancelled=cancelled)

    def _request(self, operation, text, language, *, cancelled):
        if cancelled():
            raise InterruptedError('Spelling review cancelled.')
        request = {'version': 1, 'request_id': uuid.uuid4().hex, 'operation': operation,
            'text': text, 'text_sha256': sha256(text.encode('utf-8')).hexdigest(), 'language': language}
        raw = json.dumps(request, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('Select a shorter passage for spelling review.')
        worker = Path(__file__).with_name('spelling_worker.py').absolute()
        with worker.open('rb') as source:
            code = source.read(65537)
        if len(code) > 65536:
            raise SpellingError('The spelling worker exceeds its source limit.')
        directory = Path(tempfile.mkdtemp(prefix='carr-spelling-'))
        pending = False
        try:
            request_path = directory / 'request.json'
            with request_path.open('xb') as output:
                if os.name == 'posix':
                    os.fchmod(output.fileno(), 0o600)
                output.write(raw)
            args = [os.path.abspath(sys.executable), '-I', '-B', '-X', 'utf8', '-c',
                    _BOOTSTRAP, str(worker), sha256(code).hexdigest(),
                    str(request_path), sha256(raw).hexdigest()]
            try:
                result = run_process(args, budget=AudioBudget(timeout=self.timeout,
                    max_output_bytes=2 * 1024 * 1024, max_log_bytes=MAX_REPLY_BYTES,
                    reserve_disk_bytes=0, max_staging_files=4),
                    cancelled=cancelled, watch=(directory,), cwd=str(directory), owned_tree=True)
            except AudioCleanupPending as exc:
                pending = True
                exc.cleanup_owner.add_quiescent_callback(
                    lambda: shutil.rmtree(directory, ignore_errors=True))
                raise
            except AudioCancelled:
                raise InterruptedError('Spelling review cancelled.') from None
            except AudioError:
                raise SpellingError('The isolated spelling worker failed or exceeded its time allowance.') from None
            if cancelled():
                raise InterruptedError('Spelling review cancelled.')
            return _decode_reply(result.stdout, request)
        finally:
            # Keep the private text alive only while its exact owned child still
            # requires it; a failed termination must not delete a live workspace.
            if not pending:
                shutil.rmtree(directory)

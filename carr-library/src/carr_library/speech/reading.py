"""Reversible narration derivatives that cannot mutate authenticated source text.

The offset map is maintained from explicit edits. It never reclassifies a
rewritten passage as a verified source quotation merely because it looks alike.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import difflib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import sys

from ._io import check_cancelled, digest, local_file, new_output, run_process, text_value, _sync_directory

MAX_HISTORY = 500
MAX_PROJECT_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class SourceSpan:
    start: int | None
    end: int | None
    exact: bool


@dataclass(frozen=True)
class MappedRun:
    text: str
    source_start: int | None
    source_end: int | None
    exact: bool


@dataclass(frozen=True)
class PronunciationRule:
    pattern: str
    replacement: str
    regex: bool = False
    ignore_case: bool = False
    language: str = ''
    voice_id: str = ''

    def __post_init__(self):
        text_value(self.pattern, 'pronunciation pattern', maximum=1024)
        text_value(self.replacement, 'pronunciation replacement', maximum=4096, empty=True)
        text_value(self.language, 'language', maximum=128, empty=True)
        text_value(self.voice_id, 'voice id', maximum=2048, empty=True)
        if type(self.regex) is not bool or type(self.ignore_case) is not bool:
            raise ValueError('Pronunciation pattern flags must be booleans.')


def _split(runs, offset):
    before, after, cursor = [], [], 0
    for run in runs:
        end = cursor + len(run.text)
        if end <= offset:
            before.append(run)
        elif cursor >= offset:
            after.append(run)
        else:
            at = offset - cursor
            if run.exact:
                before.append(MappedRun(run.text[:at], run.source_start, run.source_start + at, True))
                after.append(MappedRun(run.text[at:], run.source_start + at, run.source_end, True))
            else:
                before.append(MappedRun(run.text[:at], run.source_start, run.source_end, False))
                after.append(MappedRun(run.text[at:], run.source_start, run.source_end, False))
        cursor = end
    return tuple(before), tuple(after)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate reading-project field.')
        value[key] = item
    return value


def _validated_source_anchor(value, text):
    """Validate a retained-source locator as data, not as quotation authority.

    The library still must authorize and resolve each ReadingLocation. A portable
    file may be edited externally; this checks its internal structure and exact
    snapshot slices, not the truth of its claimed library identity.
    """
    from ..reader.reading_state import ReadingLocation

    if (type(value) is not dict or set(value) != {'workspace', 'edition', 'revision',
            'asset_sha256', 'positions', 'quotation_authority'}
            or value['quotation_authority'] is not False
            or type(value['positions']) is not list
            or not 1 <= len(value['positions']) <= 10000):
        raise ValueError('Invalid reading-copy source anchor.')
    previous = None
    for row in value['positions']:
        if (type(row) is not dict or set(row) != {'ordinal', 'kind', 'index',
                'start', 'end', 'text_sha256', 'text_verified_at_capture'}
                or type(row['ordinal']) is not int or not 1 <= row['ordinal'] <= 10000
                or type(row['start']) is not int or type(row['end']) is not int
                or not 0 <= row['start'] <= row['end'] <= len(text)
                or type(row['text_verified_at_capture']) is not bool):
            raise ValueError('Invalid reading-copy source position.')
        ReadingLocation(value['workspace'], value['edition'], value['revision'],
                        row['kind'], row['index'], row['text_sha256'], value['asset_sha256'])
        if previous is None:
            if row['start'] != 0:
                raise ValueError('The first source position must begin the reading snapshot.')
        elif (row['ordinal'] != previous['ordinal'] + 1
              or row['kind'] != previous['kind'] or row['index'] <= previous['index']
              or row['start'] != previous['end'] + 2
              or text[previous['end']:row['start']] != '\n\n'):
            raise ValueError('Invalid source position order or narration separator.')
        if sha256(text[row['start']:row['end']].encode('utf-8')).hexdigest() != row['text_sha256']:
            raise ValueError('Reading-copy source position checksum mismatch.')
        previous = row
    if previous['end'] != len(text):
        raise ValueError('Unmapped text follows the last source position.')
    return deepcopy(value)


def _original_runs(text, anchor):
    if anchor is None:
        return (MappedRun(text, 0, len(text), True),) if text else ()
    runs, cursor = [], 0
    for row in anchor['positions']:
        start, end = row['start'], row['end']
        if start > cursor:
            # The inserted inter-position separator is narration structure only.
            runs.append(MappedRun(text[cursor:start], None, None, False))
        if end > start:
            runs.append(MappedRun(text[start:end], start, end, True))
        cursor = end
    return tuple(runs)


def _import_pre_release_anchor(value, text):
    """Read the earlier candidate dialect once; all writes use canonical v2."""
    text_value(text, 'source text', empty=True)
    fields = {'schema', 'workspace', 'edition', 'revision', 'asset_sha256',
              'positions', 'derived_separators', 'quotation_authority'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'carr.narration-source.v1'
            or type(value['positions']) is not list
            or not 1 <= len(value['positions']) <= 10000):
        raise ValueError('Unsupported pre-release reading-copy source format.')
    positions = []
    for row in value['positions']:
        if type(row) is not dict or set(row) != {'ordinal', 'kind', 'index',
                'start', 'end', 'text_sha256', 'text_verified_at_read'}:
            raise ValueError('Invalid or mixed pre-release reading-copy source position.')
        positions.append({**{key: item for key, item in row.items()
                             if key != 'text_verified_at_read'},
                          'text_verified_at_capture': row['text_verified_at_read']})
    anchor = _validated_source_anchor({
        **{key: value[key] for key in ('workspace', 'edition', 'revision',
                                      'asset_sha256', 'quotation_authority')},
        'positions': positions}, text)
    separators = value['derived_separators']
    expected = [{'start': first['end'], 'end': second['start']}
                for first, second in zip(positions, positions[1:])]
    if (type(separators) is not list or len(separators) != len(expected)
            or any(type(row) is not dict or set(row) != {'start', 'end'}
                   or type(row['start']) is not int or type(row['end']) is not int
                   for row in separators) or separators != expected):
        raise ValueError('Invalid pre-release narration separators.')
    return anchor


class ReadingDocument:
    """An editable reading copy with persistent undo/redo, outside evidence APIs."""
    def __init__(self, source_text, *, title='Reading copy', source_path=None, source_anchor=None):
        self._source = text_value(source_text, 'source text', empty=True)
        self.title = text_value(title, 'reading-copy title', maximum=512)
        self._source_hash = sha256(self._source.encode('utf-8')).hexdigest()
        self._source_path = Path(source_path).absolute() if source_path is not None else None
        self._source_anchor = (_validated_source_anchor(source_anchor, self._source)
                               if source_anchor is not None else None)
        self._anchor_signature = json.dumps(self._source_anchor, sort_keys=True, ensure_ascii=False)
        self._states = [_original_runs(self._source, self._source_anchor)]
        self._edits = []
        self._position = 0
        self._saved_path = None
        self._saved_digest = None
        self._saved_signature = None
        self._pre_release_format = False

    @property
    def source_text(self):
        return self._source

    @property
    def source_sha256(self):
        return self._source_hash

    @property
    def source_anchor(self):
        """A copy of the claimed origin, or None for a legacy/standalone copy."""
        return deepcopy(self._source_anchor)

    @property
    def format_notice(self):
        """Explain an explicit import without changing the original saved file."""
        return ('Recovered a pre-release reading-project format. The original file is preserved; '
                'use Save As to write the current format. ' if self._pre_release_format else '')

    @property
    def text(self):
        return ''.join(run.text for run in self._states[self._position])

    @property
    def revision(self):
        return self._position

    @property
    def can_undo(self):
        return self._position > 0

    @property
    def can_redo(self):
        return self._position < len(self._edits)

    @property
    def dirty(self):
        return self._signature() != self._saved_signature

    def _signature(self):
        return (self.title, self._source_hash, self._anchor_signature, self._position,
                tuple((e['start'], e['end'], e['replacement'], e['label']) for e in self._edits))

    def _range(self, start, end):
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(self.text):
            raise ValueError('Choose an existing character range in the reading copy.')

    def source_span(self, start, end):
        """Map local snapshot offsets only; exact does not mean quotation-approved."""
        self._range(start, end)
        if start == end:
            return SourceSpan(None, None, False)
        _, tail = _split(self._states[self._position], start)
        selected, _ = _split(tail, end - start)
        known = [run for run in selected if run.source_start is not None]
        if not known:
            return SourceSpan(None, None, False)
        exact = (all(run.exact for run in selected)
                 and all(a.source_end == b.source_start for a, b in zip(selected, selected[1:])))
        return SourceSpan(min(run.source_start for run in known), max(run.source_end for run in known), exact)

    def replace(self, start, end, replacement, *, label='Edit reading copy'):
        self._range(start, end)
        text_value(replacement, 'replacement', empty=True)
        text_value(label, 'edit label', maximum=256)
        current = self.text
        if current[start:end] == replacement:
            return False
        if len(current) - (end - start) + len(replacement) > 2_000_000:
            raise ValueError('This reading-copy edit exceeds its character budget. Split it into chapters.')
        if self._position >= MAX_HISTORY:
            raise ValueError('This reading copy has reached its edit-history budget; export a new reading copy.')
        origin = self.source_span(start, end)
        before, tail = _split(self._states[self._position], start)
        _, after = _split(tail, end - start)
        middle = (MappedRun(replacement, origin.start, origin.end, False),) if replacement else ()
        self._states = self._states[:self._position + 1]
        self._edits = self._edits[:self._position]
        self._states.append(before + middle + after)
        self._edits.append({'start': start, 'end': end, 'replacement': replacement, 'label': label})
        self._position += 1
        return True

    def set_text(self, value, *, label='Edit reading copy'):
        """Use longest unchanged prefix/suffix, avoiding quadratic text matching."""
        text_value(value, 'reading text', empty=True)
        current = self.text
        start, overlap = 0, min(len(current), len(value))
        while start < overlap and current[start] == value[start]:
            start += 1
        suffix = 0
        while suffix < overlap - start and current[len(current) - suffix - 1] == value[len(value) - suffix - 1]:
            suffix += 1
        return self.replace(start, len(current) - suffix,
                            value[start:len(value) - suffix if suffix else None], label=label)

    def undo(self):
        if not self.can_undo:
            return False
        self._position -= 1
        return True

    def redo(self):
        if not self.can_redo:
            return False
        self._position += 1
        return True

    def cleanup(self, *, join_hyphenated=False, normalize_spaces=True):
        """Deterministic suggestion only; caller can preview, undo or reject it."""
        value = self.text
        if join_hyphenated:
            value = re.sub(r'(?<=\w)-\r?\n(?=\w)', '', value)
        if normalize_spaces:
            value = re.sub(r'[^\S\r\n]+', ' ', value)
        return self.set_text(value, label='Reading-only whitespace/hyphenation cleanup')

    def reject_changes(self):
        self._position = 0

    def diff(self):
        # Line-diff preview is bounded; huge one-line OCR remains one replace.
        return ''.join(difflib.unified_diff(self.source_text.splitlines(keepends=True),
            self.text.splitlines(keepends=True), fromfile='Immutable source', tofile='Reading copy'))

    def apply_pronunciations(self, rules, *, language='', voice_id='', cancelled=lambda: False, timeout=1):
        rules = tuple(rules)
        if len(rules) > 1000 or any(not isinstance(rule, PronunciationRule) for rule in rules):
            raise ValueError('Use at most 1000 validated pronunciation rules.')
        # Compute on a private clone first: errors/cancel never leave partial edits.
        clone = ReadingDocument(self.text)
        for rule in sorted(rules, key=lambda r: bool(r.language) + 2 * bool(r.voice_id)):
            check_cancelled(cancelled)
            if rule.language and rule.language != language:
                continue
            if rule.voice_id and rule.voice_id != voice_id:
                continue
            if rule.regex:
                payload = json.dumps({'text': clone.text, 'pattern': rule.pattern,
                    'replacement': rule.replacement, 'ignore_case': rule.ignore_case}, ensure_ascii=False).encode('utf-8')
                reply, _, _ = run_process([sys.executable, '-I', '-B', '-X', 'utf8', '-S', str(Path(__file__).with_name('regex_worker.py'))],
                                          input_bytes=payload, timeout=timeout, cancelled=cancelled)
                matches = json.loads(reply.decode('utf-8'))['matches']
                result, position = [], 0
                for start, end, replacement in matches:
                    result.extend((clone.text[position:start], replacement))
                    position = end
                result.append(clone.text[position:])
                value = ''.join(result)
            else:
                flags = re.IGNORECASE if rule.ignore_case else 0
                value = re.sub(re.escape(rule.pattern), lambda match: rule.replacement, clone.text, flags=flags)
            clone.set_text(value)
        check_cancelled(cancelled)
        return self.set_text(clone.text, label='Pronunciation reading layer')

    def to_dict(self):
        data = {'schema': 'carr.reading-copy.v1', 'title': self.title, 'source_text': self._source,
                'source_sha256': self._source_hash, 'edits': [dict(edit) for edit in self._edits],
                'revision': self._position}
        if self._source_anchor is not None:
            data['schema'] = 'carr.reading-copy.v2'
            data['source_anchor'] = self.source_anchor
        return data

    def save(self, path, *, cancelled=lambda: False):
        """Create or update this very project with optimistic concurrency checks."""
        check_cancelled(cancelled)
        target = local_file(path, must_exist=False)
        if target == self._source_path:
            raise ValueError('Save the reading copy separately from the original source.')
        if self._pre_release_format and target == self._saved_path:
            raise ValueError('Use Save As to preserve the original pre-release reading project.')
        payload = (json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if len(payload) > MAX_PROJECT_BYTES:
            raise ValueError('The reading-project history exceeds its storage budget.')
        if not target.exists():
            with new_output(target, cancelled=cancelled) as staged:
                staged.write_bytes(payload)
        else:
            if target != self._saved_path or digest(target) != self._saved_digest:
                raise FileExistsError('The destination already exists or changed externally. Use Save As.')
            expected = (target.stat().st_dev, target.stat().st_ino, target.stat().st_mtime_ns)
            temporary = None
            try:
                with NamedTemporaryFile('wb', dir=target.parent, prefix='.reading-', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                check_cancelled(cancelled)
                checked = local_file(target)
                if ((checked.stat().st_dev, checked.stat().st_ino, checked.stat().st_mtime_ns) != expected
                        or digest(checked) != self._saved_digest):
                    raise ValueError('The reading project changed while saving. Use Save As.')
                os.replace(temporary, target)
                _sync_directory(target.parent)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        self._saved_path, self._saved_digest = target, sha256(payload).hexdigest()
        self._saved_signature = self._signature()
        self._pre_release_format = False
        return self._saved_digest

    @classmethod
    def load(cls, path):
        target = local_file(path)
        if target.stat().st_size > MAX_PROJECT_BYTES:
            raise ValueError('The reading project exceeds its storage budget.')
        raw = target.read_bytes()
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        document = cls.from_dict(data)
        document._saved_path, document._saved_digest = target, sha256(raw).hexdigest()
        document._saved_signature = document._signature()
        return document

    @classmethod
    def from_dict(cls, data):
        """Validate an exported reading object without approving its source."""
        fields = {'schema', 'title', 'source_text', 'source_sha256', 'edits', 'revision'}
        if not isinstance(data, dict):
            raise ValueError('Invalid reading-project fields.')
        version = data.get('schema')
        pre_release = False
        if version == 'carr.reading-copy.v1' and set(data) == fields:
            anchor = None
        elif (version == 'carr.reading-copy.v2' and set(data) == fields | {'source_anchor'}
                and type(data['source_anchor']) is dict):
            anchor = data['source_anchor']
            if 'schema' in anchor:
                anchor = _import_pre_release_anchor(anchor, data['source_text'])
                pre_release = True
        else:
            raise ValueError('Unsupported reading-project version or fields.')
        document = cls(data['source_text'], title=data['title'], source_anchor=anchor)
        if document.source_sha256 != data['source_sha256']:
            raise ValueError('Reading-project original-source checksum mismatch.')
        if not isinstance(data['edits'], list) or len(data['edits']) > MAX_HISTORY:
            raise ValueError('Invalid reading-project history.')
        for edit in data['edits']:
            if not isinstance(edit, dict) or set(edit) != {'start', 'end', 'replacement', 'label'}:
                raise ValueError('Invalid reading-project edit.')
            if not document.replace(edit['start'], edit['end'], edit['replacement'], label=edit['label']):
                raise ValueError('The reading-project history contains an empty edit.')
        if type(data['revision']) is not int or not 0 <= data['revision'] <= len(data['edits']):
            raise ValueError('Invalid reading-project revision.')
        document._position = data['revision']
        document._pre_release_format = pre_release
        return document

    def export(self, path, *, original=False, cancelled=lambda: False):
        target = local_file(path, must_exist=False)
        if target == self._source_path or target == self._saved_path:
            raise ValueError('Export to a new text file, separate from the original and reading project.')
        value = self.source_text if original else self.text
        with new_output(target, cancelled=cancelled) as staged:
            staged.write_bytes(value.encode('utf-8'))
        return digest(target)


def export_dictionary(rules, path, *, cancelled=lambda: False):
    check_cancelled(cancelled)
    rules = tuple(rules)
    if len(rules) > 1000 or any(not isinstance(rule, PronunciationRule) for rule in rules):
        raise ValueError('Use at most 1000 validated pronunciation rules.')
    payload = json.dumps({'schema': 'carr.pronunciations.v1', 'rules': [asdict(rule) for rule in rules]}, ensure_ascii=False)
    with new_output(path, cancelled=cancelled) as temporary:
        temporary.write_text(payload, encoding='utf-8')


def import_dictionary(path):
    source = local_file(path)
    if source.stat().st_size > 8_000_000:
        raise ValueError('The pronunciation dictionary exceeds its size budget.')
    data = json.loads(source.read_text(encoding='utf-8'), object_pairs_hook=_unique)
    if not isinstance(data, dict) or set(data) != {'schema', 'rules'} or data['schema'] != 'carr.pronunciations.v1':
        raise ValueError('Unsupported pronunciation dictionary.')
    if not isinstance(data['rules'], list) or len(data['rules']) > 1000:
        raise ValueError('Invalid pronunciation rule count.')
    try:
        return tuple(PronunciationRule(**row) for row in data['rules'])
    except (TypeError, KeyError) as exc:
        raise ValueError('Invalid pronunciation dictionary fields.') from exc

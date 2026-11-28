"""Original text, source mapping and durable narration-copy regression tests."""
from hashlib import sha256
import json
from pathlib import Path
import time

import pytest

from carr_library.speech import (ReadingDocument, PronunciationRule, SourceSpan,
                                 export_dictionary, import_dictionary)


def _bound_project():
    from carr_library.speech.source import NarrationSource
    text = 'ABC\n\nDEF'
    source = NarrationSource(text, 'Source', 'library', 'edition', 'revision', (
        {'ordinal': 1, 'kind': 'physical_page', 'index': 3, 'start': 0, 'end': 3,
         'text_sha256': sha256(b'ABC').hexdigest(), 'text_verified_at_capture': False},
        {'ordinal': 2, 'kind': 'physical_page', 'index': 5, 'start': 5, 'end': 8,
         'text_sha256': sha256(b'DEF').hexdigest(), 'text_verified_at_capture': True}), (), 'a' * 64)
    return source.reading_document()


def test_legacy_v1_copy_remains_readable_and_explicitly_unbound():
    value = {'schema': 'carr.reading-copy.v1', 'title': 'Legacy reading copy',
             'source_text': 'ABC', 'source_sha256': sha256(b'ABC').hexdigest(),
             'edits': [{'start': 1, 'end': 2, 'replacement': 'X', 'label': 'Legacy edit'}],
             'revision': 1}
    legacy = ReadingDocument.from_dict(value)
    assert legacy.text == 'AXC' and legacy.source_anchor is None
    assert legacy.to_dict() == value
    assert legacy.undo() and legacy.text == 'ABC'


def test_source_anchor_is_detached_and_does_not_invent_quotation_authority():
    bound = _bound_project()
    anchor = bound.source_anchor
    anchor['workspace'] = 'other'
    anchor['positions'][0]['text_verified_at_capture'] = True
    anchor['positions'][1]['start'] = 0
    exported = bound.to_dict()
    exported['source_anchor']['positions'][0]['text_sha256'] = '0' * 64
    assert bound.source_anchor['workspace'] == 'library'
    assert bound.source_anchor['positions'][0]['text_verified_at_capture'] is False
    assert bound.source_anchor['positions'][1]['start'] == 5
    assert set(bound.source_anchor) == {'workspace', 'edition', 'revision', 'asset_sha256',
                                       'positions', 'quotation_authority'}
    assert bound.source_anchor['quotation_authority'] is False
    assert ReadingDocument.from_dict(bound.to_dict()).source_anchor == bound.source_anchor
    assert bound.source_span(0, 3) == SourceSpan(0, 3, True)
    assert bound.source_span(3, 5) == SourceSpan(None, None, False)


@pytest.mark.parametrize('path,value', [
    (('workspace',), ''), (('workspace',), 'other\nworkspace'),
    (('asset_sha256',), 'not-a-hash'), (('quotation_authority',), True),
    (('quotation_authority',), 0), (('positions',), []),
    (('positions', 0, 'start'), True), (('positions', 0, 'end'), 4),
    (('positions', 0, 'ordinal'), False), (('positions', 1, 'ordinal'), 4),
    (('positions', 1, 'kind'), 'logical_segment'), (('positions', 1, 'index'), 3),
    (('positions', 0, 'text_sha256'), '0' * 64),
    (('positions', 0, 'text_verified_at_capture'), 'true'),
    (('derived_separators',), []), (('positions', 1, 'start'), False),
])
def test_damaged_or_authoritative_source_anchor_is_rejected(path, value):
    data = _bound_project().to_dict()
    target = data['source_anchor']
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(data)


@pytest.mark.parametrize('change', ['v1-with-anchor', 'v2-without-anchor', 'v2-null-anchor'])
def test_reading_schema_never_silently_discards_source_binding(change):
    data = _bound_project().to_dict()
    if change == 'v1-with-anchor':
        data['schema'] = 'carr.reading-copy.v1'
    elif change == 'v2-without-anchor':
        del data['source_anchor']
    else:
        data['source_anchor'] = None
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(data)


def _pre_release_project():
    document = _bound_project()
    document.replace(0, 1, 'Edited')
    document.replace(len(document.text), len(document.text), '!')
    document.undo()
    value = document.to_dict()
    anchor = value['source_anchor']
    anchor['schema'] = 'carr.narration-source.v1'
    anchor['derived_separators'] = [{'start': 3, 'end': 5}]
    for position in anchor['positions']:
        position['text_verified_at_read'] = position.pop('text_verified_at_capture')
    return value, document


def test_pre_release_project_import_preserves_history_and_requires_save_as(tmp_path):
    value, original = _pre_release_project()
    old_path = tmp_path / 'earlier.reading.json'
    old_path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    before = old_path.read_bytes()
    imported = ReadingDocument.load(old_path)
    assert 'pre-release' in imported.format_notice and 'Save As' in imported.format_notice
    assert imported.to_dict() == original.to_dict()
    assert imported.source_text == original.source_text and imported.text == original.text
    assert imported.source_anchor == original.source_anchor
    assert imported.can_redo and imported.redo() and imported.text.endswith('!')
    assert imported.undo() and imported.text == original.text
    assert not imported.source_span(0, len(imported.text)).exact
    with pytest.raises(ValueError, match='Save As'):
        imported.save(old_path)
    assert old_path.read_bytes() == before
    current_path = tmp_path / 'current.reading.json'
    imported.save(current_path)
    assert not imported.format_notice
    assert old_path.read_bytes() == before
    reopened = ReadingDocument.load(current_path)
    assert not reopened.format_notice
    assert reopened.to_dict() == original.to_dict()


@pytest.mark.parametrize('mutation', ['schema', 'mixed', 'separators', 'boolean-separator',
                                    'authority', 'hash', 'text'])
def test_invalid_pre_release_format_is_not_silently_migrated(tmp_path, mutation):
    value, _original = _pre_release_project()
    anchor = value['source_anchor']
    if mutation == 'schema':
        anchor['schema'] = 'unknown-source-format'
    elif mutation == 'mixed':
        anchor['positions'][0]['text_verified_at_capture'] = False
    elif mutation == 'separators':
        anchor['derived_separators'] = []
    elif mutation == 'boolean-separator':
        anchor['derived_separators'][0]['start'] = True
    elif mutation == 'authority':
        anchor['quotation_authority'] = True
    elif mutation == 'hash':
        anchor['positions'][0]['text_sha256'] = '0' * 64
    else:
        value['source_text'] = None
    path = tmp_path / 'invalid.reading.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        ReadingDocument.load(path)
    assert path.read_bytes() == before


def test_pre_release_recovery_import_does_not_overwrite_its_saved_bytes(tmp_path):
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    value, original = _pre_release_project()
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(original, 'fixture:voice', volume=60, cursor=2))
    record = json.loads(store.path.read_text(encoding='utf-8'))
    record['document'] = value
    store.path.write_text(json.dumps(record), encoding='utf-8')
    before = store.path.read_bytes()
    imported = store.load()
    assert imported.document.to_dict() == original.to_dict()
    assert imported.document.format_notice
    assert (imported.voice_id, imported.volume, imported.cursor) == ('fixture:voice', 60, 2)
    with pytest.raises(ValueError, match='Save As'):
        store.save(imported)
    assert store.path.read_bytes() == before
    other = ReaderDraftStore(tmp_path, slot='new-independent-copy')
    other.save(imported)
    assert other.load().document.to_dict() == original.to_dict()
    assert store.path.read_bytes() == before


def test_edit_never_changes_raw_source_and_mapping_is_conservative():
    raw = 'A raw line.\r\nІще інший рядок.\nمرحبا'
    copy = ReadingDocument(raw)
    assert copy.source_span(0, 10) == SourceSpan(0, 10, True)
    copy.replace(2, 5, 'corrected')
    assert copy.source_text == raw
    assert copy.source_sha256 == sha256(raw.encode()).hexdigest()
    assert copy.source_span(2, 11) == SourceSpan(2, 5, False)
    assert copy.source_span(11, 16) == SourceSpan(5, 10, True)
    assert copy.source_span(0, 16) == SourceSpan(0, 10, False)
    assert copy.undo() and copy.text == raw
    assert copy.source_span(0, 10).exact
    assert copy.redo() and 'corrected' in copy.text


def test_insert_has_no_invented_source_offset():
    copy = ReadingDocument('ABCD')
    copy.replace(2, 2, 'xyz')
    assert copy.text == 'ABxyzCD'
    assert copy.source_span(2, 5) == SourceSpan(None, None, False)
    assert copy.source_span(0, 7) == SourceSpan(0, 4, False)
    copy.replace(3, 4, '123')
    assert copy.source_span(3, 6) == SourceSpan(None, None, False)


def test_delete_does_not_rejoin_noncontiguous_source_as_exact():
    copy = ReadingDocument('ABCDE')
    copy.replace(2, 3, '')
    assert copy.text == 'ABDE'
    assert copy.source_span(0, 4) == SourceSpan(0, 5, False)
    assert copy.source_span(2, 4) == SourceSpan(3, 5, True)


def test_redo_is_invalidated_by_new_edit():
    copy = ReadingDocument('ABC')
    copy.replace(0, 1, 'X')
    copy.undo()
    copy.replace(1, 2, 'Y')
    assert not copy.can_redo
    assert copy.text == 'AYC'


def test_set_text_preserves_prefix_suffix_without_guessing_changed_offsets():
    copy = ReadingDocument('prefix middle suffix')
    copy.set_text('prefix replacement suffix')
    assert copy.source_span(0, 7) == SourceSpan(0, 7, True)
    assert not copy.source_span(7, 18).exact
    assert copy.source_span(18, 25) == SourceSpan(13, 20, True)


def test_reading_project_roundtrip_keeps_undo_and_redo(tmp_path):
    copy = ReadingDocument('Original\r\nукраїнський текст.', title='Книжка')
    copy.replace(0, 8, 'Narrated')
    copy.replace(8, 8, '!')
    copy.undo()
    path = tmp_path / 'Книжка.reading.json'
    checksum = copy.save(path)
    assert checksum == sha256(path.read_bytes()).hexdigest()
    reopened = ReadingDocument.load(path)
    assert reopened.source_text == copy.source_text
    assert reopened.text == copy.text
    assert reopened.can_redo and reopened.redo()
    assert reopened.text == 'Narrated!\r\nукраїнський текст.'
    assert not copy.dirty
    reopened.save(path)
    assert not reopened.dirty


def test_project_tampered_source_or_edit_rejected(tmp_path):
    copy = ReadingDocument('ABC')
    copy.replace(1, 2, 'X')
    path = tmp_path / 'project.json'
    copy.save(path)
    raw = json.loads(path.read_text())
    raw['source_text'] = 'other'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='checksum'):
        ReadingDocument.load(path)
    raw['source_text'] = 'ABC'
    raw['edits'][0]['start'] = 200
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='range'):
        ReadingDocument.load(path)


def test_to_dict_cannot_mutate_live_edit_history():
    copy = ReadingDocument('ABC')
    copy.replace(1, 2, 'D')
    value = copy.to_dict()
    value['edits'][0]['replacement'] = 'BAD'
    assert copy.to_dict()['edits'][0]['replacement'] == 'D'


def test_save_refuses_immutable_original_and_existing_unrelated_file(tmp_path):
    original = tmp_path / 'source.txt'
    original.write_bytes(b'ABC\r\n')
    copy = ReadingDocument('ABC\r\n', source_path=original)
    copy.replace(0, 1, 'Z')
    with pytest.raises(ValueError, match='original'):
        copy.save(original)
    existing = tmp_path / 'unrelated.json'
    existing.write_bytes(b'preserve me')
    with pytest.raises(FileExistsError):
        copy.save(existing)
    assert existing.read_bytes() == b'preserve me'
    assert original.read_bytes() == b'ABC\r\n'


def test_save_detects_external_modification(tmp_path):
    copy = ReadingDocument('ABC')
    target = tmp_path / 'draft.json'
    copy.save(target)
    other = ReadingDocument.load(target)
    other.replace(0, 1, 'Z')
    other.save(target)
    copy.replace(0, 1, 'Q')
    with pytest.raises(FileExistsError, match='changed'):
        copy.save(target)
    assert ReadingDocument.load(target).text == 'ZBC'


def test_cancelled_save_and_export_do_not_publish(tmp_path):
    copy = ReadingDocument('ABC')
    with pytest.raises(InterruptedError):
        copy.save(tmp_path / 'draft.json', cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        copy.export(tmp_path / 'copy.txt', cancelled=lambda: True)
    assert not list(tmp_path.iterdir())


def test_export_raw_and_derivative_preserves_line_breaks(tmp_path):
    raw = 'One\r\nДва\nThree\rFour'
    copy = ReadingDocument(raw)
    copy.replace(0, 3, 'Changed')
    copy.export(tmp_path / 'original.txt', original=True)
    copy.export(tmp_path / 'narration.txt')
    assert (tmp_path / 'original.txt').read_bytes() == raw.encode('utf-8')
    assert (tmp_path / 'narration.txt').read_bytes() == copy.text.encode('utf-8')


def test_cleanup_is_reversible_narration_only():
    original = 'A  hyphen-\nated\tword.\n\nParagraph.'
    copy = ReadingDocument(original)
    copy.cleanup(join_hyphenated=True)
    assert copy.text == 'A hyphenated word.\n\nParagraph.'
    assert copy.source_text == original
    assert 'Immutable source' in copy.diff()
    copy.reject_changes()
    assert copy.text == original
    assert copy.can_redo


def test_literal_and_bounded_regex_pronunciation_scope():
    copy = ReadingDocument('Dr. Smith, 12 kg. Dr. Jones.')
    copy.apply_pronunciations([
        PronunciationRule('Dr.', 'Doctor', language='en'),
        PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True),
        PronunciationRule('Doctor', 'Doktor', language='de'),
    ], language='en')
    assert copy.text == 'Doctor Smith, 12 kilograms. Doctor Jones.'
    assert copy.source_text == 'Dr. Smith, 12 kg. Dr. Jones.'
    copy.undo()
    assert copy.text == copy.source_text


def test_catastrophic_regex_is_killed_and_changes_are_atomic():
    copy = ReadingDocument('a' * 2000 + '!')
    before = copy.text
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        copy.apply_pronunciations([PronunciationRule('!', '?'),
            PronunciationRule('(a+)+$', 'X', regex=True)], timeout=.15)
    assert time.monotonic() - started < 5
    assert copy.text == before and copy.revision == 0


def test_dictionary_roundtrip_no_automatic_execution(tmp_path):
    rules = (PronunciationRule('Kyiv', 'Київ', language='uk'),
             PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True, voice_id='voice'))
    path = tmp_path / 'pronunciations.json'
    export_dictionary(rules, path)
    assert import_dictionary(path) == rules
    with pytest.raises(FileExistsError):
        export_dictionary(rules, path)


@pytest.mark.parametrize('raw', ['bad\0text', '\ud800'])
def test_invalid_source_unicode_rejected(raw):
    with pytest.raises(ValueError):
        ReadingDocument(raw)


def test_regex_child_preserves_runtime_bytecode_and_unicode_protocol(tmp_path, monkeypatch):
    """Use the real bounded regex worker after importing a stale cache fixture."""
    import py_compile
    from carr_library.speech import reading

    worker = Path(reading.__file__).with_name('regex_worker.py')
    sealed = tmp_path / 'sealed-runtime'
    sealed.mkdir()
    module = sealed / 'bytecode_probe.py'
    module.write_text("marker = 'old'\n", encoding='utf-8')
    py_compile.compile(str(module), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    module.write_text("marker = 'current approved source'\n", encoding='utf-8')
    before = {path.relative_to(sealed): path.read_bytes()
              for path in sealed.rglob('*') if path.is_file()}
    helper = tmp_path / 'regex_worker.py'
    helper.write_text(
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(sealed)!r})\n"
        "import bytecode_probe\n"
        "assert bytecode_probe.marker == 'current approved source'\n"
        f"runpy.run_path({str(worker)!r}, run_name='__main__')\n",
        encoding='utf-8')
    monkeypatch.setattr(reading, '__file__', str(tmp_path / 'reading.py'))
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    copy = ReadingDocument('Назва 12 кг.')
    copy.apply_pronunciations(
        [PronunciationRule(r'(\d+) кг', r'\1 кілограмів', regex=True)], timeout=10)
    assert copy.text == 'Назва 12 кілограмів.'
    assert copy.source_text == 'Назва 12 кг.'
    assert {path.relative_to(sealed): path.read_bytes()
            for path in sealed.rglob('*') if path.is_file()} == before


def test_one_thousand_matching_pronunciation_rules_commit_one_undo_step():
    document = ReadingDocument('[0000]')
    rules = [PronunciationRule(f'[{index:04d}]', f'[{index + 1:04d}]')
             for index in range(1000)]
    assert document.apply_pronunciations(rules)
    assert document.text == '[1000]'
    assert document.revision == 1 and len(document.to_dict()['edits']) == 1
    assert document.source_text == '[0000]'
    assert document.undo() and document.text == '[0000]'
    assert document.redo() and document.text == '[1000]'


def test_literal_expansion_is_admitted_before_large_allocation_and_is_atomic():
    import tracemalloc
    document = ReadingDocument('a' * 10_000)
    before = document.to_dict()
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match='budget'):
            document.apply_pronunciations([PronunciationRule('a', 'b' * 4096)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The rejected output would be over 40 MB. Admission must not allocate it.
    assert peak < 8 * 1024 * 1024
    assert document.to_dict() == before


def test_cancellation_during_many_literal_matches_leaves_the_copy_unchanged():
    document = ReadingDocument('a' * 100_000)
    before = document.to_dict()
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 6
    with pytest.raises(InterruptedError):
        document.apply_pronunciations([PronunciationRule('a', 'b')], cancelled=cancelled)
    assert checks == 6 and document.to_dict() == before


def test_regex_backreference_expansion_is_bounded_inside_the_real_child():
    import subprocess
    import sys
    from carr_library.speech import reading
    worker = Path(reading.__file__).with_name('regex_worker.py')
    code = (
        'import json, runpy, tracemalloc\n'
        f'worker = runpy.run_path({str(worker)!r})\n'
        'tracemalloc.start()\n'
        'try:\n'
        '    worker["main"]()\n'
        'except ValueError:\n'
        '    print(json.dumps({"rejected": True, "peak": tracemalloc.get_traced_memory()[1]}))\n'
        'else:\n'
        '    raise AssertionError("Unbounded replacement was accepted.")\n'
        'finally:\n'
        '    tracemalloc.stop()\n')
    payload = {'text': 'a' * 20_000, 'pattern': '(a+)',
               'replacement': r'\1' * 2048, 'ignore_case': False}
    completed = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-S', '-c', code],
        input=json.dumps(payload), capture_output=True, text=True, timeout=15, check=True)
    result = json.loads(completed.stdout)
    assert result['rejected'] is True
    # A single match.expand(template) would allocate 40 MB before rejecting it.
    assert result['peak'] < 8 * 1024 * 1024


def test_failed_regex_expansion_never_publishes_earlier_rules():
    document = ReadingDocument('!' + 'a' * 20_000)
    before = document.to_dict()
    with pytest.raises((ValueError, RuntimeError)):
        document.apply_pronunciations([PronunciationRule('!', '?'),
            PronunciationRule('(a+)', r'\1' * 2048, regex=True)], timeout=10)
    assert document.to_dict() == before


@pytest.mark.parametrize('text,pattern,replacement', [
    ('ab', r'(?P<one>a)(b)', r'\g<one>-\2-\\-\101'),
    ('a', r'(a)', r'\112\1'),
    ('b ab', r'(a)?b', r'<\1>'),
    ('aa', r'(?=a)|a', 'X'),
    ('a', 'a', r'\\g<one>'),
])
def test_bounded_regex_worker_keeps_python_replacement_semantics(text, pattern, replacement):
    import re
    document = ReadingDocument(text)
    document.apply_pronunciations([PronunciationRule(pattern, replacement, regex=True)], timeout=10)
    assert document.text == re.sub(pattern, replacement, text)
    assert document.source_text == text


@pytest.mark.parametrize('replacement', ['\\', r'\g<missing>'])
def test_invalid_regex_replacement_remains_atomic(replacement):
    document = ReadingDocument('a')
    with pytest.raises((ValueError, RuntimeError)):
        document.apply_pronunciations([PronunciationRule('a', replacement, regex=True)], timeout=10)
    assert document.text == 'a' and document.revision == 0


def test_bounded_rules_preserve_language_then_exact_voice_precedence():
    document = ReadingDocument('start')
    document.apply_pronunciations([
        PronunciationRule('language', 'voice', voice_id='fixture:voice'),
        PronunciationRule('general', 'language', language='en'),
        PronunciationRule('start', 'general'),
        PronunciationRule('voice', 'wrong', language='de', voice_id='fixture:voice'),
    ], language='en', voice_id='fixture:voice')
    assert document.text == 'voice' and document.revision == 1

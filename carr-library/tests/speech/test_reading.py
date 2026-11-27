"""Original text, source mapping and durable narration-copy regression tests."""
from hashlib import sha256
import json
from pathlib import Path
import time

import pytest

from carr_library.speech import (ReadingDocument, PronunciationRule, SourceSpan,
                                 export_dictionary, import_dictionary)


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

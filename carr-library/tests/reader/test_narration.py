from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path

import pytest

from carr_library.reader.narration import (
    DIRECTORY, NarrationStore, PronunciationRule, comparison_text, decode_project,
    encode_project, export_text, import_text, new_project, normalization_preview,
    pronunciation_preview, redo, revise, save_as, snapshot_narration_files, source_spans,
    text_digest, undo, validate_narration_payload,
)
from carr_library.reader.reading_state import ReadingLocation
from carr_library.ingest.writer_guard import exclusive_writer


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    return NarrationStore(root)


def source_project():
    text = 'Оригінальний текст.\r\nSlovenská veta. 😀'
    location = ReadingLocation('workspace', 'edition', 'revision', 'logical_segment',
                               1, text_digest(text), '1' * 64)
    return new_project(text, title='Український текст', source_location=location)


def test_reading_edits_preserve_immutable_source_and_explicit_provenance():
    original = source_project()
    changed = revise(original, 'Edited reading words.')
    assert changed.original_text == original.current_text
    assert changed.original_sha256 == original.original_sha256
    assert changed.source_location == original.source_location
    assert original.current_text != changed.current_text
    assert 'not a verified quotation' in changed.provenance
    with pytest.raises(FrozenInstanceError):
        changed.original_text = 'Changed source'


def test_source_hash_mismatch_fails_without_retargeting():
    project = source_project()
    with pytest.raises(ValueError, match='selected source'):
        new_project('Different words', source_location=project.source_location)


def test_revision_undo_redo_and_branch_keep_original_and_unique_numbers():
    project = new_project('Original')
    two = revise(revise(project, 'First edit'), 'Second edit')
    assert undo(two).current_text == 'First edit'
    assert redo(undo(two)) == two
    fork = revise(undo(two), 'Alternative edit')
    assert [r.number for r in fork.revisions] == [0, 1, 3]
    assert fork.original_text == 'Original'
    with pytest.raises(ValueError, match='no later'):
        redo(fork)
    with pytest.raises(ValueError, match='no earlier'):
        undo(project)


def test_dictionary_is_literal_longest_first_and_does_not_cascade():
    rules = (PronunciationRule('A.B', 'Alpha'), PronunciationRule('A', 'B'),
             PronunciationRule('B', 'C'), PronunciationRule('[x]', r'\1'))
    result, count = pronunciation_preview('A.B A B [x] a', rules)
    assert result == r'Alpha B C \1 a'
    assert count == 4


def test_pronunciation_language_label_is_not_automatic_language_detection():
    rule = PronunciationRule('Nitra', 'Нітра', 'uk')
    result, count = pronunciation_preview('Nitra. Nitra.', (rule,))
    assert result == 'Нітра. Нітра.' and count == 2
    with pytest.raises(ValueError, match='duplicate'):
        pronunciation_preview('Nitra', (rule, rule))


def test_pronunciation_expansion_is_bounded_before_building_giant_string(monkeypatch):
    import carr_library.reader.narration as narration
    monkeypatch.setattr(narration, 'MAX_TEXT_BYTES', 12)
    with pytest.raises(ValueError, match='expansion'):
        pronunciation_preview('aaaa', (PronunciationRule('a', 'word'),))


def test_shared_prefix_dictionary_has_explicit_work_budget(monkeypatch):
    import carr_library.reader.narration as narration
    monkeypatch.setattr(narration, 'MAX_MATCH_STEPS', 10)
    with pytest.raises(ValueError, match='work budget'):
        pronunciation_preview('aaaaaaaaaaaa', (PronunciationRule('aaaaab', 'word'),))


def test_spacing_preview_does_not_invent_punctuation_or_accents():
    original = '  slovo\t\tbez  čiarky  \r\n\r\n\r\nукраїнське слово\rкінець'
    assert normalization_preview(original) == ' slovo bez čiarky\n\nукраїнське слово\nкінець'


def test_conservative_unicode_offset_map_has_no_false_changed_alignment():
    project = revise(new_project('😀 α original tail'), '😀 α changed tail')
    assert source_spans(project) == (
        ('unchanged', 0, 4, 0, 4), ('derived', 4, 12, 4, 11), ('unchanged', 12, 17, 11, 16))
    assert 'no claim of exact source-word alignment' in comparison_text(project)
    assert 'identical' in comparison_text(new_project('same'))


@pytest.mark.parametrize('old,new', [('', ''), ('', 'new'), ('old', ''), ('X', 'Y'), ('same', 'same')])
def test_source_span_ranges_cover_reading_text_without_false_edges(old, new):
    project = revise(new_project(old), new)
    previous = 0
    for kind, a, b, c, d in source_spans(project):
        assert c == previous
        assert 0 <= a <= b <= len(old) and 0 <= c <= d <= len(new)
        if kind == 'unchanged':
            assert old[a:b] == new[c:d]
        previous = d
    assert previous == len(new)


def test_unicode_project_roundtrip_retains_history_rules_and_source():
    project = replace(revise(source_project(), 'Čítanie українською'),
                      rules=(PronunciationRule('слово', 'сло́во', 'uk'),))
    assert decode_project(encode_project(project)) == project


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(version=True),
    lambda v: v.update(original_sha256='0' * 64),
    lambda v: v.update(cursor=True),
    lambda v: v['revisions'][0].update(text='Changed original'),
    lambda v: v.update(unknown='field'),
    lambda v: v['revisions'][0].update(unexpected=1),
])
def test_malformed_saved_project_is_rejected(mutation):
    value = json.loads(encode_project(source_project()))
    mutation(value)
    with pytest.raises(ValueError, match='invalid'):
        decode_project(json.dumps(value).encode())


def test_duplicate_json_field_cannot_override_original_identity():
    raw = encode_project(new_project('text'))
    raw = raw.replace(b'"version":1', b'"version":1,"version":1')
    with pytest.raises(ValueError, match='Duplicate'):
        decode_project(raw)


def test_save_load_and_stale_save_preserve_existing_bytes(store):
    project = source_project()
    first = store.save(project)
    loaded, digest = store.load(project.id)
    assert loaded == project and digest == first
    current = revise(project, 'new reading text')
    second = store.save(current, expected_digest=first)
    with pytest.raises(ValueError, match='changed elsewhere'):
        store.save(revise(project, 'stale edit'), expected_digest=first)
    assert store.load(project.id) == (current, second)


def test_save_cannot_change_original_even_with_valid_lease(store):
    project = source_project()
    digest = store.save(project)
    replacement = replace(new_project('Different original'), id=project.id)
    with pytest.raises(ValueError, match='cannot be replaced'):
        store.save(replacement, expected_digest=digest)
    assert store.load(project.id)[0] == project


def test_draft_recovery_is_separate_and_conflicting_draft_is_kept(store):
    original = source_project()
    digest = store.save(original)
    draft = revise(original, 'Unsaved reading words')
    draft_digest = store.save(draft, draft=True)
    assert store.load(original.id) == (original, digest)
    assert store.load(original.id, draft=True) == (draft, draft_digest)
    with pytest.raises(ValueError, match='different recovery'):
        store.clear_draft(original.id, expected_digest='0' * 64)
    assert store.load(original.id, draft=True)[0] == draft
    store.clear_draft(original.id, expected_digest=draft_digest)
    with pytest.raises(FileNotFoundError):
        store.load(original.id, draft=True)


def test_atomic_save_failure_preserves_previous_and_removes_staging(store, monkeypatch):
    import carr_library.reader.narration as narration
    project = source_project()
    digest = store.save(project)
    monkeypatch.setattr(narration.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        store.save(revise(project, 'pending'), expected_digest=digest)
    assert store.load(project.id) == (project, digest)
    assert list((store.root / DIRECTORY).iterdir()) == [store.root / DIRECTORY / (project.id + '.json')]


def test_project_symlink_and_hardlink_are_rejected(store, tmp_path):
    project = source_project()
    target = tmp_path / 'external.json'
    target.write_bytes(encode_project(project))
    folder = store.root / DIRECTORY
    folder.mkdir()
    path = folder / (project.id + '.json')
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip('Host does not permit symlink creation')
    with pytest.raises(ValueError):
        store.load(project.id)
    path.unlink()
    import os
    os.link(target, path)
    with pytest.raises(ValueError):
        store.load(project.id)


def test_backup_snapshot_has_safe_relative_names_and_validated_payloads(store):
    project = source_project()
    store.save(project)
    store.save(revise(project, 'Recovery'), draft=True)
    with exclusive_writer(store.root):
        files = snapshot_narration_files(store.root)
    assert set(files) == {f'{DIRECTORY}/{project.id}.json', f'{DIRECTORY}/{project.id}.draft.json'}
    assert all(validate_narration_payload(name, data).id == project.id for name, data in files.items())
    with pytest.raises(ValueError, match='filename'):
        validate_narration_payload('../' + project.id + '.json', encode_project(project))


def test_snapshot_refuses_unknown_files_and_corruption(store):
    project = source_project()
    store.save(project)
    unknown = store.root / DIRECTORY / 'untracked.json'
    unknown.write_text('{}')
    with pytest.raises(ValueError, match='filename'):
        snapshot_narration_files(store.root)
    unknown.unlink()
    (store.root / DIRECTORY / (project.id + '.json')).write_bytes(b'not json')
    with pytest.raises(ValueError, match='invalid'):
        store.list_projects()


def test_list_filters_source_workspace_and_keeps_independent_projects(store):
    source = source_project()
    other = replace(source, id='2' * 32,
                    source_location=replace(source.source_location, workspace='other'))
    independent = new_project('Authored', title='Standalone')
    store.save(source)
    store.save(other, draft=True)
    store.save(independent)
    assert {r['project'].id for r in store.list_projects('workspace')} == {source.id, independent.id}


def test_utf8_import_and_export_preserve_original_and_refuse_silent_overwrite(store, tmp_path):
    input_path = tmp_path / 'Вхід.txt'
    input_path.write_bytes(b'\xef\xbb\xbf' + 'Текст\r\nŽivot'.encode())
    project = import_text(input_path)
    assert project.original_text == 'Текст\r\nŽivot'
    changed = revise(project, 'New reading text')
    output = tmp_path / 'Output.txt'
    export_text(changed, output, private_root=store.root)
    assert output.read_bytes() == b'New reading text'
    with pytest.raises(FileExistsError):
        export_text(project, output, private_root=store.root)
    export_text(project, output, private_root=store.root, overwrite=True)
    assert output.read_bytes() == 'Текст\r\nŽivot'.encode()
    assert input_path.read_bytes().startswith(b'\xef\xbb\xbf')
    with pytest.raises(ValueError, match='outside'):
        export_text(project, store.root / 'original.txt', private_root=store.root)


def test_empty_utf8_document_imports_as_an_independent_blank_copy(tmp_path):
    path = tmp_path / 'Empty.txt'
    path.write_bytes(b'')
    project = import_text(path)
    assert project.original_text == '' and project.source_location is None


def test_save_as_forks_project_without_retargting_source_or_erasing_history():
    project = revise(source_project(), 'Reading copy')
    fork = save_as(project, title='Separate project')
    assert fork.id != project.id and fork.title == 'Separate project'
    assert fork.original_text == project.original_text
    assert fork.revisions == project.revisions and fork.source_location == project.source_location

"""Synthetic canonical reading histories against the current project store."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os

import pytest

from carr_library.reader import legacy_reading as legacy
from carr_library.reader.narration import (
    NarrationStore, PronunciationRule, decode_project, encode_project, new_project,
    reading_project_reference, revise, save_as, source_spans, undo,
    validate_reading_project_reference,
)


def raw(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def payload(*, bound=True):
    source = 'First.\n\nКиїв — čítanie.'
    value = dict(schema='carr.reading-copy.v2' if bound else 'carr.reading-copy.v1',
        title='Збережена історія — Café', source_text=source,
        source_sha256=sha256(source.encode()).hexdigest(), edits=[
            dict(start=0, end=5, replacement='Changed', label='First change'),
            dict(start=0, end=7, replacement='First', label='Same words again'),
            dict(start=len(source), end=len(source), replacement='!', label='Redo tail'),
        ], revision=1)
    if bound:
        positions = []
        for ordinal, index, start, end in ((3, 8, 0, 6), (4, 12, 8, len(source))):
            positions.append(dict(ordinal=ordinal, kind='physical_page', index=index,
                start=start, end=end, text_sha256=sha256(source[start:end].encode()).hexdigest(),
                text_verified_at_capture=False))
        value['source_anchor'] = dict(workspace='library-A', edition='edition-A',
            revision='sealed-old', asset_sha256='a' * 64, positions=positions,
            quotation_authority=False)
    return value


def test_saved_cursor_and_redo_are_kept_without_full_revision_snapshots():
    data = raw(payload())
    snapshot = legacy.decode_snapshot(data)
    assert snapshot.saved_revision == snapshot.selected_revision == 1
    assert snapshot.text.startswith('Changed.')
    assert snapshot.at(0).text == payload()['source_text']
    assert snapshot.at(2).text == payload()['source_text']
    assert snapshot.at(3).text.endswith('!')
    assert snapshot.raw == data and len(snapshot.edits) == 3
    assert not hasattr(snapshot, '_states')
    assert snapshot.at(2).runs[0].exact is False
    assert snapshot.at(0).runs[1] == legacy.MappedRun('\n\n', None, None, False)


def test_complete_500_step_history_stays_compact_and_forks_only_selected_revision():
    value = payload(bound=False)
    value.update(source_text='0', source_sha256=sha256(b'0').hexdigest(),
        edits=[dict(start=0, end=1, replacement=str((index + 1) % 10), label=f'Edit {index}')
               for index in range(500)], revision=250)
    data = raw(value)
    snapshot = legacy.decode_snapshot(data)
    project = legacy.project_from_snapshot(snapshot, title='Selected old revision')
    assert len(snapshot.edits) == 500 and snapshot.at(500).text == '0'
    assert len(project.revisions) == 2 and project.legacy_origin.selected_revision == 250
    assert len(data) < 50000
    assert json.loads(encode_project(project))['version'] == 4


@pytest.mark.parametrize('dialect,flag', [
    ('source_anchor', 'text_verified_at_read'), ('source_origin', 'text_verified')])
def test_exact_released_legacy_anchor_dialects_preserve_all_positions(dialect, flag):
    value = payload()
    original = legacy.decode_snapshot(raw(value))
    anchor = value.pop('source_anchor')
    for row in anchor['positions']:
        row[flag] = row.pop('text_verified_at_capture')
    anchor['schema'] = 'carr.narration-source.v1'
    anchor['derived_separators'] = [dict(start=6, end=8)]
    value[dialect] = anchor
    converted = legacy.decode_snapshot(raw(value))
    assert converted.source_anchor == original.source_anchor
    assert converted.raw == raw(value)


@pytest.mark.parametrize('case', [
    'unknown', 'duplicate', 'nan', 'v1-anchor', 'two-anchors', 'wrong-source-hash',
    'verified-type', 'quotation-authority', 'separator', 'page-hash', 'ordinal-bool',
    'nonconsecutive-ordinal', 'extra-position', 'mixed-kind', 'bad-identity',
    'edit-bool', 'edit-outside', 'noop-redo', 'cursor-bool', 'cursor-outside',
    'too-many-edits', 'mixed-legacy-flag',
])
def test_invalid_or_mixed_histories_fail_before_any_state_publication(case):
    value = payload()
    if case == 'unknown':
        value['executable'] = 'never-run'
    elif case == 'duplicate':
        with pytest.raises(ValueError):
            legacy.decode_snapshot(raw(value).replace(b'"revision":1', b'"revision":1,"revision":1'))
        return
    elif case == 'nan':
        with pytest.raises(ValueError):
            legacy.decode_snapshot(raw(value).replace(b'"revision":1', b'"revision":NaN'))
        return
    elif case == 'v1-anchor':
        value['schema'] = 'carr.reading-copy.v1'
    elif case == 'two-anchors':
        value['source_origin'] = deepcopy(value['source_anchor'])
    elif case == 'wrong-source-hash':
        value['source_sha256'] = '0' * 64
    elif case == 'verified-type':
        value['source_anchor']['positions'][0]['text_verified_at_capture'] = 1
    elif case == 'quotation-authority':
        value['source_anchor']['quotation_authority'] = True
    elif case == 'separator':
        value['source_anchor']['positions'][1]['start'] = 7
    elif case == 'page-hash':
        value['source_anchor']['positions'][0]['text_sha256'] = '0' * 64
    elif case == 'ordinal-bool':
        value['source_anchor']['positions'][0]['ordinal'] = True
    elif case == 'nonconsecutive-ordinal':
        value['source_anchor']['positions'][1]['ordinal'] = 5
    elif case == 'extra-position':
        value['source_anchor']['positions'][0]['path'] = '/not-authority'
    elif case == 'mixed-kind':
        value['source_anchor']['positions'][1]['kind'] = 'logical_segment'
    elif case == 'bad-identity':
        value['source_anchor']['workspace'] = '\x00'
    elif case == 'edit-bool':
        value['edits'][0]['start'] = False
    elif case == 'edit-outside':
        value['edits'][0]['end'] = 99999
    elif case == 'noop-redo':
        value['edits'][2] = dict(start=0, end=5, replacement='First', label='Hidden invalid redo')
    elif case == 'cursor-bool':
        value['revision'] = True
    elif case == 'cursor-outside':
        value['revision'] = 4
    elif case == 'too-many-edits':
        value['edits'] *= 167
    elif case == 'mixed-legacy-flag':
        value['source_anchor']['positions'][0]['text_verified_at_read'] = False
    with pytest.raises(ValueError):
        legacy.decode_snapshot(raw(value))


def test_cancellation_checks_redo_history_as_well_as_saved_cursor(tmp_path):
    value = payload(bound=False)
    value['revision'] = 0
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 6
    with pytest.raises(InterruptedError):
        legacy.LegacyReadingStore(tmp_path).retain(raw(value), cancelled=cancelled)
    assert not (tmp_path / legacy.DIRECTORY).exists()


def test_same_rewritten_words_never_gain_exact_source_alignment():
    snapshot = legacy.decode_snapshot(raw(payload()), revision=2)
    project = legacy.project_from_snapshot(snapshot, title='Explicit derivative')
    assert project.current_text == project.original_text
    assert project.revisions[project.cursor].number == 1
    assert all(row[0] == 'derived' for row in source_spans(project))
    changed = revise(project, 'First.\n\nnew reading words')
    assert all(row[0] == 'derived' for row in source_spans(changed))
    original = undo(project)
    assert [row[0] for row in source_spans(original)] == ['unchanged', 'derived', 'unchanged']


def test_store_deduplicates_exact_raw_history_and_retains_it_across_forks(tmp_path):
    data = raw(payload())
    retained = legacy.LegacyReadingStore(tmp_path)
    snapshot = legacy.decode_snapshot(data, revision=2)
    identity = retained.retain(data)
    assert retained.retain(data) == identity
    project = legacy.project_from_snapshot(snapshot, title='New explicit copy')
    store = NarrationStore(tmp_path)
    saved = store.save(project)
    fork = save_as(project, title='Second editable copy')
    store.save(fork)
    assert store.load(project.id) == (project, saved)
    assert store.load(fork.id)[0].legacy_origin == project.legacy_origin
    files = legacy.snapshot_import_files(tmp_path)
    assert files == {legacy.DIRECTORY + '/' + identity + '.json': data}
    assert len(list((tmp_path / legacy.DIRECTORY).glob('*.json'))) == 1
    assert retained.load(identity, revision=3).text.endswith('!')


def test_same_semantic_json_with_different_bytes_retains_exact_original_files(tmp_path):
    a = raw(payload(bound=False))
    b = json.dumps(payload(bound=False), ensure_ascii=False, indent=2).encode()
    store = legacy.LegacyReadingStore(tmp_path)
    first, second = store.retain(a), store.retain(b)
    assert first != second
    assert store.load(first).raw == a and store.load(second).raw == b


def test_imported_projects_require_retained_history_and_immutable_origin(tmp_path):
    snapshot = legacy.decode_snapshot(raw(payload()))
    project = legacy.project_from_snapshot(snapshot, title='Bound copy')
    store = NarrationStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.save(project)
    assert not (tmp_path / 'narration-projects').exists()
    legacy.LegacyReadingStore(tmp_path).retain(snapshot.raw)
    digest = store.save(project)
    changed = replace(project, legacy_origin=snapshot.at(2).origin)
    with pytest.raises(ValueError, match='original source'):
        store.save(changed, expected_digest=digest)
    assert store.load(project.id) == (project, digest)
    archive = tmp_path / legacy.DIRECTORY / (snapshot.archive_sha256 + '.json')
    archive.write_bytes(snapshot.raw + b' ')
    with pytest.raises(ValueError, match='filename'):
        store.load(project.id)


def test_workspace_filter_carries_multi_position_identity(tmp_path):
    data = raw(payload())
    legacy.LegacyReadingStore(tmp_path).retain(data)
    project = legacy.project_from_snapshot(legacy.decode_snapshot(data), title='Bound')
    store = NarrationStore(tmp_path)
    store.save(project)
    store.save(new_project('independent', title='Independent'))
    assert project.workspace == 'library-A'
    assert {row['project'].title for row in store.list_projects('library-A')} == {'Bound', 'Independent'}
    assert {row['project'].title for row in store.list_projects('library-B')} == {'Independent'}


def test_legacy_project_versions_keep_original_schema_without_empty_origin_field():
    project = new_project('ordinary', title='Existing current format')
    version2 = json.loads(encode_project(project))
    assert version2['version'] == 2 and 'legacy_origin' not in version2
    advanced = replace(project, rules=(PronunciationRule('x', 'y', regex=True),))
    version3 = json.loads(encode_project(advanced))
    assert version3['version'] == 3 and 'legacy_origin' not in version3
    assert decode_project(encode_project(advanced)) == advanced
    snapshot = legacy.decode_snapshot(raw(payload()))
    imported = legacy.project_from_snapshot(snapshot, title='V4')
    assert decode_project(encode_project(imported)) == imported


def test_saved_audio_handoff_carries_immutable_multi_position_origin():
    snapshot = legacy.decode_snapshot(raw(payload()), revision=2)
    project = legacy.project_from_snapshot(snapshot, title='Studio handoff')
    digest = sha256(encode_project(project)).hexdigest()
    reference = reading_project_reference(project, saved_digest=digest)
    assert reference['legacy_origin'] == asdict(snapshot.origin)
    source_id = f"reading-project:{project.id}:revision:{reference['revision']}"
    assert validate_reading_project_reference(reference, original_text=project.original_text,
                                               source_id=source_id) is reference
    corrupted = deepcopy(reference)
    corrupted['legacy_origin']['source_anchor']['positions'][0]['text_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        validate_reading_project_reference(corrupted, original_text=project.original_text, source_id=source_id)


def test_hardlinked_external_snapshot_and_retained_archive_are_rejected(tmp_path):
    source, alias = tmp_path / 'original.json', tmp_path / 'alias.json'
    source.write_bytes(raw(payload()))
    os.link(source, alias)
    with pytest.raises(ValueError):
        legacy.read_snapshot(source)
    alias.unlink()
    retained = legacy.LegacyReadingStore(tmp_path)
    identity = retained.retain(source.read_bytes())
    archive = tmp_path / legacy.DIRECTORY / (identity + '.json')
    os.link(archive, tmp_path / 'archive-alias.json')
    with pytest.raises(ValueError):
        legacy.snapshot_import_files(tmp_path)


def test_late_competing_archive_is_never_replaced_or_deleted(tmp_path, monkeypatch):
    from carr_library.audio import jobs
    data = raw(payload())
    actual = jobs.publish_noreplace
    competitor = b'competing external bytes'
    def raced(stage, target):
        target.write_bytes(competitor)
        return actual(stage, target)
    monkeypatch.setattr(jobs, 'publish_noreplace', raced)
    with pytest.raises(FileExistsError):
        legacy.LegacyReadingStore(tmp_path).retain(data)
    path = tmp_path / legacy.DIRECTORY / (sha256(data).hexdigest() + '.json')
    assert path.read_bytes() == competitor
    assert not list(path.parent.glob('.reading-import-*.tmp'))


def test_byte_and_file_inventory_limits_apply_before_additional_publication(tmp_path, monkeypatch):
    data = raw(payload())
    monkeypatch.setattr(legacy, 'MAX_SNAPSHOT_BYTES', len(data) - 1)
    with pytest.raises(ValueError, match='budget'):
        legacy.LegacyReadingStore(tmp_path).retain(data)
    assert not (tmp_path / legacy.DIRECTORY).exists()

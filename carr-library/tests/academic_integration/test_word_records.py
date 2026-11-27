"""Bounded Word records and actual canonical CSL formatting; no Office claim."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from carr_library.academic_integration import word_records as model
from carr_library.academic_integration.word_bridge import (
    WordDecision, build_proposal, completed_receipt, _runs, _restore_items,
)

_spec = importlib.util.spec_from_file_location('word_record_fixtures', Path(__file__).with_name('word_fixtures.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def test_metadata_xml_roundtrip_is_inert_and_preserves_complete_current_wire(monkeypatch):
    value = fixture.installed_capture(fixture.proposal())
    metadata = value['metadata']
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Passive metadata launched a child.'))
    xml = model.metadata_xml(metadata)
    assert model.parse_metadata_xml(xml) == metadata
    assert model.validate_capture(value) is value
    assert model.canonical(metadata) == model.canonical(json.loads(model.canonical(metadata)))
    assert model.LEGACY_NAMESPACE != model.NAMESPACE


@pytest.mark.parametrize('raw', [
    b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":"\\ud800"}',
], ids=['duplicate', 'nan', 'infinity', 'surrogate'])
def test_strict_json_refuses_ambiguous_or_unencodable_records(raw):
    with pytest.raises(ValueError):
        model.parse_json(raw)


@pytest.mark.parametrize('change', [
    lambda c: c['document'].__setitem__('pid', True),
    lambda c: c['document'].__setitem__('text', 'bad\0text'),
    lambda c: c['selection'].__setitem__('start', -1),
    lambda c: c['metadata'].__setitem__('revision', True),
    lambda c: c['metadata'].__setitem__('extra', 'not admitted'),
    lambda c: c['controls'][0].__setitem__('native_id', 'changed'),
    lambda c: c['controls'][0].__setitem__('text', 'changed'),
    lambda c: c['controls'].append(deepcopy(c['controls'][0])),
    lambda c: c['metadata']['interchange']['clusters'][0]['items'][0].__setitem__('id', 'foreign'),
    lambda c: c['metadata']['source_scope'].__setitem__('snapshot_sha256', 'BAD'),
], ids=['pid-type', 'nul', 'negative-range', 'revision-type', 'unknown-field',
        'native-binding', 'edited-range', 'duplicate-range', 'foreign-reference', 'source-digest'])
def test_changed_or_forged_capture_refuses_before_any_operation(change):
    value = fixture.installed_capture(fixture.proposal())
    change(value)
    with pytest.raises(ValueError):
        model.validate_capture(value)


@pytest.mark.parametrize('attribute,value', [
    ('readonly', True), ('protection', 0), ('track_revisions', True), ('undo_level', 1),
])
def test_existing_word_editing_policy_is_never_modified_or_bypassed(attribute, value):
    current = fixture.capture()
    current['document'][attribute] = value
    with pytest.raises(ValueError, match='writable'):
        model.validate_proposal(fixture.proposal(current), current)
    assert current['document'][attribute] == value


@pytest.mark.parametrize('kind', ['selection', 'control', 'field', 'foreign-story'])
def test_insertion_requires_a_collapsed_main_story_outside_foreign_objects(kind):
    current = fixture.capture()
    if kind == 'selection':
        current['selection']['end'] = 1
    elif kind == 'foreign-story':
        current['selection']['story'] = 3
    elif kind == 'control':
        current['controls'].append(dict(native_id='zotero', tag='ZOTERO_ITEM', story=1,
            start=0, end=2, anchor=0, text='ZZ', locked=False, note_index=None))
    else:
        current['fields'].append(dict(start=0, end=2, story=1, code_sha256='a' * 64, result_sha256='b' * 64))
    before = deepcopy(current)
    with pytest.raises(ValueError):
        model.validate_proposal(fixture.proposal(current), current)
    assert current == before


@pytest.mark.parametrize('story,foreign', [(1, 'field'), (3, 'field'), (1, 'control'), (3, 'control')])
def test_replacing_managed_text_never_discards_nested_foreign_fields_or_controls(story, foreign):
    current = fixture.installed_capture(fixture.proposal())
    managed = current['controls'][0]
    managed['story'] = story
    if foreign == 'field':
        current['fields'].append(dict(start=1, end=2, story=story,
            code_sha256='a' * 64, result_sha256='b' * 64))
    else:
        current['controls'].append(dict(native_id='foreign', tag='ZOTERO_ITEM', story=story,
            start=1, end=2, anchor=1, text='Z', locked=False, note_index=None))
    with pytest.raises(ValueError, match='overlap'):
        model.mutation_allowed(current, insertion=False)


@pytest.mark.parametrize('change', [
    lambda p: p.__setitem__('revision', True),
    lambda p: p.__setitem__('session', '0' * 32),
    lambda p: p.__setitem__('capture_sha256', '0' * 64),
    lambda p: p['operations'][0].__setitem__('text', 'unreviewed'),
    lambda p: p['operations'][0]['runs'][0].__setitem__('styles', [['nested']]),
    lambda p: p['operations'][0]['runs'][0].__setitem__('styles', ['bold', 'bold']),
    lambda p: p['operations'][0].__setitem__('native_id', 'invented'),
], ids=['revision-type', 'session', 'capture-hash', 'text', 'style-type', 'duplicate-style', 'new-native-id'])
def test_whole_proposal_binding_is_required(change):
    current = fixture.capture()
    value = fixture.proposal(current)
    change(value)
    with pytest.raises(ValueError):
        model.validate_proposal(value, current)


@pytest.mark.parametrize('field,value', [
    ('proposal_sha256', '0' * 64), ('metadata_sha256', '0' * 64),
    ('revision', True), ('saved_by_bridge', True),
])
def test_completed_receipt_cannot_self_assert_a_different_commit(field, value):
    proposal = fixture.proposal()
    receipt = fixture.receipt(proposal)
    receipt[field] = value
    with pytest.raises(ValueError):
        completed_receipt(receipt, proposal)


def test_completed_binding_requires_exact_text_identity_inventory_and_metadata():
    proposal = fixture.proposal()
    good = fixture.receipt(proposal)
    assert completed_receipt(good, proposal) == good
    for field, value in [('text_sha256', '0' * 64), ('story', 3), ('tag', 'resonance:cite:' + '0' * 32)]:
        bad = deepcopy(good)
        bad['bindings'][0][field] = value
        with pytest.raises(ValueError):
            completed_receipt(bad, proposal)
    bad = deepcopy(good); bad['bindings'] *= 2
    with pytest.raises(ValueError):
        completed_receipt(bad, proposal)


def test_approval_is_exact_single_explicit_and_revocable():
    value = WordDecision()
    token = model.fingerprint(fixture.proposal())
    value.offer(token)
    assert not value.state(token)
    with pytest.raises(ValueError):
        value.approve('0' * 64)
    value.approve(token)
    assert value.state(token)
    with pytest.raises(ValueError):
        value.approve(token)
    value.close()
    assert value.closed and not value.state(token)
    with pytest.raises(ValueError):
        value.approve(token)


def test_typography_preserves_unicode_underline_superscript_and_literal_text():
    runs = _runs('  <i>Книга 😀</i> <u>under</u><sup>2</sup>  ', 'Книга 😀 under2')
    assert ''.join(run['text'] for run in runs) == 'Книга 😀 under2'
    assert any(run['styles'] == ['underline'] for run in runs)
    assert model.utf16_length('😀') == 2
    model.validate_runs(runs, 'Книга 😀 under2')
    with pytest.raises(ValueError):
        _runs('<a href="https://example.invalid">active</a>', 'active')


def test_duplicate_reference_items_preserve_their_distinct_verified_locators():
    cluster = fixture.interchange()['clusters'][0]
    cluster['items'] = [{'id': 'ref', 'locator': '11', 'label': 'page'}, {'id': 'ref', 'locator': '23', 'label': 'page'}]
    cluster['locator_evidence'] = [{'reference_id': 'ref', 'locator_verified': True}] * 2
    items = _restore_items(cluster)
    assert [item.locator for item in items] == ['11', '23']
    assert all(item.locator_verified for item in items)


@pytest.mark.parametrize('style, note, expected', [
    ('apa', False, '(Smith, 2020)'), ('ieee', False, '[1]'),
    ('chicago-notes', True, 'Jane Smith'),
])
def test_actual_canonical_processor_builds_complete_source_bound_proposal(tmp_path, style, note, expected):
    current = fixture.capture()
    intent = {'action': 'insert_citation', 'citation_id': None, 'items': [
        dict(reference_id='ref', locator=None, label='page', locator_verified=False,
             prefix='', suffix='', suppress_author=False)]}
    records = [fixture.reference()]
    before = deepcopy(records)
    result = build_proposal(current, intent, records=records,
        preview=N(style=style, locale='en-US'), directory=tmp_path,
        source_scope=dict(workspace='fixture', snapshot_sha256='e' * 64, name_mode='family-given'),
        cancelled=lambda: False)
    assert expected in result['operations'][0]['text']
    assert result['operations'][0]['note'] is note
    assert result['interchange']['processor']['name'] == 'citeproc-js'
    assert result['interchange']['clusters'][0]['locator_evidence'][0]['locator_verified'] is None
    assert records == before
    model.validate_proposal(result, current)


def test_actual_regeneration_retains_stable_ids_and_updates_the_entire_sequence(tmp_path):
    current = fixture.installed_capture(fixture.proposal())
    identifier = current['metadata']['interchange']['clusters'][0]['id']
    intent = {'action': 'edit_citation', 'citation_id': identifier, 'items': [
        dict(reference_id='ref', locator='12', label='page', locator_verified=True,
             prefix='', suffix='', suppress_author=False)]}
    result = build_proposal(current, intent, records=[fixture.reference()],
        preview=N(style='apa', locale='en-US'), directory=tmp_path,
        source_scope=dict(workspace='fixture', snapshot_sha256='e' * 64, name_mode='family-given'),
        cancelled=lambda: False)
    assert result['document_id'] == current['metadata']['document_id']
    assert result['revision'] == 2
    assert result['operations'][0]['native_id'] == current['controls'][0]['native_id']
    assert result['interchange']['clusters'][0]['id'] == identifier
    assert '12' in result['operations'][0]['text']
    assert not any(row['native_id'] is None for row in result['operations'])


def test_real_bibliography_only_preview_includes_all_selected_records_without_invented_citations(tmp_path):
    current = fixture.capture()
    result = build_proposal(current, dict(action='regenerate_bibliography', citation_id=None, items=[]),
        records=[fixture.reference()], preview=N(style='apa', locale='en-US'), directory=tmp_path,
        source_scope=dict(workspace='fixture', snapshot_sha256='e' * 64, name_mode='family-given'),
        cancelled=lambda: False)
    assert result['interchange']['clusters'] == []
    assert len(result['operations']) == 1
    assert result['operations'][0]['tag'].startswith(model.TAG_BIBLIOGRAPHY)
    assert 'Source methods' in result['operations'][0]['text']

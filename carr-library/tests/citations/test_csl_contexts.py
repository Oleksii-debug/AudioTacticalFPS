"""Real QuickJS document citation IDs and actual footnote positions."""
from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest

from carr_library.citations import csl


NOTE_STYLE = '''<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="note">
<info><title>Synthetic note-context probe</title><id>urn:carr:note-context-probe</id></info>
<citation near-note-distance="2"><layout delimiter="; "><group delimiter=" | ">
<text variable="title"/>
<choose>
  <if position="ibid-with-locator"><text value="IBID WITH LOCATOR"/></if>
  <else-if position="ibid"><text value="IBID"/></else-if>
  <else-if position="near-note"><text value="NEAR NOTE"/></else-if>
  <else-if position="subsequent"><text value="SUBSEQUENT"/></else-if>
  <else><text value="FIRST"/></else>
</choose>
<text variable="first-reference-note-number" prefix="first-note="/>
</group></layout></citation>
<bibliography><layout><text variable="title"/></layout></bibliography></style>'''
RECORDS = [dict(id=identity, type='book', title=title,
                author=[{'literal': 'Known author'}], issued={'date-parts': [[2026]]})
           for identity, title in (('a', 'Alpha'), ('b', 'Beta'))]


@pytest.fixture(scope='module')
def note_style():
    return csl.load_custom_style(NOTE_STYLE)


def cluster(identifier, reference='a', note=None, **item_options):
    return {'id': identifier, 'items': [{'id': reference, **item_options}], 'note_index': note}


@pytest.mark.parametrize(('notes', 'expected'), [
    ((1, 2), 'Alpha | IBID | first-note=1'),
    ((1, 1), 'Alpha | IBID'),
    ((1, 3), 'Alpha | NEAR NOTE | first-note=1'),
    ((1, 8), 'Alpha | SUBSEQUENT | first-note=1'),
])
def test_real_note_positions_control_ibid_and_near_note(note_style, notes, expected):
    result = csl.render(RECORDS, style=note_style,
        citations=[cluster('first', note=notes[0]), cluster('repeat', note=notes[1])])
    assert result.citations == ('Alpha | FIRST', expected)
    assert result.citation_ids == ('first', 'repeat')
    assert result.note_indices == notes


def test_gapped_document_notes_preserve_actual_first_reference_number(note_style):
    result = csl.render(RECORDS, style=note_style, citations=[
        cluster('alpha-first', note=7), cluster('beta', 'b', 8), cluster('alpha-again', note=11)])
    assert result.citations == ('Alpha | FIRST', 'Beta | FIRST',
                                'Alpha | SUBSEQUENT | first-note=7')
    assert result.note_indices == (7, 8, 11)
    assert result.citation_ids == ('alpha-first', 'beta', 'alpha-again')


def test_explicit_locators_stay_manual_with_document_context(note_style):
    result = csl.render(RECORDS, style=note_style, citations=[
        cluster('first', note=7, locator='3', label='page'),
        cluster('again', note=8, locator='4', label='page')])
    assert result.citations == ('Alpha | FIRST', 'Alpha | IBID WITH LOCATOR | first-note=7')
    assert any('supplied manually' in warning for warning in result.warnings)


def test_stable_cluster_ids_survive_insertion_and_reordering(note_style):
    first = [cluster('cite-alpha', note=1), cluster('cite-beta', 'b', 2)]
    inserted = [cluster('cite-new', 'b', 1), cluster('cite-alpha', note=2), cluster('cite-beta', 'b', 3)]
    reordered = [cluster('cite-beta', 'b', 1), cluster('cite-alpha', note=2)]
    before = csl.render(RECORDS, style=note_style, citations=first)
    after = csl.render(RECORDS, style=note_style, citations=inserted)
    moved = csl.render(RECORDS, style=note_style, citations=reordered)
    assert before.citation_ids == ('cite-alpha', 'cite-beta')
    assert after.citation_ids == ('cite-new', 'cite-alpha', 'cite-beta')
    assert moved.citation_ids == ('cite-beta', 'cite-alpha')
    assert dict(zip(after.citation_ids, after.citations)) == {
        'cite-new': 'Beta | FIRST', 'cite-alpha': 'Alpha | FIRST',
        'cite-beta': 'Beta | NEAR NOTE | first-note=1'}
    assert moved.citations == ('Beta | FIRST', 'Alpha | FIRST')
    assert moved.note_indices == (1, 2)
    assert csl.render(RECORDS, style=note_style, citations=first).citations == before.citations


@pytest.mark.parametrize('style', ['harvard', 'ieee', 'note'])
def test_legacy_and_explicit_default_contexts_have_identical_formatting(note_style, style):
    selected_style = note_style if style == 'note' else style
    legacy = [[{'id': 'a'}], [{'id': 'b'}], [{'id': 'a'}]]
    explicit = [cluster('citation-1'), cluster('citation-2', 'b'), cluster('citation-3')]
    old = csl.render(RECORDS, style=selected_style, citations=legacy)
    new = csl.render(RECORDS, style=selected_style, citations=explicit)
    assert new == old
    assert old.citation_ids == ('citation-1', 'citation-2', 'citation-3')
    assert old.note_indices == ((1, 2, 3) if style == 'note' else (0, 0, 0))


def test_omitted_note_numbers_use_sequence_positions_without_guessing_from_neighbors(note_style):
    missing = {'id': 'omitted', 'items': [{'id': 'a'}]}
    result = csl.render(RECORDS, style=note_style,
        citations=[cluster('explicit', 'b', 7), missing, cluster('none', 'b')])
    assert result.citation_ids == ('explicit', 'omitted', 'none')
    assert result.note_indices == (7, 2, 3)
    # Existing documents must supply every real note number; None has the
    # same positional default as the PR103 contract, not previous-number+1.


def test_mixed_legacy_and_document_clusters_preserve_order_and_effective_context(note_style):
    result = csl.render(RECORDS, style=note_style,
        citations=[[{'id': 'a'}], cluster('document-repeat', note=8), [{'id': 'b'}]])
    assert result.citation_ids == ('citation-1', 'document-repeat', 'citation-3')
    assert result.note_indices == (1, 8, 3)
    assert result.citations == ('Alpha | FIRST', 'Alpha | SUBSEQUENT | first-note=1', 'Beta | FIRST')


def test_default_and_empty_example_sequences_expose_aligned_context(note_style):
    implicit = csl.render(RECORDS, style=note_style)
    explicit = csl.render(RECORDS, style=note_style, citations=[[{'id': 'a'}], [{'id': 'b'}]])
    assert implicit == explicit
    assert implicit.citation_ids == ('citation-1', 'citation-2')
    assert implicit.note_indices == (1, 2)
    empty = csl.render(RECORDS, style=note_style, citations=[])
    assert empty.citations == empty.citation_ids == empty.note_indices == ()
    assert set(identity for entry in empty.entry_ids for identity in entry) == {'a', 'b'}


def test_explicit_context_for_in_text_style_and_maximum_note_boundary():
    result = csl.render(RECORDS, style='harvard',
                        citations=[cluster('main-text'), cluster('in-note', 'b', 1_000_000)])
    assert result.note_indices == (0, 1_000_000)
    assert result.citation_ids == ('main-text', 'in-note')


def test_document_context_and_metadata_are_not_mutated(note_style):
    records = deepcopy(RECORDS)
    citations = [cluster('a-in-document', note=7, locator='9', label='page'),
                 {'id': 'group', 'items': [{'id': 'b'}, {'id': 'a'}], 'note_index': 9}]
    original = deepcopy((records, citations))
    csl.render(records, style=note_style, citations=citations)
    assert (records, citations) == original


def test_unicode_and_javascript_shaped_ids_are_inert_data(note_style, tmp_path):
    target = tmp_path / 'must_not_exist'
    identifier = "x');require('fs').writeFileSync('" + str(target) + "','bad');//"
    assert len(identifier) <= 200 and not any(char.isspace() for char in identifier)
    result = csl.render(RECORDS, style=note_style,
                        citations=[cluster('примітка-Šimko', note=2), cluster(identifier, 'b', 4)])
    assert result.citation_ids == ('примітка-Šimko', identifier)
    assert result.citations == ('Alpha | FIRST', 'Beta | FIRST')
    assert not target.exists()


@pytest.mark.parametrize('identifier', [None, 0, 1, True, [], {}, '', ' ', 'two words',
    'line\nbreak', 'tab\tspace', 'no\u00a0break', 'line\u2028break', 'x' * 201,
    '__proto__', 'prototype', 'constructor', 'toString', 'hasOwnProperty', '__defineGetter__',
    'a\u202eb', 'a\u2067b', 'a\x85b', 'a\x00b', '\ud800'])
def test_invalid_document_ids_fail_before_runtime(identifier, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError):
        csl.render(RECORDS, style='harvard', citations=[cluster(identifier)])


@pytest.mark.parametrize('note', [0, -1, 1_000_001, True, False, '7', 7.0,
                                float('nan'), float('inf'), [], {}])
def test_invalid_note_indices_fail_before_runtime(note, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='note index'):
        csl.render(RECORDS, style='harvard', citations=[cluster('note', note=note)])


@pytest.mark.parametrize('citations', [
    [cluster('same'), cluster('same', 'b')],
    [[{'id': 'a'}], cluster('citation-1', 'b')],
    [cluster('citation-2'), [{'id': 'b'}]],
])
def test_duplicate_explicit_and_generated_ids_fail_before_runtime(citations, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='unique and nonreserved'):
        csl.render(RECORDS, style='harvard', citations=citations)


@pytest.mark.parametrize('value', [
    {'items': [{'id': 'a'}]}, {'id': 'missing-items'},
    {'id': 'extra', 'items': [{'id': 'a'}], 'properties': {'noteIndex': 7}},
    {'id': 'extra', 'items': [{'id': 'a'}], '__proto__': {}},
    {'id': 'empty', 'items': []}, {'id': 'bad', 'items': None},
    {'id': 'bad', 'items': [{'id': 'a'}, {'id': 'a'}]},
    {'id': 'bad', 'items': [{'id': 'missing'}]},
    {'id': 'bad', 'items': [{'id': 'a', '__proto__': {}}]},
])
def test_normalized_context_cannot_bypass_existing_item_validation(value, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError):
        csl.render(RECORDS, style='harvard', citations=[value])


def test_document_context_ids_count_toward_combined_input_budget(monkeypatch):
    records = [{'id': 'a', 'type': 'book', 'title': 'A'}]
    legacy = [[{'id': 'a'}]]
    old_size = len(json.dumps({'records': records, 'citations': legacy}, ensure_ascii=False).encode('utf-8'))
    budget = replace(csl.RenderBudget(), max_input_bytes=old_size)
    # The exact legacy boundary still works for explicit and implicit examples.
    result = csl.render(records, style='ieee', citations=legacy, budget=budget)
    assert csl.render(records, style='ieee', budget=budget) == result
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='byte budget'):
        csl.render(records, style='ieee', citations=[cluster('x' * 200)], budget=budget)


def test_many_context_ids_and_cluster_counts_remain_bounded(monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    citations = [cluster(str(index) + 'x' * 199) for index in range(4)]
    with pytest.raises(ValueError, match='byte budget'):
        csl.render(RECORDS, style='ieee', citations=citations,
                   budget=replace(csl.RenderBudget(), max_input_bytes=500))
    with pytest.raises(ValueError, match='batch budget'):
        csl.render(RECORDS, style='ieee', citations=[cluster('one'), [{'id': 'b'}]],
                   budget=replace(csl.RenderBudget(), max_citations=1))


def test_render_result_constructor_keeps_backward_compatible_defaults():
    result = csl.RenderedBibliography(csl.available_styles()[0], 'en-US', 'text',
                                      ('Entry',), (('a',),), ('Citation',))
    assert result.citation_ids == result.note_indices == ()

"""Real pinned CSL processor regressions; no formatter or runtime mocks."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import sys

import pytest

from carr_library.citations import csl


BOOK = {'id': 'smith2020', 'type': 'book', 'title': 'Synthetic source research',
        'author': [{'family': 'Smith', 'given': 'Jane'}],
        'issued': {'date-parts': [[2020]]}, 'publisher': 'Example Press'}


@pytest.fixture(scope='module')
def styled_books():
    return {style.id: csl.render([deepcopy(BOOK)], style=style.id)
            for style in csl.available_styles()}


def test_all_seven_real_processors_render_same_metadata(styled_books):
    assert set(styled_books) == {'apa', 'chicago-author-date', 'chicago-note', 'mla',
                                'harvard', 'vancouver', 'ieee'}
    for style, result in styled_books.items():
        assert result.style.id == style
        assert result.entry_ids == (('smith2020',),)
        assert len(result.entries) == len(result.citations) == 1
        assert 'Smith' in result.entries[0] and '2020' in result.entries[0]
        assert 'Example Press' in result.entries[0]
        assert result.processor_package_version == '2.4.63'
        assert result.processor_reported_version == '1.4.61'
        assert '(c) Frank Bennett' in result.attribution


def test_apa_book_golden(styled_books):
    result = styled_books['apa']
    assert result.entries == ('Smith, J. (2020). Synthetic source research. Example Press.',)
    assert result.citations == ('(Smith, 2020)',)


def test_harvard_book_golden(styled_books):
    result = styled_books['harvard']
    assert result.entries == ('Smith, J. (2020) Synthetic source research. Example Press.',)
    assert result.citations == ('(Smith, 2020)',)


def test_vancouver_book_golden(styled_books):
    result = styled_books['vancouver']
    assert result.entries == ('1. Smith J. Synthetic source research. Example Press; 2020.',)
    assert result.citations == ('(1)',)


def test_ieee_book_golden(styled_books):
    result = styled_books['ieee']
    assert result.entries == ('[1] J. Smith, Synthetic source research. Example Press, 2020.',)
    assert result.citations == ('[1]',)


def test_chicago_and_mla_book_goldens(styled_books):
    assert styled_books['chicago-author-date'].entries == (
        'Smith, Jane. 2020. Synthetic Source Research. Example Press.',)
    assert styled_books['chicago-author-date'].citations == ('(Smith 2020)',)
    assert styled_books['chicago-note'].entries == (
        'Smith, Jane. Synthetic Source Research. Example Press, 2020.',)
    assert styled_books['chicago-note'].citations == (
        'Jane Smith, Synthetic Source Research (Example Press, 2020).',)
    assert styled_books['mla'].entries == (
        'Smith, Jane. Synthetic Source Research. Example Press, 2020.',)
    assert styled_books['mla'].citations == ('(Smith)',)


def test_disambiguation_updates_prior_citation_and_sorts_bibliography():
    records = [dict(deepcopy(BOOK), id='z', title='Zebra'),
               dict(deepcopy(BOOK), id='a', title='Alpha')]
    result = csl.render(records, style='harvard')
    assert result.entry_ids == (('a',), ('z',))
    assert result.citations == ('(Smith, 2020b)', '(Smith, 2020a)')
    assert result.entries == ('Smith, J. (2020a) Alpha. Example Press.',
                              'Smith, J. (2020b) Zebra. Example Press.')


def test_numeric_style_uses_citation_order_not_source_order():
    records = [dict(deepcopy(BOOK), id='a', title='Alpha'),
               dict(deepcopy(BOOK), id='z', title='Zebra')]
    result = csl.render(records, style='ieee',
        citations=[[{'id': 'z'}], [{'id': 'a'}], [{'id': 'z'}, {'id': 'a'}]])
    assert result.entry_ids == (('z',), ('a',))
    assert result.citations == ('[1]', '[2]', '[1], [2]')


def test_empty_citation_sequence_still_includes_all_selected_references():
    result = csl.render([BOOK], style='ieee', citations=[])
    assert result.citations == ()
    assert result.entry_ids == (('smith2020',),)


def test_uncited_entries_do_not_take_numbers_before_cited_entries():
    records = [dict(deepcopy(BOOK), id='uncited', title='Uncited source'),
               dict(deepcopy(BOOK), id='cited', title='Cited source')]
    result = csl.render(records, style='ieee', citations=[[{'id': 'cited'}]])
    assert result.citations == ('[1]',)
    assert result.entry_ids == (('cited',), ('uncited',))


def test_publication_extent_is_not_inferred_as_quotation_locator():
    record = dict(deepcopy(BOOK), page='100-150')
    result = csl.render([record], style='harvard')
    assert result.citations == ('(Smith, 2020)',)
    assert '100–150' in result.entries[0]
    assert not any('manually' in warning for warning in result.warnings)
    explicit = csl.render([record], style='harvard',
                         citations=[[{'id': record['id'], 'locator': '7', 'label': 'page'}]])
    assert explicit.citations == ('(Smith, 2020, p. 7)',)
    assert any('supplied manually' in warning for warning in explicit.warnings)


def test_note_style_retains_extent_but_does_not_claim_verified_pinpoint():
    record = dict(deepcopy(BOOK), page='100-150')
    result = csl.render([record], style='chicago-note')
    assert '100–150' in result.citations[0]
    assert any('publication extent, not verified quotation' in warning for warning in result.warnings)


def test_input_metadata_and_cite_items_are_not_mutated():
    records = [deepcopy(BOOK)]
    clusters = [[{'id': BOOK['id'], 'locator': 'vii', 'label': 'page'}]]
    before = deepcopy((records, clusters))
    result = csl.render(records, style='ieee', citations=clusters)
    assert (records, clusters) == before
    assert 'vii' in result.citations[0]


@pytest.mark.parametrize('locale', csl.LOCALES)
def test_actual_pinned_locales_work_without_network(locale, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Formatting must not open a network socket.')
    monkeypatch.setattr(socket, 'socket', denied)
    record = dict(deepcopy(BOOK), title='Український текст a slovenský zdroj')
    result = csl.render([record], style='harvard', locale=locale)
    assert result.locale == locale
    assert 'Український' in result.entries[0] and 'slovenský' in result.entries[0]


def test_corporate_literal_remains_one_literal_creator():
    record = dict(deepcopy(BOOK), author=[{'literal': 'Research Council, Europe'}])
    result = csl.render([record], style='harvard')
    assert result.citations == ('(Research Council, Europe, 2020)',)
    assert result.entries[0].startswith('Research Council, Europe (2020)')


def test_missing_fields_are_style_fallbacks_not_invented_values():
    result = csl.render([{'id': 'x', 'type': 'book', 'title': 'Known title'}], style='harvard')
    assert result.entries == ('Known title (no date).',)
    assert any('no author' in warning for warning in result.warnings)
    assert any('no publication date' in warning for warning in result.warnings)


def test_html_preserves_typography_without_executable_links_or_attributes():
    record = dict(deepcopy(BOOK), title='A <i>styled</i> source',
                  URL='https://example.test/reference')
    result = csl.render([record], style='ieee', output='html')
    output = result.bibliography
    assert '<i>' in output
    assert 'example.test/reference' in output
    assert 'href=' not in output and '<a ' not in output
    assert '<script' not in output and 'onclick' not in output


def test_injected_javascript_is_inert_json_text(tmp_path):
    target = tmp_path / 'should_not_exist'
    title = "'); require('fs').writeFileSync('" + str(target) + "','bad'); //"
    record = dict(deepcopy(BOOK), title=title)
    result = csl.render([record], style='ieee')
    assert not target.exists()
    assert 'writeFileSync' in result.bibliography


@pytest.mark.parametrize('bad', [
    dict(BOOK, id='__proto__'), dict(BOOK, id='constructor'), dict(BOOK, id='toString'),
    dict(BOOK, URL='javascript:alert(1)'), dict(BOOK, URL='file:///private/source'),
    dict(BOOK, URL='https://name:secret@example.test/'),
    dict(BOOK, URL='https://example.test/a\nb'),
    dict(BOOK, type=[]), dict(BOOK, title='\ud800'), dict(BOOK, title='x\x00y'),
    dict(BOOK, author=[{'literal': 'Council', 'family': 'Smith'}]),
    dict(BOOK, author=[{'constructor': 'bad'}]),
    dict(BOOK, issued={'date-parts': [[2020, 13]]}),
    dict(BOOK, issued={'date-parts': [[True]]}),
    dict(BOOK, issued={'date-parts': [[[1]]]}),
    dict(BOOK, unknown_field='not silently discarded'),
])
def test_invalid_or_unsafe_records_fail_before_runtime(bad, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError):
        csl.render([bad], style='ieee')


@pytest.mark.parametrize('control', ['\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
                                   '\u2066', '\u2067', '\u2068', '\u2069', '\x85'])
def test_directional_overrides_and_c1_controls_cannot_spoof_metadata(control, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='control'):
        csl.render([dict(BOOK, title='Source' + control + 'txt.exe')])
    record = dict(deepcopy(BOOK), author=[{'family': 'Smith' + control, 'given': 'Jane'}])
    with pytest.raises(ValueError, match='control'):
        csl.render([record])
    with pytest.raises(ValueError, match='control'):
        csl.render([BOOK], citations=[[{'id': BOOK['id'], 'prefix': 'See' + control}]])


@pytest.mark.parametrize('parts', [[2026, 2, 31], [2023, 2, 29], [1900, 2, 29],
                                  [2024, 4, 31], [2024, 6, 31], [2024, 11, 31]])
def test_impossible_calendar_dates_fail_without_normalization(parts, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    record = dict(deepcopy(BOOK), issued={'date-parts': [parts]})
    with pytest.raises(ValueError, match='calendar date'):
        csl.render([record])
    assert record['issued']['date-parts'] == [parts]


@pytest.mark.parametrize('parts', [[2024, 2, 29], [2000, 2, 29], [-4, 2, 29],
                                  [0, 2, 29], [2024, 2], [2024]])
def test_valid_leap_partial_and_early_year_dates_remain_supported(parts):
    record = dict(deepcopy(BOOK), title='العربية і українська', issued={'date-parts': [parts]})
    original = deepcopy(record)
    result = csl.render([record], style='harvard')
    assert 'العربية' in result.bibliography and 'українська' in result.bibliography
    assert record == original


@pytest.mark.parametrize('citations', [
    [[{'id': 'missing'}]], [[{'id': BOOK['id']}, {'id': BOOK['id']}]],
    [[{'id': BOOK['id'], 'label': 'page'}]],
    [[{'id': BOOK['id'], 'locator': '1', 'label': []}]],
    [[{'id': BOOK['id'], 'suppress-author': 1}]],
    [[{'id': BOOK['id'], 'locator': ''}]], [[]],
    [[{'id': BOOK['id'], '__proto__': {}}]],
])
def test_invalid_citations_fail_before_runtime(citations, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError):
        csl.render([BOOK], style='ieee', citations=citations)


def test_duplicate_reference_ids_are_rejected():
    with pytest.raises(ValueError, match='unique'):
        csl.render([BOOK, BOOK], style='ieee')


@pytest.mark.parametrize('options', [{'style': '../apa'}, {'style': '<style/>'},
                                    {'locale': 'zz-ZZ'}, {'output': 'javascript'}])
def test_uninstalled_or_executable_options_are_rejected(options):
    with pytest.raises(ValueError):
        csl.render([BOOK], **options)


def test_selected_input_and_repeated_clusters_have_bounded_memory():
    budget = replace(csl.RenderBudget(), max_input_bytes=500)
    with pytest.raises(ValueError, match='byte budget'):
        csl.render([dict(BOOK, title='a' * 600)], style='ieee', budget=budget)
    with pytest.raises(ValueError, match='byte budget'):
        csl.render([BOOK], style='ieee', budget=budget,
                   citations=[[{'id': BOOK['id'], 'prefix': 'a' * 100}] for _ in range(10)])


def test_implicit_citations_obey_the_explicit_cluster_budget():
    records = [BOOK, dict(deepcopy(BOOK), id='second')]
    budget = replace(csl.RenderBudget(), max_records=2, max_citations=1)
    with pytest.raises(ValueError, match='default citation sequence'):
        csl.render(records, style='ieee', budget=budget)


def test_cpu_budget_is_enforced_by_actual_runtime():
    with pytest.raises(RuntimeError, match='resource budget'):
        csl.render([BOOK], style='ieee', budget=replace(csl.RenderBudget(), cpu_seconds=0.000001))


def test_memory_budget_is_enforced_by_actual_runtime():
    with pytest.raises(RuntimeError, match='resource budget'):
        csl.render([BOOK], style='apa', budget=replace(csl.RenderBudget(), memory_bytes=4 * 1024**2))


def test_missing_runtime_has_explicit_offline_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(RuntimeError, match='no online fallback'):
        csl.render([BOOK], style='ieee')


def test_tampered_processor_is_rejected_before_execution(tmp_path, monkeypatch):
    for folder in ('styles', '_vendor_citeproc'):
        shutil.copytree(csl._ROOT / folder, tmp_path / folder)
    (tmp_path / '_vendor_citeproc/citeproc.js').write_text('throw new Error("altered");')
    monkeypatch.setattr(csl, '_ROOT', tmp_path)
    with pytest.raises(RuntimeError, match='integrity'):
        csl.render([BOOK], style='ieee')


def test_all_pinned_resources_and_license_notices_match_manifest():
    manifest = json.loads((csl._ROOT / '_vendor_citeproc/UPSTREAM.json').read_text())
    for relative, expected in manifest['sha256'].items():
        assert sha256((csl._ROOT / relative).read_bytes()).hexdigest() == expected
    assert manifest['processor']['package_version'] == '2.4.63'
    assert manifest['processor']['reported_engine_version'] == '1.4.61'
    cpal = (csl._ROOT / '_vendor_citeproc/CPAL').read_text()
    assert 'Attribution Copyright Notice: (c) Frank Bennett' in cpal
    assert 'citeproc-js implements the Citation Style Language' in cpal
    assert 'Attribution URL: https://citationstyles.org/' in cpal

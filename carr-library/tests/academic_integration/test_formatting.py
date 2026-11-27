"""Actual pinned CSL engine checks. Soundness is not inferred from stub strings."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import time

import pytest

from carr_library.academic_integration import (
    CitationCancelled, CitationCluster, CitationError, CitationRequest,
    available_styles, render_citations,
)
from carr_library.academic_integration import formatting


@pytest.fixture
def references():
    return [dict(id='book-doe', type='book', title='The Book of Science',
                 author=[{'family': 'Doe', 'given': 'Jane'}],
                 issued={'date-parts': [[2020]]}, publisher='Example Press', page='1-400')]


@pytest.mark.parametrize('style,expected_bibliography,expected_citation', [
    ('apa', 'Doe, J. (2020). The Book of Science (pp. 1–400). Example Press.', '(Doe, 2020, p. 12)'),
    ('chicago-author-date', 'Doe, Jane. 2020. The Book of Science. Example Press.', '(Doe 2020, 12)'),
    ('chicago-notes', 'Doe, Jane. The Book of Science. Example Press, 2020.',
     'Jane Doe, The Book of Science (Example Press, 2020), 12.'),
    ('mla', 'Doe, Jane. The Book of Science. Example Press, 2020, pp. 1–400.', '(Doe 12)'),
    ('harvard', 'Doe, J. (2020) The Book of Science. Example Press, pp. 1–400.', '(Doe, 2020, p. 12)'),
    ('vancouver', '1. Doe J. The Book of Science. Example Press; 2020.', '(1)'),
    ('ieee', '[1] J. Doe, The Book of Science. Example Press, 2020, pp. 1–400.', '[1, p. 12]'),
])
def test_real_styles_preserve_page_extent_and_apply_the_actual_rules(
        references, style, expected_bibliography, expected_citation, record_property):
    # Each style must reach the real engine independently. A failed common
    # fixture used to turn one style failure into seven setup errors.
    started = time.monotonic()
    record_property('style', style)
    try:
        result = render_citations(references, style=style,
            citations=[CitationRequest('book-doe', '12', locator_verified=True)])
    finally:
        record_property('elapsed_seconds', time.monotonic() - started)
    assert result.bibliography[0].text == expected_bibliography
    assert result.citations[0].text == expected_citation
    assert result.bibliography[0].reference_id == 'book-doe'
    assert result.processor['name'] == 'citeproc-js'
    assert '<i>The Book of Science</i>' in result.bibliography[0].html or style == 'vancouver'


def test_upstream_pins_licenses_and_all_asset_digests():
    root = formatting._ROOT
    manifest = json.loads((root / 'UPSTREAM.json').read_text())
    raw = (root / 'vendor/citeproc.js').read_bytes()
    assert sha256(raw).hexdigest() == manifest['processor']['sha256']
    assert manifest['processor']['chosen_license'] == 'CPAL-1.0'
    assert 'ATTRIBUTION' in (root / 'vendor/LICENSE-CPAL-1.0.txt').read_text()
    for style in manifest['styles']:
        assert sha256((root / 'styles' / style['filename']).read_bytes()).hexdigest() == style['sha256']
    for name, digest in manifest['locales'].items():
        assert sha256((root / 'locales' / name).read_bytes()).hexdigest() == digest


def test_no_pinpoint_is_inferred_from_publication_page_extent(references):
    result = render_citations(references, citations=[CitationRequest('book-doe')])
    assert result.citations[0].text == '(Doe, 2020)'
    assert '1–400' in result.bibliography[0].text
    assert result.interchange['clusters'][0]['locator_evidence'][0]['locator_verified'] is None


def test_source_is_immutable_and_private_notes_never_enter_the_processing_copy(references):
    references[0]['note'] = 'Private workspace notes; not bibliographic metadata.'
    references[0]['abstract'] = 'Confidential unpublished passage.'
    original = deepcopy(references)
    result = render_citations(references)
    assert references == original
    assert 'note' not in result.references[0]
    assert 'abstract' not in result.references[0]
    changed = result.interchange
    changed['references'][0]['title'] = 'Changed externally'
    assert result.references[0]['title'] == original[0]['title']


def test_engine_updates_earlier_citations_for_author_date_disambiguation(references):
    second = deepcopy(references[0])
    second.update(id='book-second', title='Another Book')
    result = render_citations([references[0], second], citations=[
        CitationRequest('book-doe'), CitationRequest('book-second')])
    assert result.citations[0].text == '(Doe, 2020b)'
    assert result.citations[1].text == '(Doe, 2020a)'
    assert [x.reference_id for x in result.bibliography] == ['book-second', 'book-doe']


def test_numbered_styles_follow_actual_first_citation_order(references):
    second = deepcopy(references[0])
    second.update(id='other', title='Other Book')
    result = render_citations([references[0], second], style='ieee', citations=[
        CitationRequest('other'), CitationRequest('book-doe'), CitationRequest('other')])
    assert [x.text for x in result.citations] == ['[1]', '[2]', '[1]']
    assert [x.reference_id for x in result.bibliography] == ['other', 'book-doe']


def test_real_journal_volume_issue_doi_and_pinpoint_are_distinct():
    references = [{'id': 'journal', 'type': 'article-journal',
        'title': 'Understanding research',
        'author': [{'family': 'Doe', 'given': 'Jane'}, {'family': 'Roe', 'given': 'Robert'}],
        'issued': {'date-parts': [[2023, 7, 12]]}, 'container-title': 'Example Journal',
        'volume': '12', 'issue': '3', 'page': '100-125', 'DOI': '10.5555/example.2023'}]
    result = render_citations(references, citations=[
        CitationRequest('journal', '112-114', locator_verified=True)])
    assert result.bibliography[0].text == (
        'Doe, J., & Roe, R. (2023). Understanding research. Example Journal, '
        '12(3), 100–125. https://doi.org/10.5555/example.2023')
    assert '<i>Example Journal</i>, <i>12</i>(3)' in result.bibliography[0].html
    assert result.citations[0].text == '(Doe & Roe, 2023, pp. 112–114)'


def test_cluster_group_and_repeated_notes_use_real_processor_state(references):
    result = render_citations(references, style='chicago-notes', citations=[
        CitationCluster((CitationRequest('book-doe', '12', locator_verified=True),), 'first', 1),
        CitationCluster((CitationRequest('book-doe', '18', locator_verified=True),), 'second', 2)])
    assert result.citations[0].text == 'Jane Doe, The Book of Science (Example Press, 2020), 12.'
    # The pinned Chicago style's ibid-with-locator branch keeps author + locator.
    assert result.citations[1].text == 'Doe, 18.'
    assert [x.cluster_id for x in result.citations] == ['first', 'second']


@pytest.mark.parametrize('locale,expected', [('uk-UA', 'С. 12'), ('sk-SK', 's. 12')])
def test_actual_locales_and_unicode(locale, expected, references):
    references[0].update(title='Наука, človek і суспільство',
                         author=[{'family': 'Шевченко', 'given': 'Тарас'}])
    result = render_citations(references, locale=locale,
                             citations=[CitationRequest('book-doe', '12', locator_verified=True)])
    assert expected in result.citations[0].text
    assert 'Шевченко' in result.citations[0].text
    assert 'Наука, človek і суспільство' in result.bibliography[0].text


def test_unverified_locator_and_unknown_reference_stop_before_worker(references, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid citation reached the runtime.')
    monkeypatch.setattr(formatting, '_run', forbidden)
    with pytest.raises(CitationError, match='Verify'):
        render_citations(references, citations=[CitationRequest('book-doe', '12')])
    with pytest.raises(CitationError, match='unknown'):
        render_citations(references, citations=[CitationRequest('missing')])
    with pytest.raises(CitationError, match='unique'):
        render_citations(references * 2)


def test_html_output_does_not_publish_active_content(references):
    references[0]['title'] = 'Safe <i>emphasis</i> <script>alert(1)</script><img src="https://invalid.example/x">'
    result = render_citations(references)
    html = result.bibliography[0].html
    assert '<script' not in html and '<img' not in html
    assert '<i>' in html
    assert 'Safe' in result.bibliography[0].text


def test_local_style_is_explicit_offline_and_no_macro_cycles(tmp_path, references):
    template = '''<?xml version="1.0" encoding="utf-8"?><style xmlns="http://purl.org/net/xbiblio/csl" class="in-text" version="1.0"><info><title>Local style</title><id>urn:test:local</id></info>{macro}<citation><layout><text variable="title"/></layout></citation><bibliography><layout><text variable="title"/></layout></bibliography></style>'''
    path = tmp_path / 'local.csl'
    path.write_text(template.format(macro=''), encoding='utf-8')
    result = render_citations(references, style_path=path, citations=[CitationRequest('book-doe')])
    assert result.citations[0].text == 'The Book of Science'
    assert result.style.id.startswith('local-')
    assert result.style.source == 'local:local.csl'
    assert str(tmp_path) not in json.dumps(result.interchange)
    path.write_text(template.format(macro='<macro name="loop"><text macro="loop"/></macro>'), encoding='utf-8')
    with pytest.raises(CitationError, match='Recursive'):
        render_citations(references, style_path=path)
    path.write_text('<!DOCTYPE style [<!ENTITY leak SYSTEM "file:///etc/passwd">]>' + template.split('?>')[1].format(macro=''), encoding='utf-8')
    with pytest.raises(CitationError, match='DTD'):
        render_citations(references, style_path=path)


def test_tampered_packaged_style_is_rejected(tmp_path, references, monkeypatch):
    source = formatting._ROOT
    (tmp_path / 'styles').mkdir()
    shutil.copy(source / 'UPSTREAM.json', tmp_path)
    (tmp_path / 'styles/apa.csl').write_text('tampered')
    monkeypatch.setattr(formatting, '_ROOT', tmp_path)
    with pytest.raises(CitationError, match='integrity'):
        render_citations(references)


def test_missing_runtime_is_explicit_and_cancellation_precedes_launch(tmp_path, references):
    with pytest.raises(CitationError, match='Node.js'):
        render_citations(references, node_path=tmp_path / 'no-node')
    with pytest.raises(CitationCancelled):
        render_citations(references, cancelled=lambda: True)


def test_actual_engine_timeout_is_bounded(references):
    references = [dict(references[0], id=f'reference-{index}', title=f'Source {index}')
                  for index in range(1000)]
    start = time.monotonic()
    with pytest.raises(CitationError, match='time budget'):
        render_citations(references, timeout=0.1)
    assert time.monotonic() - start < 3


def test_actual_worker_cancels_after_launch(references, monkeypatch):
    import quickjs
    context_type = quickjs.Context
    evaluated = []

    class ObservedContext:
        def __init__(self):
            self.context = context_type()

        def __getattr__(self, name):
            return getattr(self.context, name)

        def eval(self, code):
            result = self.context.eval(code)
            evaluated.append(True)
            return result

    monkeypatch.setattr(quickjs, 'Context', ObservedContext)
    with pytest.raises(CitationCancelled):
        render_citations(references, cancelled=lambda: bool(evaluated))
    # The real pinned processor ran. Cancellation before returning its result
    # suppresses publication; this does not claim mid-instruction interruption.
    assert evaluated == [True]


def test_runtime_ignores_node_options_preload_injection(tmp_path, references, monkeypatch):
    marker = tmp_path / 'should-not-exist'
    script = tmp_path / 'preload.cjs'
    script.write_text('require("node:fs").writeFileSync(' + json.dumps(str(marker)) + ', "bad");')
    monkeypatch.setenv('NODE_OPTIONS', '--require=' + str(script))
    result = render_citations(references)
    assert result.bibliography[0].reference_id == 'book-doe'
    assert not marker.exists()


@pytest.mark.parametrize('locale', ['en-US', 'uk-UA', 'sk-SK'])
def test_real_document_profile_uses_structured_xml_without_the_string_parser(
        locale, references, monkeypatch):
    from carr_library.citations import csl

    original_execute = csl._execute
    observed = []

    def actual_execute(payload, *, processor, bridge, budget, cancelled=None):
        assert payload['style_xml']['name'] == 'style'
        assert all(value['name'] == 'locale' for value in payload['locales'].values())
        assert budget.cpu_seconds == 30 and budget.memory_bytes == 256 * 1024 * 1024
        from carr_library.academic_integration.runtime_adapter import adapter_identity
        identity = adapter_identity()
        assert sha256(processor.encode('utf-8')).hexdigest() == identity['adapted_processor_sha256']
        assert sha256((formatting._ROOT / 'vendor/citeproc.js').read_bytes()).hexdigest() == identity['base_processor_sha256']
        observed.append(True)
        # The actual pinned processor still formats the reference. Poison only
        # its fallback XML-string parser so the old runtime handoff fails.
        forbidden = "CSL.parseXml = function () { throw new Error('Unexpected XML string parser'); };\n"
        return original_execute(payload, processor=processor, bridge=forbidden + bridge,
                                budget=budget, cancelled=cancelled)

    monkeypatch.setattr(csl, '_execute', actual_execute)
    result = render_citations(references, locale=locale,
        citations=[CitationRequest('book-doe', '12', locator_verified=True)])
    assert observed == [True]
    assert result.bibliography[0].reference_id == 'book-doe'
    assert 'The Book of Science' in result.bibliography[0].text
    assert '12' in result.citations[0].text
    assert result.style.sha256 == next(style.sha256 for style in available_styles() if style.id == 'apa')

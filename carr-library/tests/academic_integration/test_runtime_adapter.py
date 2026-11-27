"""Real bounded-engine equivalence and historical receipt compatibility."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy

import pytest

from carr_library.academic_integration import (
    CitationCluster, CitationError, CitationParagraph, CitationRequest,
    export_citation_docx, read_citation_docx, regenerate_citation_docx, render_citations,
)
from carr_library.academic_integration import formatting, regeneration, runtime_adapter


REFERENCES = [
    {'id': 'alpha', 'type': 'book', 'title': 'Alpha',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2020]]}},
    {'id': 'beta', 'type': 'book', 'title': 'Beta',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2020]]}},
    {'id': 'early', 'type': 'book', 'title': 'Earlier',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2019]]}},
    {'id': 'adams', 'type': 'book', 'title': 'Elsewhere',
     'author': [{'family': 'Adams', 'given': 'Amy'}], 'issued': {'date-parts': [[2021]]}},
]


@contextmanager
def original_processor(monkeypatch):
    """Execute the real pinned original, under the same production budgets."""
    actual_run = formatting._run
    def original(raw):
        assert sha256(raw).hexdigest() == formatting._manifest()['processor']['sha256']
        return raw.decode('utf-8'), None
    def legacy_run(*args, **kwargs):
        value = actual_run(*args, **kwargs)
        assert value.pop('runtime_adapter') is None
        return value
    with monkeypatch.context() as patch:
        patch.setattr(runtime_adapter, 'prepare_processor', original)
        patch.setattr(formatting, '_run', legacy_run)
        yield


def legacy_document(tmp_path, monkeypatch):
    with original_processor(monkeypatch):
        result = render_citations(REFERENCES[:2], style='harvard', citations=[
            CitationCluster((CitationRequest('beta'),), 'b', 1),
            CitationCluster((CitationRequest('alpha'),), 'a', 2)])
        assert result.processor == formatting._manifest()['processor']
        return export_citation_docx(result, tmp_path / 'original.docx',
            title='Original historical processor', paragraphs=[
                CitationParagraph('First claim.', ('b',)),
                CitationParagraph('Second claim.', ('a',))])


def test_original_assets_are_unchanged_and_new_result_names_the_exact_adapter():
    manifest_bytes = (formatting._ROOT / 'UPSTREAM.json').read_bytes()
    original = (formatting._ROOT / 'vendor/citeproc.js').read_bytes()
    identity = runtime_adapter.adapter_identity()
    processor, proof = runtime_adapter.prepare_processor(original)
    assert proof == identity
    assert proof['upstream_revision'] == 'adffcfa3189ed21cbdb5272a74a8edbf817ac326'
    assert proof['base_processor_sha256'] == sha256(original).hexdigest()
    assert proof['adapted_processor_sha256'] == sha256(processor.encode('utf-8')).hexdigest()
    assert proof['patch_sha256'] == sha256(runtime_adapter._PATCH.read_bytes()).hexdigest()
    assert proof['license'] == 'CPAL-1.0'
    assert proof['upstream_status'].startswith('Unmerged upstream compatibility patch')
    result = render_citations(REFERENCES[:1], style='harvard')
    assert result.processor == dict(formatting._manifest()['processor'], runtime_adapter=identity)
    assert 'Frank Bennett' in result.attribution and 'Dan Stillman' in result.attribution
    assert (formatting._ROOT / 'UPSTREAM.json').read_bytes() == manifest_bytes
    assert (formatting._ROOT / 'vendor/citeproc.js').read_bytes() == original
    proof['id'] = 'caller mutation'
    assert runtime_adapter.adapter_identity() == identity


@pytest.mark.parametrize('kind', ['modified', 'oversized', 'missing'])
def test_bad_patch_asset_is_refused_before_actual_runtime(tmp_path, monkeypatch, kind):
    path = tmp_path / 'patch.json'
    if kind == 'modified':
        path.write_bytes(runtime_adapter._PATCH.read_bytes() + b' ')
    elif kind == 'oversized':
        path.write_bytes(b' ' * (runtime_adapter._MAX_PATCH_BYTES + 1))
    monkeypatch.setattr(runtime_adapter, '_PATCH', path)
    from carr_library.citations import csl
    def forbidden(*args, **kwargs):
        pytest.fail('An unverified processor adaptation reached QuickJS')
    monkeypatch.setattr(csl, '_execute', forbidden)
    with pytest.raises(CitationError, match='integrity|incomplete'):
        render_citations(REFERENCES[:1], style='harvard')


def test_original_processor_change_is_not_silently_adapted():
    raw = (formatting._ROOT / 'vendor/citeproc.js').read_bytes()
    with pytest.raises(ValueError, match='original citation processor'):
        runtime_adapter.prepare_processor(raw + b'\n')


@pytest.mark.parametrize('descending', [False, True])
@pytest.mark.parametrize('reversed_input', [False, True])
def test_actual_macro_cache_matches_original_multikey_sort_and_disambiguation(
        tmp_path, monkeypatch, descending, reversed_input):
    # The same nested author/date macros appear in multiple keys and in BOTH
    # citation and bibliography sort contexts. A different key direction must
    # not leak through a cache. The reference order is independently permuted.
    direction = 'descending' if descending else 'ascending'
    style = tmp_path / 'multiple-sort-keys.csl'
    style.write_text('''<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
      <info><title>Multiple sort contexts</title><id>urn:carr:test:sort-cache</id></info>
      <macro name="name"><names variable="author"><name form="short"/></names></macro>
      <macro name="author-key"><text macro="name"/></macro>
      <macro name="year"><date variable="issued"><date-part name="year"/></date></macro>
      <macro name="date-key"><text macro="year"/></macro>
      <macro name="title-key"><text variable="title"/></macro>
      <citation disambiguate-add-year-suffix="true">
        <sort><key macro="author-key"/><key macro="date-key" sort="descending"/>
          <key macro="title-key" sort="descending"/><key macro="author-key"/></sort>
        <layout prefix="(" suffix=")" delimiter="; ">
          <group delimiter=" "><text macro="name"/><text macro="year"/></group>
        </layout>
      </citation>
      <bibliography>
        <sort><key macro="author-key"/><key macro="date-key"/>
          <key macro="title-key" sort="''' + direction + '''"/><key macro="author-key"/></sort>
        <layout><group delimiter=" | "><text macro="name"/><text macro="year"/>
          <text variable="title"/></group></layout>
      </bibliography>
    </style>''', encoding='utf-8')
    refs = list(reversed(REFERENCES)) if reversed_input else deepcopy(REFERENCES)
    citations = [
        CitationRequest('beta'), CitationRequest('alpha'),
        CitationCluster(tuple(CitationRequest(row['id']) for row in reversed(REFERENCES)),
                        'all', 0),
    ]
    import quickjs
    actual_context, limits = quickjs.Context, []
    class BoundedContext:
        def __init__(self):
            self.context = actual_context()
            self.values = {}
            limits.append(self.values)
        def __getattr__(self, name):
            return getattr(self.context, name)
        def set_memory_limit(self, value):
            assert value == 256 * 1024 * 1024
            self.values['memory'] = value
            return self.context.set_memory_limit(value)
        def set_time_limit(self, value):
            assert value == 30.0
            self.values['time'] = value
            return self.context.set_time_limit(value)
        def set_max_stack_size(self, value):
            assert value == 4 * 1024 * 1024
            self.values['stack'] = value
            return self.context.set_max_stack_size(value)
    monkeypatch.setattr(quickjs, 'Context', BoundedContext)
    with original_processor(monkeypatch):
        original = render_citations(refs, style_path=style, citations=citations)
    adapted = render_citations(refs, style_path=style, citations=citations)
    assert adapted.bibliography == original.bibliography
    assert adapted.citations == original.citations
    assert adapted.warnings == original.warnings
    assert adapted.style == original.style
    expected = ['adams', 'early'] + (['beta', 'alpha'] if descending else ['alpha', 'beta'])
    assert [entry.reference_id for entry in adapted.bibliography] == expected
    assert adapted.citations[0].text != adapted.citations[1].text
    assert any('2020a' in row.text for row in adapted.citations)
    assert any('2020b' in row.text for row in adapted.citations)
    assert len(limits) == 2 and all(set(row) == {'memory', 'time', 'stack'} for row in limits)


def test_historical_docx_regenerates_to_explicit_adapter_without_mutating_original(tmp_path, monkeypatch):
    source = legacy_document(tmp_path, monkeypatch)
    before_bytes = source.read_bytes()
    before = read_citation_docx(source)
    output = regenerate_citation_docx(source, tmp_path / 'adapted.docx')
    after = read_citation_docx(output)
    assert before['processor'] == formatting._manifest()['processor']
    assert after['processor'] == dict(before['processor'], runtime_adapter=runtime_adapter.adapter_identity())
    for key in ('style', 'references', 'clusters', 'document', 'bibliography', 'citations'):
        assert after[key] == before[key]
    assert source.read_bytes() == before_bytes
    assert read_citation_docx(source) == before
    assert 'runtime_adapter' not in before['processor']


@pytest.mark.parametrize('change', [
    lambda proof: proof.update(patch_sha256='0' * 64),
    lambda proof: proof.update(adapted_processor_sha256='0' * 64),
    lambda proof: proof.update(upstream_revision='0' * 40),
    lambda proof: proof.update(resource_profile='unreviewed'),
    lambda proof: proof.update(extra='do not silently drop'),
])
def test_rehashed_unknown_adapter_receipt_is_refused_before_engine(tmp_path, monkeypatch, change):
    source = legacy_document(tmp_path, monkeypatch)
    rewrite = runpy.run_path(str(Path(__file__).with_name('test_regeneration.py')))['rewrite_snapshot']
    proof = runtime_adapter.adapter_identity()
    change(proof)
    rewrite(source, lambda snapshot: snapshot['processor'].update(runtime_adapter=proof))
    original = source.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('An unrecognized saved processor reached the runtime')
    monkeypatch.setattr(regeneration, 'render_citations', forbidden)
    with pytest.raises(CitationError, match='processor identity changed'):
        regenerate_citation_docx(source, tmp_path / 'refused.docx')
    assert source.read_bytes() == original
    assert not (tmp_path / 'refused.docx').exists()

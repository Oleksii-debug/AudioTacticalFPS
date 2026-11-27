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


@pytest.fixture(scope='module')
def real_results():
    items = [dict(id='book-doe', type='book', title='The Book of Science',
                  author=[{'family': 'Doe', 'given': 'Jane'}],
                  issued={'date-parts': [[2020]]}, publisher='Example Press', page='1-400')]
    return {style.id: render_citations(items, style=style.id,
            citations=[CitationRequest('book-doe', '12', locator_verified=True)])
            for style in available_styles()}


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
        real_results, style, expected_bibliography, expected_citation):
    result = real_results[style]
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
    processes = []
    original = formatting.subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(formatting.subprocess, 'Popen', launch)
    with pytest.raises(CitationCancelled):
        render_citations(references, cancelled=lambda: bool(processes))
    assert len(processes) == 1 and processes[0].poll() is not None


def test_runtime_ignores_environment_preload_injection(tmp_path, references, monkeypatch):
    marker = tmp_path / 'should-not-exist'
    script = tmp_path / 'preload.cjs'
    script.write_text('require("node:fs").writeFileSync(' + json.dumps(str(marker)) + ', "bad");')
    monkeypatch.setenv('NODE_OPTIONS', '--require=' + str(script))
    python_preload = tmp_path / 'quickjs.py'
    python_preload.write_text('raise RuntimeError("untrusted import preload executed")')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setenv('PYTHONSTARTUP', str(python_preload))
    result = render_citations(references)
    assert result.bibliography[0].reference_id == 'book-doe'
    assert not marker.exists()


def test_default_quickjs_needs_no_path_node_and_preserves_provenance(tmp_path, references, monkeypatch):
    from carr_library.citations import csl
    monkeypatch.setenv('PATH', str(tmp_path))
    result = render_citations(references, citations=[CitationRequest('book-doe')])
    assert result.citations[0].text == '(Doe, 2020)'
    runtime = result.interchange['runtime']
    assert runtime['name'] == 'QuickJS' and runtime['binding'] == 'quickjs-ng'
    assert runtime['source_sha256'] == sha256(Path(csl.__file__).read_bytes()).hexdigest()
    assert runtime['bridge_sha256'] == sha256(formatting._QUICKJS_BRIDGE.encode('utf-8')).hexdigest()
    assert runtime['processor_sha256'] == result.processor['sha256']
    assert runtime['memory_bytes'] == 512 * 1024 * 1024
    assert runtime['stack_bytes'] == 4 * 1024 * 1024
    assert runtime['binding_version']
    assert str(tmp_path) not in json.dumps(result.interchange)


def test_tampered_processor_stops_before_any_worker(tmp_path, references, monkeypatch):
    bundle = tmp_path / 'bundle'
    shutil.copytree(formatting._ROOT, bundle)
    processor = bundle / 'vendor' / 'citeproc.js'
    processor.write_bytes(processor.read_bytes() + b'\n// altered processor')
    monkeypatch.setattr(formatting, '_ROOT', bundle)
    def forbidden(*args, **kwargs):
        pytest.fail('An altered processor reached process creation.')
    monkeypatch.setattr(formatting.subprocess, 'Popen', forbidden)
    with pytest.raises(CitationError, match='integrity'):
        render_citations(references)


def test_shared_quickjs_context_has_no_host_functions_and_keeps_input_as_data():
    from carr_library.citations import csl
    payload = {'value': '"); throw new Error("injected"); //',
               '__proto__': {'polluted': True}}
    bridge = """(function (data) {
        return JSON.stringify({value:data.value, process:typeof process,
            require:typeof require, fetch:typeof fetch, python:typeof python,
            polluted:typeof Object.prototype.polluted});
    })"""
    raw = csl._evaluate_processor('', bridge, json.dumps(payload),
        csl.RenderBudget(memory_bytes=16 * 1024 * 1024, cpu_seconds=1))
    result = json.loads(raw)
    assert result.pop('value') == payload['value']
    assert set(result.values()) == {'undefined'}


def test_shared_quickjs_context_interrupts_actual_excess_cpu():
    from carr_library.citations import csl
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='resource budget'):
        csl._evaluate_processor('', '(function (data) { while (true) {} })', '{}',
            csl.RenderBudget(memory_bytes=16 * 1024 * 1024, cpu_seconds=0.01))
    assert time.monotonic() - started < 3


def test_explicit_node_compatibility_dispatch_captures_source_and_blocks_preloads(
        tmp_path, references, monkeypatch):
    # Dispatch fixture only; actual CSL quality is covered by the real QuickJS
    # cases above. This does not claim an arbitrary file is a real Node runtime.
    binary = tmp_path / 'node.exe'
    binary.write_bytes(b'non-executable compatibility dispatch fixture')
    calls = []
    raw_result = {'processor_version': formatting._manifest()['processor']['version'],
                  'parameters': {'entry_ids': [['book-doe']]},
                  'bibliography': ['Compatibility dispatch fixture'], 'citations': [], 'warnings': []}
    class Complete:
        returncode = 0
        def poll(self):
            return 0
    def launch(command, **kwargs):
        calls.append(command)
        stage = Path(kwargs['cwd'])
        assert Path(command[0]).samefile(binary)
        assert command[-1] == str(stage / 'csl_worker.cjs')
        assert kwargs['shell'] is False
        assert not {'NODE_OPTIONS', 'NODE_PATH', 'PYTHONPATH', 'PYTHONHOME'} & set(kwargs['env'])
        assert (stage / 'vendor' / 'citeproc.js').read_bytes() == formatting._processor_bytes()
        assert (stage / 'csl_worker.cjs').read_bytes() == (formatting._ROOT / 'csl_worker.cjs').read_bytes()
        kwargs['stdout'].write(json.dumps(raw_result).encode('utf-8'))
        kwargs['stdout'].flush()
        return Complete()
    monkeypatch.setenv('NODE_OPTIONS', '--require=unreviewed.cjs')
    monkeypatch.setenv('NODE_PATH', str(tmp_path))
    monkeypatch.setattr(formatting.subprocess, 'Popen', launch)
    result = render_citations(references, node_path=binary)
    runtime = result.interchange['runtime']
    assert runtime['name'] == 'Node.js' and runtime['mode'] == 'explicit compatibility'
    assert runtime['executable_sha256'] == sha256(binary.read_bytes()).hexdigest()
    assert len(calls) == 1
    assert str(binary) not in json.dumps(result.interchange)


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]'])
def test_processor_result_decoder_rejects_ambiguous_output(raw):
    with pytest.raises(CitationError):
        formatting._decode_result(raw)

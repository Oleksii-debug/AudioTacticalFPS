"""Actual isolated CLI over rich retained DOCX; no Office or live provider."""
import importlib.util
from pathlib import Path

import pytest

from carr_library.assistant import translation_docx_rich as rich
from carr_library.assistant.profiles import ProfileStore

_spec = importlib.util.spec_from_file_location('rich_cli_current_cases',
    Path(__file__).with_name('test_translation_layout_cli.py'))
_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cli)
call, arguments = _cli.call, _cli.arguments
_spec = importlib.util.spec_from_file_location('rich_cli_document_cases',
    Path(__file__).with_name('test_translation_rich_docx.py'))
_document = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_document)
bound, ready = _document.bound, _document.ready


def test_actual_cli_reviews_rich_source_then_saves_one_explicit_partition_job(bound):
    before = list(bound.state['calls'])
    result, preview = call(bound, 'docx-preview', *arguments(bound))
    assert result.returncode == 0, result.stderr
    assert preview['layout']['schema'] == rich.PROFILE
    assert preview['layout']['canonical_paragraphs'] == 7
    assert preview['layout']['note_paragraphs'] == 3
    assert preview['layout']['notice'] == rich.NOTICE
    assert not preview['provider_calls_started'] and not bound.service.list()
    result, outcome = call(bound, 'docx-submit', *arguments(bound),
        '--approve-local', '--review-sha256', preview['review_sha256'])
    assert result.returncode == 0, result.stderr
    assert len(outcome['jobs']) == 1 and not outcome['provider_calls_started']
    job = bound.service.get(outcome['jobs'][0])
    assert job['source_partition']['profile'] == rich.PROFILE
    assert bound.state['calls'] == before and bound.source.read_bytes() == bound.original


@pytest.mark.parametrize('consent', ['wrong-review', 'wrong-mode'])
def test_actual_cli_refuses_rich_source_without_the_exact_local_review(bound, consent):
    result, preview = call(bound, 'docx-preview', *arguments(bound))
    assert result.returncode == 0, result.stderr
    digest = '0' * 64 if consent == 'wrong-review' else preview['review_sha256']
    flag = '--approve-remote' if consent == 'wrong-mode' else '--approve-local'
    result, value = call(bound, 'docx-submit', *arguments(bound), flag, '--review-sha256', digest)
    assert result.returncode == 1 and value is None and not bound.service.list()


def test_actual_cli_exports_completed_rich_job_offline_without_overwrite(bound, tmp_path):
    selected, identities = _document.completed(bound)
    ProfileStore(bound.app.root / 'state/providers').remove(bound.profile.id)
    output = tmp_path / 'Translated rich CLI Ω.docx'
    scope = ('--workspace', bound.workspace, '--edition', bound.edition)
    before = list(bound.state['calls'])
    result, receipt = call(bound, 'docx-export', *scope, '--output', output, *identities)
    assert result.returncode == 0, result.stderr
    assert receipt['status'] == 'complete' and receipt['notice'] == rich.NOTICE
    assert receipt['changed_members'] == ['word/document.xml', 'word/endnotes.xml', 'word/footnotes.xml']
    assert rich.describe(output.read_bytes())['media'] == selected.mapping['media']
    committed = output.read_bytes()
    result, value = call(bound, 'docx-export', *scope, '--output', output, *identities)
    assert result.returncode == 1 and value is None
    assert output.read_bytes() == committed and bound.state['calls'] == before
    assert bound.source.read_bytes() == bound.original

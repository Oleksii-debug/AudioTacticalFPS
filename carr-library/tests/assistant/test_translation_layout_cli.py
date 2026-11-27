"""Actual isolated CLI processes over retained DOCX and ordinary saved jobs.

The parent prepares synthetic accepted translations only where a completed job
is required. CLI children use no live service, inherit no test transport, and
assert the exact parent-imported CLI origin before running the real parser.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.assistant import translation_cli
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.translation_documents import run_documents

_spec = importlib.util.spec_from_file_location('formatted_cli_document_cases',
    Path(__file__).with_name('test_translation_layout.py'))
_document = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_document)
bound, ready = _document.bound, _document.ready


def call(case, *arguments):
    actual = Path(translation_cli.__file__).resolve()
    root = actual.parents[2]
    roots = [str(root)]
    if (root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
        roots.append(str(root.parent))
    bootstrap = (
        "import json,pathlib,sys; sys.path[:0]=json.loads(sys.argv[1]); "
        "from carr_library.assistant import translation_cli as entry; "
        "assert pathlib.Path(entry.__file__).resolve()==pathlib.Path(sys.argv[2]).resolve(); "
        "raise SystemExit(entry.main(json.loads(sys.argv[3])))")
    arguments = ['--data-dir', str(case.app.root), *map(str, arguments)]
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONIOENCODING'] = 'ascii'
    result = subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap,
        json.dumps(roots), str(actual), json.dumps(arguments)],
        cwd=case.source.parent, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    # CLI JSON must stay valid even through an ASCII/legacy Windows text pipe.
    output = result.stdout.decode('ascii', 'strict')
    return result, json.loads(output) if output else None


def arguments(case):
    return ('--workspace', case.workspace, '--edition', case.edition,
            '--profile', case.profile.id, '--source', 'en', '--target', 'uk', '--budget', '0')


def test_actual_cli_previews_then_saves_only_explicit_unchanged_layout_jobs(bound):
    before = list(bound.state['calls'])
    result, preview = call(bound, 'docx-preview', *arguments(bound))
    assert result.returncode == 0, result.stderr
    assert preview['layout']['paragraphs'] == 6
    assert preview['layout']['quotation_authority'] is False
    assert preview['provider_calls_started'] is False
    assert not bound.service.list()
    result, prepared = call(bound, 'docx-submit', *arguments(bound),
        '--approve-local', '--review-sha256', preview['review_sha256'])
    assert result.returncode == 0, result.stderr
    assert len(prepared['jobs']) == 6 and prepared['provider_calls_started'] is False
    assert bound.state['calls'] == before
    assert bound.source.read_bytes() == bound.original


@pytest.mark.parametrize('mode', ['wrong-review', 'wrong-destination'])
def test_actual_cli_rejects_changed_review_or_remote_consent_for_local_profile(bound, mode):
    result, preview = call(bound, 'docx-preview', *arguments(bound))
    assert result.returncode == 0, result.stderr
    digest = '0' * 64 if mode == 'wrong-review' else preview['review_sha256']
    approval = '--approve-remote' if mode == 'wrong-destination' else '--approve-local'
    result, value = call(bound, 'docx-submit', *arguments(bound),
                          approval, '--review-sha256', digest)
    assert result.returncode == 1 and value is None
    assert not bound.service.list() and bound.source.read_bytes() == bound.original


def test_actual_cli_complete_jobs_export_offline_and_refuse_existing_file(bound):
    layout = _document.captured(bound)
    identities = _document.prepared(bound, layout)
    assert run_documents(bound.service, identities)['status'] == 'complete'
    ProfileStore(bound.app.root / 'state/providers').remove(bound.profile.id)
    before = list(bound.state['calls'])
    scope = ('--workspace', bound.workspace, '--edition', bound.edition)
    result, value = call(bound, 'docx-run', *scope, *reversed(identities))
    assert result.returncode == 0 and value['status'] == 'complete', result.stderr
    output = bound.source.parent / 'CLI translated Ω.docx'
    result, receipt = call(bound, 'docx-export', *scope, '--output', output, *reversed(identities))
    assert result.returncode == 0, result.stderr
    assert receipt['status'] == 'complete' and receipt['format'] == 'docx'
    assert receipt['quotation_authority'] is False and output.is_file()
    committed = output.read_bytes()
    result, value = call(bound, 'docx-export', *scope, '--output', output, *identities)
    assert result.returncode == 1 and value is None
    assert output.read_bytes() == committed and bound.source.read_bytes() == bound.original
    assert bound.state['calls'] == before


def test_actual_cli_unfinished_jobs_require_the_original_current_profile(bound):
    layout = _document.captured(bound)
    identities = _document.prepared(bound, layout)
    ProfileStore(bound.app.root / 'state/providers').remove(bound.profile.id)
    before = list(bound.state['calls'])
    result, value = call(bound, 'docx-run', '--workspace', bound.workspace,
                          '--edition', bound.edition, *identities)
    assert result.returncode == 1 and value is None
    assert all(not job['receipts'] for job in bound.service.list())
    assert bound.state['calls'] == before

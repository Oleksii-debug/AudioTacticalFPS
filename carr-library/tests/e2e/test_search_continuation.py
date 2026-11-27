"""Actual CLI processes and approved assistant plans share bounded continuation."""
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import Plan, Scope, Step
from carr_library.assistant.service import AssistantService


def _fixture_module(name, path):
    """Load only the checked-in peer fixture, also under standalone pytest."""
    spec = spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError('The exact search fixture is unavailable: ' + str(path))
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish_text_corpus = _fixture_module('carr_continuation_corpus_fixture',
    Path(__file__).resolve().parents[1] / 'search' / 'test_search_pagination.py').publish_text_corpus


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Continuation entrypoint')
    identities, _ = publish_text_corpus(app, workspace, count=3)
    env = dict(os.environ, PYTHONUTF8='1')
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'src'), str(ROOT)))

    def invoke(*arguments, expected=0):
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'carr_library', '--data-dir', str(app.root), *arguments],
            cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == expected, (result.returncode, result.stdout, result.stderr)
        assert 'Traceback' not in result.stderr
        return result
    return app, workspace, identities, invoke


def test_cli_pages_continue_across_real_processes_and_preserve_exact_source_ids(source):
    _, workspace, identities, invoke = source
    args = ('--search', 'Needle', '--workspace', workspace, '--limit-per-library', '1')
    cursor, actual = None, []
    for number in range(1, 4):
        option = ('--search-page',) if cursor is None else ('--search-cursor', cursor)
        document = json.loads(invoke(*args, *option).stdout)
        assert document['page_number'] == number and document['total_libraries'] == 1
        assert 'never a global score' in document['ordering']
        rows = [row for group in document['libraries'] for row in group['results']]
        assert len(rows) == 1 and rows[0]['workspace'] == workspace and rows[0]['verified']
        actual.append(rows[0]['edition_id'])
        cursor = document['next_cursor']
    assert actual == list(identities) and cursor is None


def test_cli_refuses_changed_cursor_options_without_partial_output(source):
    _, workspace, _, invoke = source
    args = ('--search', 'Needle', '--workspace', workspace, '--limit-per-library', '1')
    first = json.loads(invoke(*args, '--search-page').stdout)
    result = invoke('--search', 'different', '--workspace', workspace,
                    '--limit-per-library', '1', '--search-cursor', first['next_cursor'], expected=1)
    assert not result.stdout.strip() and 'another query' in result.stderr


@pytest.mark.parametrize('option', ('--search-page', '--search-cursor'))
def test_cli_continuation_requires_an_explicit_search(source, option):
    _, _, _, invoke = source
    args = (option,) if option == '--search-page' else (option, 'bounded-cursor')
    result = invoke(*args, expected=2)
    assert 'require --search' in result.stderr


def test_approved_assistant_tool_returns_cursor_and_only_continues_after_a_new_plan(source):
    app, workspace, identities, _ = source
    service = AssistantService(app)
    scope = Scope(workspace_id=workspace)
    cursor, observed = None, []
    for number in range(1, 4):
        arguments = {'query': 'Needle', 'limit': 1}
        if cursor is not None:
            arguments['cursor'] = cursor
        plan = Plan(scope, (Step('library.search', arguments),), 'One source page only')
        job = service.submit(plan)
        assert service.get(job).status == 'queued'
        report = service.run(job)
        assert report.status == 'complete' and report.completed == report.total == 1, report
        result, = report.results
        assert result['page_number'] == number and len(result['results']) == 1
        hit, = result['results']
        assert hit['verified'] and hit['workspace'] == workspace
        assert app.copy(workspace, hit['ref'], hit['revision'], 'quote') == hit['quote'] == 'Needle'
        observed.append(hit['edition_id'])
        cursor = result['next_cursor']
        assert len(service.jobs()) == number
    assert observed == list(identities) and cursor is None


@pytest.mark.parametrize('cursor', ('', 'x' * 8193, True, []))
def test_assistant_cursor_validation_does_not_expand_tool_permissions(source, cursor):
    app, workspace, _, _ = source
    service = AssistantService(app)
    plan = Plan(Scope(workspace_id=workspace),
                (Step('library.search', {'query': 'Needle', 'cursor': cursor}),))
    with pytest.raises(ValueError):
        service.submit(plan)
    assert service.jobs() == ()

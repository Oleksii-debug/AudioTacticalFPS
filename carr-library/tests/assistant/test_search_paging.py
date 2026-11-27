"""Assistant uses the canonical bounded search cursor, never a second searcher."""
from dataclasses import replace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step


def run(service, scope, arguments):
    identity = service.submit(Plan(scope, (Step('library.search', arguments),)))
    return service.run(identity)


def test_assistant_visits_all_real_source_windows_and_returns_opaque_continuations(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Paged source')
    imported = app.add_text(workspace, 'More than one source window',
        '\f'.join('Needle retained segment ' + str(index) + '.' for index in range(205)))
    scope = Scope(workspace, max_seconds=300)
    service = AssistantService(app)
    cursor, pages, seen = None, [], []
    while True:
        result = run(service, scope, {'query': 'Needle', 'mode': 'terms', 'limit': 100,
                                     'cursor': cursor, 'offset': 0})
        assert result.status == 'complete', result
        page, = result.results
        pages.append(page['source_pages'])
        assert page['source_editions'] == 1 and 0 <= page['source_characters'] <= 5_000_000
        assert len(page['window_identity']) == 64 and len(page['results']) <= 100
        assert page['cursor']['schema'] == 'carr-source-search-cursor/1'
        assert all(hit['workspace'] == workspace and hit['edition_id'] == imported['edition']
                   for hit in page['results'])
        seen.extend(hit['ref'] for hit in page['results'])
        cursor = page['next_source_cursor']
        if cursor is None:
            break
        assert len(pages) < 4
    assert pages == [100, 100, 5]
    assert len(seen) == len(set(seen)) == 205


@pytest.mark.parametrize('change', ['workspace', 'query', 'revision'])
def test_changed_scope_or_source_rejects_an_existing_search_cursor(tmp_path, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original source scope')
    other = app.create_workspace('Foreign private scope')
    imported = app.add_text(workspace, 'Paged', '\f'.join('Needle record ' + str(i) for i in range(101)))
    app.add_text(other, 'Foreign', 'Needle private result')
    service, scope = AssistantService(app), Scope(workspace, max_seconds=300)
    first = run(service, scope, {'query': 'Needle', 'limit': 100})
    assert first.status == 'complete'
    args = {'query': 'Needle', 'cursor': first.results[0]['next_source_cursor']}
    assert args['cursor'] is not None
    if change == 'workspace':
        scope = replace(scope, workspace_id=other)
    elif change == 'query':
        args['query'] = 'private'
    else:
        app.update_metadata(workspace, imported['edition'], title='New active revision')
    result = run(service, scope, args)
    assert result.status == 'failed' and result.completed == 0
    assert not result.results


def test_result_paging_keeps_exact_cursor_and_does_not_reset_source_window(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Result pages')
    app.add_text(workspace, 'Three positions', 'Needle first\fNeedle second\fNeedle third')
    service, scope = AssistantService(app), Scope(workspace)
    first = run(service, scope, {'query': 'Needle', 'mode': 'terms', 'limit': 1})
    assert first.status == 'complete'
    page = first.results[0]
    assert page['has_more_results'] is True and page['source_pages'] == 3
    second = run(service, scope, {'query': 'Needle', 'mode': 'terms', 'limit': 1,
                                  'cursor': page['cursor'], 'offset': 1})
    assert second.status == 'complete'
    following = second.results[0]
    assert following['cursor'] == page['cursor'] and following['window_identity'] == page['window_identity']
    assert following['results'][0]['ref'] != page['results'][0]['ref']
    assert following['offset'] == 1


@pytest.mark.parametrize('extra', [
    {'cursor': 'not opaque JSON'}, {'offset': True}, {'offset': -1},
    {'cursor': {'schema': 'carr-source-search-cursor/1', 'scope': 'invented',
                'edition_after': None, 'after_page': None}},
    {'_source_page': {}}, {'_page_info': {}},
])
def test_invalid_navigation_arguments_are_rejected_before_source_reads(tmp_path, extra, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Scope')
    service, scope = AssistantService(app), Scope(workspace)
    def fail(*args, **kwargs):
        pytest.fail('Invalid cursor/input cannot reach native source search.')
    monkeypatch.setattr(app, 'search_page', fail)
    with pytest.raises(ValueError):
        service.submit(Plan(scope, (Step('library.search', {'query': 'Needle', **extra}),)))

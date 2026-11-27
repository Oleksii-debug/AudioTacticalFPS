"""Real isolated CLI processes: cursors survive process exit and keep source scope.

Child imports come only from the product already imported by this test. An
installed-wheel run must not accidentally substitute checkout/src via PYTHONPATH.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher
from carr_library.application import LibraryApp


CHILD = r"""
import json
from pathlib import Path
import runpy
import sys
contract = json.loads(sys.argv[1])
sys.path[:0] = contract['roots']
import carr_library
from bootstrap.vendor.cairn_extractors import hasher
assert Path(carr_library.__file__).resolve() == Path(contract['product'])
assert Path(hasher.__file__).resolve() == Path(contract['hasher'])
sys.argv = ['carr_library', *sys.argv[2:]]
runpy.run_module('carr_library', run_name='__main__')
"""


def invoke(root, *arguments, expected=0):
    product = Path(carr_library.__file__).resolve()
    extractor = Path(hasher.__file__).resolve()
    contract = dict(product=str(product), hasher=str(extractor),
                    roots=list(dict.fromkeys((str(product.parent.parent),
                                              str(extractor.parents[3])))))
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run(
        [sys.executable, '-I', '-B', '-X', 'utf8', '-c', CHILD,
         json.dumps(contract), '--data-dir', str(root), *map(str, arguments)],
        cwd=root.parent, env=environment, text=True, encoding='utf-8',
        capture_output=True, timeout=45)
    assert result.returncode == expected, (result.returncode, result.stdout, result.stderr)
    assert 'Traceback' not in result.stderr
    return result


def run_page(app, workspaces, *, cursor=None, query='Railway', **options):
    arguments = ['--search', query, '--search-pages']
    for workspace in workspaces:
        arguments.extend(('--workspace', workspace))
    for name, value in options.items():
        arguments.extend(('--' + name.replace('_', '-'), str(value)))
    if cursor is not None:
        arguments.extend(('--search-cursor', cursor))
    return json.loads(invoke(app.root, *arguments).stdout)


def collect(app, workspaces, **options):
    cursor, pages = None, []
    for _ in range(64):
        page = run_page(app, workspaces, cursor=cursor, **options)
        pages.append(page)
        assert type(page['complete']) is bool
        assert page['complete'] == (page['next_cursor'] is None)
        assert page['selected_libraries'] == len(workspaces)
        if page['complete']:
            return pages
        assert page['next_cursor'] != cursor, 'A successful page must make forward progress.'
        cursor = page['next_cursor']
    raise AssertionError('CLI continuation did not reach the finite corpus boundary.')


def test_isolated_cli_reopens_and_reaches_every_retained_source_segment(tmp_path):
    app = LibraryApp(tmp_path / 'private library Україна')
    workspace = app.create_workspace('Selected source')
    untouched = app.create_workspace('Unselected source')
    texts = [f'Railway evidence number {number:04d} — незмінний оригінал.' for number in range(205)]
    assert app.add_text(workspace, 'Real retained TXT', '\f'.join(texts))['state'] == 'complete'
    assert app.add_text(untouched, 'Must stay absent', 'Railway foreign scope.')['state'] == 'complete'
    before = {path.name: sha256(path.read_bytes()).hexdigest() for path in app.assets_dir.glob('*.txt')}
    pages = collect(app, [workspace], limit_per_library=37, search_page_sources=41)
    rows = [row for page in pages for group in page['libraries'] for row in group['results']]
    assert len(rows) == len({row['page'] for row in rows}) == 205
    assert {row['quote'] for row in rows} == set(texts)
    assert all(row['workspace'] == workspace and row['verified'] for row in rows)
    assert len(pages) >= 6 and all(page['scanned_segments'] <= 41 for page in pages)
    assert all('window' in page['ordering'] for page in pages)
    assert before == {path.name: sha256(path.read_bytes()).hexdigest() for path in app.assets_dir.glob('*.txt')}


def test_cli_pages_all_twenty_five_explicitly_selected_libraries(tmp_path):
    app = LibraryApp(tmp_path / 'many libraries')
    workspaces = []
    for number in range(25):
        workspace = app.create_workspace(f'Library {number:02d}')
        workspaces.append(workspace)
        assert app.add_text(workspace, f'Source {number}', f'Railway in library {number}.')['state'] == 'complete'
    order = workspaces[::2] + workspaces[1::2]
    pages = collect(app, order, search_library_budget=7, limit_per_library=3)
    groups = [group for page in pages for group in page['libraries']]
    assert [group['id'] for group in groups] == order
    assert all(len(group['results']) == 1 for group in groups)
    assert all(page['scanned_libraries'] <= 7 for page in pages)


def test_cli_empty_window_is_continuable_and_never_false_completion(tmp_path):
    app = LibraryApp(tmp_path / 'sparse')
    workspace = app.create_workspace('Sparse source')
    assert app.add_text(workspace, 'Sparse', 'Unrelated one.\fUnrelated two.\fRailway at the end.')['state'] == 'complete'
    pages = collect(app, [workspace], search_page_sources=1)
    assert not pages[0]['complete'] and not pages[0]['libraries'][0]['results']
    assert not pages[1]['complete'] and not pages[1]['libraries'][0]['results']
    rows = [row for page in pages for group in page['libraries'] for row in group['results']]
    assert [row['quote'] for row in rows] == ['Railway at the end.']


def test_cli_continuation_refuses_changed_query_without_partial_json(tmp_path):
    app = LibraryApp(tmp_path / 'bound')
    workspace = app.create_workspace('Bound source')
    assert app.add_text(workspace, 'Bound', 'Railway first.\fRailway second.')['state'] == 'complete'
    first = run_page(app, [workspace], limit_per_library=1)
    assert first['next_cursor']
    refused = invoke(app.root, '--search', 'different', '--workspace', workspace,
        '--search-cursor', first['next_cursor'], expected=1)
    assert not refused.stdout.strip()
    assert 'start a new search' in refused.stderr.lower()
    retried = run_page(app, [workspace], cursor=first['next_cursor'], limit_per_library=1)
    assert retried['libraries'][0]['results'][0]['quote'] == 'Railway second.'


@pytest.mark.parametrize('options', [
    ('--search-pages',),
    ('--search-cursor', 'untrusted-token'),
    ('--search-page-sources', '2'),
    ('--search-library-budget', '1'),
], ids=['paging', 'cursor', 'source-budget', 'library-budget'])
def test_paging_flags_without_search_create_no_library(tmp_path, options):
    root = tmp_path / 'must remain absent'
    result = invoke(root, *options, expected=2)
    assert 'require --search' in result.stderr
    assert not root.exists()

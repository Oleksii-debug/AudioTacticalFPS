"""Actual guarded CLI children, durable pages and explicit import/review."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker


def invoke(app, workspace, *arguments, timeout=120):
    # The child exercises the same source or installed modules already selected
    # by its parent. It cannot silently prefer this test's checkout.
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))))
    result = subprocess.run([sys.executable, '-B', '-s', '-P', '-X', 'utf8',
        '-m', 'carr_library', 'import-batches', '--data-dir', str(app.root),
        '--workspace', workspace, *map(str, arguments)], env=environment,
        capture_output=True, text=True, encoding='utf-8', errors='strict', timeout=timeout)
    return result


def accepted(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def selected(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit CLI source pages')
    collection = app.create_collection(workspace, 'Reviewed collection')
    folder = tmp_path / 'selected'
    folder.mkdir()
    for name in ('a.txt', 'b.txt', 'c.txt'):
        (folder / name).write_text('Exact CLI source ' + name + '.', encoding='utf-8')
    return app, workspace, collection, folder


def test_actual_cli_continuation_is_passive_and_run_imports_only_selected_page(selected):
    app, workspace, collection, folder = selected
    originals = {path.name: path.read_bytes() for path in folder.iterdir()}
    first = accepted(invoke(app, workspace, 'plan', folder, '--collection', collection,
        '--ocr-languages', 'eng', '--page-files', 2, '--scan-entries', 40, '--scan-seconds', 30))
    assert first['version'] == 2 and first['counts']['queued'] == 2
    assert first['scan']['limits'] == {'max_files': 2, 'max_entries': 40,
                                      'timeout': 30.0, 'max_bytes': 4 * 1024**2}
    assert first['collections'] == [collection] and app.browse(workspace) == ()
    row = first['rows'][1]
    excluded = accepted(invoke(app, workspace, 'exclude', first['batch_id'], row['row_id']))
    assert excluded['counts']['excluded'] == 1 and excluded['rows'][1]['row_id'] == row['row_id']
    second = accepted(invoke(app, workspace, 'continue', first['batch_id'], '--page-files', 7))
    assert second['batch_id'] == first['scan']['next'] and second['scan']['previous'] == first['batch_id']
    assert second['total'] == 1 and second['scan']['cursor']['complete']
    assert second['scan']['cursor']['planned'] == 3
    assert second['collections'] == first['collections'] and second['config'] == first['config']
    assert app.browse(workspace) == ()
    repeated = accepted(invoke(app, workspace, 'continue', first['batch_id'], '--page-files', 1))
    assert repeated == second
    summaries = accepted(invoke(app, workspace, 'list'))
    assert {item['batch_id'] for item in summaries} == {first['batch_id'], second['batch_id']}
    assert accepted(invoke(app, workspace, 'show', first['batch_id'])) == excluded
    imported = accepted(invoke(app, workspace, 'run', first['batch_id']))
    assert imported['counts']['complete'] == 1 and imported['counts']['excluded'] == 1
    assert imported['rows'][0]['request_id'] == first['rows'][0]['request_id']
    assert imported['rows'][0]['result']['isolated'] and imported['rows'][0]['result']['verified']
    assert len(app.browse(workspace, collection=collection)) == 1
    assert accepted(invoke(app, workspace, 'show', second['batch_id']))['counts']['queued'] == 1
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == originals
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


def test_actual_cli_restored_cursor_refuses_continuation_and_never_auto_runs(selected, tmp_path):
    app, workspace, collection, folder = selected
    first = accepted(invoke(app, workspace, 'plan', folder, '--collection', collection, '--page-files', 1))
    old_rows = deepcopy(first['rows'])
    backup = tmp_path / 'saved'
    digest = create_backup(app, backup)
    shutil.rmtree(app.root)
    shutil.rmtree(folder)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    saved = accepted(invoke(restored, workspace, 'show', first['batch_id']))
    assert saved['scan']['review_required'] and saved['scan']['cursor'] == first['scan']['cursor']
    assert saved['counts']['queued'] == 1 and restored.browse(workspace) == ()
    denied = invoke(restored, workspace, 'continue', first['batch_id'])
    assert denied.returncode == 1 and 'fresh explicit selection' in denied.stderr
    assert denied.stdout == '' and restored.browse(workspace) == ()
    assert accepted(invoke(restored, workspace, 'show', first['batch_id'])) == saved
    imported = accepted(invoke(restored, workspace, 'run', first['batch_id']))
    assert imported['counts']['complete'] == 1
    assert imported['rows'][0]['request_id'] == old_rows[0]['request_id']
    assert imported['rows'][0]['manifest'] == old_rows[0]['manifest']
    assert imported['rows'][0]['result']['isolated'] and imported['rows'][0]['result']['verified']
    assert len(restored.browse(workspace, collection=collection)) == 1


def test_actual_cli_wrong_workspace_and_page_budget_never_rewrite_existing_plan(selected):
    app, workspace, _collection, folder = selected
    first = accepted(invoke(app, workspace, 'plan', folder, '--page-files', 1))
    second_workspace = app.create_workspace('Separate CLI scope')
    denied = invoke(app, second_workspace, 'continue', first['batch_id'])
    assert denied.returncode == 1 and denied.stdout == ''
    invalid = invoke(app, workspace, 'continue', first['batch_id'], '--page-files', 5001)
    assert invalid.returncode == 1 and invalid.stdout == ''
    assert accepted(invoke(app, workspace, 'show', first['batch_id'])) == first
    assert len(accepted(invoke(app, workspace, 'list'))) == 1
    assert app.browse(workspace) == () and app.browse(second_workspace) == ()
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []

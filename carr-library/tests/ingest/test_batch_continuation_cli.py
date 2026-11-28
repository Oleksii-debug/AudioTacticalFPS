"""Actual selected CLI children; reviewed pages never imply automatic import."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import carr_library
from carr_library.application import LibraryApp
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.operations import ResourceBroker


def invoke(app, workspace, *arguments, timeout=120, standalone=False):
    selected = Path(carr_library.__file__).resolve().parent
    proof = {
        'source_selection': selected == (Path(__file__).parents[2] / 'src' / 'carr_library').resolve(),
        'files': {path.relative_to(selected).as_posix(): sha256(path.read_bytes()).hexdigest()
                  for path in selected.rglob('*.py')}}
    proof_path = app.root.parent / 'import-cli-selected-package.json'
    raw = json.dumps(proof, sort_keys=True).encode()
    proof_path.write_bytes(raw)
    child = r'''
import json, sys, sysconfig
from hashlib import sha256
from pathlib import Path
import carr_library
expected, proof_path, proof_digest, standalone = sys.argv[1:5]
raw = Path(proof_path).read_bytes()
assert sha256(raw).hexdigest() == proof_digest
proof = json.loads(raw)
actual = Path(carr_library.__file__).resolve().parent
allowed = {Path(expected).resolve()}
if proof['source_selection'] is True:
    allowed.add((Path(sysconfig.get_path('purelib')) / 'carr_library').resolve())
assert actual in allowed, (actual, allowed)
files = {path.relative_to(actual).as_posix(): sha256(path.read_bytes()).hexdigest()
         for path in actual.rglob('*.py')}
assert files == proof['files'], 'Selected CLI package differs from parent file/hash inventory.'
if standalone == '1':
    from carr_library.ingest import batch_cli
    assert Path(batch_cli.__file__).resolve() == actual / 'ingest' / 'batch_cli.py'
    result = batch_cli.main(sys.argv[5:])
else:
    from carr_library import __main__
    assert Path(__main__.__file__).resolve() == actual / '__main__.py'
    result = __main__.main(['import-batch', *sys.argv[5:]])
raise SystemExit(result)
'''
    return subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', child,
        str(selected), str(proof_path), sha256(raw).hexdigest(), '1' if standalone else '0',
        '--data-dir', str(app.root), '--workspace', workspace, *map(str, arguments)],
        cwd=app.root.parent, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, text=True, encoding='utf-8', errors='strict', timeout=timeout)


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


def test_actual_cli_explicit_profile_stays_frozen_through_partial_source_resume(selected):
    app, workspace, collection, folder = selected
    source = folder / 'segmented.txt'
    original = b'First exact source.\fSecond exact source.\fThird exact source.'
    source.write_bytes(original)
    first = accepted(invoke(app, workspace, 'plan', source, '--collection', collection,
        '--source-mib', 2, '--segment-mib', 1, '--segments', 3, '--steps', 1,
        '--seconds', 90, '--memory-mib', 512, '--library-mib', 64, '--temporary-mib', 128))
    selected_budget = ImportBudget(source_bytes=2*MIB, segment_bytes=MIB, segments=3,
        steps=1, seconds=90, memory_bytes=512*MIB, library_bytes=64*MIB, temporary_bytes=128*MIB)
    assert first['version'] == 3 and first['import_budget'] == selected_budget.to_dict()
    identities = {key: first['rows'][0][key] for key in ('row_id', 'request_id', 'work_id', 'edition_id')}
    partial = invoke(app, workspace, 'run', first['batch_id'], '--max-items', 1)
    assert partial.returncode == 2, partial.stdout + partial.stderr
    current = json.loads(partial.stdout)
    assert current['rows'][0]['state'] == 'queued'
    assert current['rows'][0]['result']['state'] == 'pending'
    assert current['rows'][0]['result']['completed'] == 1
    assert app.browse(workspace) == ()
    source.unlink()
    for _ in range(3):
        result = invoke(app, workspace, 'run', first['batch_id'], '--max-items', 1, standalone=True)
        assert result.returncode in (0, 2), result.stdout + result.stderr
        current = json.loads(result.stdout)
        assert current['import_budget'] == selected_budget.to_dict()
        assert {key: current['rows'][0][key] for key in identities} == identities
        if result.returncode == 0:
            break
    assert current['state'] == 'complete' and current['rows'][0]['result']['verified']
    assert len(app.browse(workspace, collection=collection)) == 1
    hit, = app.search(workspace, 'Third exact source')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Third exact source'
    assert app.assets.resolve(current['rows'][0]['manifest']['sha256']).read_bytes() == original
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


@pytest.mark.parametrize('automatic', [False, True])
def test_cli_captures_exact_seven_or_eight_ocr_fields_without_launch(selected, automatic):
    from carr_library.extract.page_ocr import Config, ExtendedConfig
    app, workspace, _collection, folder = selected
    flags = ['--auto-ocr-languages'] if automatic else []
    plan = accepted(invoke(app, workspace, 'plan', folder / 'a.txt',
        '--ocr-languages', 'eng+ukr+slk', '--review-default-budget', *flags))
    expected = ExtendedConfig(languages='eng+ukr+slk') if automatic else Config(languages='eng+ukr+slk')
    assert plan['config'] == asdict(expected) and len(plan['config']) == (8 if automatic else 7)
    assert plan['import_budget'] == ImportBudget().to_dict() and plan['version'] == 3
    assert app.browse(workspace) == ()
    assert not (app.root / 'import-requests').exists()
    assert not (app.root / 'imports.sqlite').exists()


@pytest.mark.parametrize('kind', ['absent-root', 'empty-root', 'file-root', 'aliased-database'])
def test_cli_requires_existing_regular_library_before_constructing_app(tmp_path, capsys, kind):
    from types import SimpleNamespace
    from carr_library.ingest import batch_cli
    root = tmp_path / 'chosen-library'
    if kind == 'empty-root':
        root.mkdir()
    elif kind == 'file-root':
        root.write_bytes(b'not a library directory')
    elif kind == 'aliased-database':
        existing = LibraryApp(root)
        existing.create_workspace('Existing library')
        os.link(root / 'library.sqlite', tmp_path / 'database-alias')
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
              for path in tmp_path.rglob('*') if path.is_file()}
    code = batch_cli.run(['--data-dir', str(root), '--workspace', 'missing-workspace', 'list'])
    assert code == 1 and capsys.readouterr().out == ''
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
             for path in tmp_path.rglob('*') if path.is_file()}
    assert after == before
    if kind == 'absent-root':
        assert not root.exists()
    elif kind == 'empty-root':
        assert tuple(root.iterdir()) == ()
    elif kind == 'file-root':
        assert root.read_bytes() == b'not a library directory'
    else:
        assert (root / 'library.sqlite').stat().st_nlink == 2


@pytest.mark.parametrize('flag,value', [
    ('--source-mib', '0'), ('--segment-mib', '65'), ('--segments', '2147483648'),
    ('--steps', '1002'), ('--seconds', 'nan'), ('--seconds', '86401'),
    ('--memory-mib', '1'), ('--library-mib', '1'), ('--temporary-mib', '1')])
def test_invalid_cli_reviewed_budget_has_no_library_or_plan_side_effect(tmp_path, capsys, flag, value):
    from carr_library.ingest import batch_cli
    root = tmp_path / 'never-created'
    code = batch_cli.run(['--data-dir', str(root), '--workspace', 'unused',
        'plan', str(tmp_path / 'not-opened.txt'), flag, value])
    assert code == 1 and capsys.readouterr().out == ''
    assert not root.exists()

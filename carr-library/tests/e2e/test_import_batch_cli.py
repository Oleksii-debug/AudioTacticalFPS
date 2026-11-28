"""Real isolated current-package Import Center commands; no source-tree fallback."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import carr_library
from bootstrap.vendor.cairn_extractors import hasher
from carr_library.application import LibraryApp
from carr_library.ingest.worker import load_request


CHILD = r'''
import json, runpy, sys
from pathlib import Path
contract = json.loads(sys.argv[1])
sys.path[:0] = contract['roots']
import carr_library
from bootstrap.vendor.cairn_extractors import hasher
assert Path(carr_library.__file__).resolve() == Path(contract['product'])
assert Path(hasher.__file__).resolve() == Path(contract['hasher'])
sys.argv = ['carr_library', 'import-batch', *sys.argv[2:]]
runpy.run_module('carr_library', run_name='__main__')
'''


def invoke(root, workspace, *arguments, status=0):
    product, extractor = Path(carr_library.__file__).resolve(), Path(hasher.__file__).resolve()
    contract = dict(product=str(product), hasher=str(extractor),
        roots=list(dict.fromkeys((str(product.parent.parent), str(extractor.parents[3])))))
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    completed = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c',
        CHILD, json.dumps(contract), '--data-dir', str(root), '--workspace', workspace,
        *map(str, arguments)], cwd=root.parent, env=environment, capture_output=True,
        text=True, encoding='utf-8', timeout=45)
    assert completed.returncode == status, (completed.stdout, completed.stderr)
    assert 'Traceback' not in completed.stderr
    return completed


def test_actual_cli_plan_continue_list_show_and_row_choices_do_not_import(tmp_path):
    app = LibraryApp(tmp_path / 'library Україна')
    workspace = app.create_workspace('Saved CLI pages')
    folder = tmp_path / 'Оригінали'
    folder.mkdir()
    (folder / 'a.txt').write_text('Exact first CLI source.', encoding='utf-8')
    (folder / 'b.txt').write_text('Exact second CLI source.', encoding='utf-8')
    first = json.loads(invoke(app.root, workspace, 'plan', folder,
        '--page-files', 1, '--review-default-budget', '--source-mib', 2,
        '--memory-mib', 768, '--library-mib', 64, '--temporary-mib', 128,
        '--segment-mib', 1, '--steps', 2).stdout)
    assert first['version'] == 3 and first['total'] == 1
    assert first['source_profile']['schema'] == 'carr-streamed-text-profile/2'
    assert first['rows'][0]['file'] == 'a.txt'
    shown = json.loads(invoke(app.root, workspace, 'show', first['batch_id']).stdout)
    assert shown == first
    listed = json.loads(invoke(app.root, workspace, 'list').stdout)
    assert [item['batch_id'] for item in listed] == [first['batch_id']]
    second = json.loads(invoke(app.root, workspace, 'continue', first['batch_id'],
                               '--page-files', 1).stdout)
    assert second['rows'][0]['file'] == 'b.txt'
    assert second['scan']['previous'] == first['batch_id']
    assert second['source_profile'] == first['source_profile']
    excluded = json.loads(invoke(app.root, workspace, 'exclude',
        second['batch_id'], second['rows'][0]['row_id']).stdout)
    assert excluded['rows'][0]['state'] == 'excluded'
    included = json.loads(invoke(app.root, workspace, 'include',
        second['batch_id'], second['rows'][0]['row_id']).stdout)
    assert included['rows'][0]['state'] == 'queued'
    assert included['rows'][0]['request_id'] == second['rows'][0]['request_id']
    assert not list((app.root / 'originals').iterdir())
    assert not list(app.root.glob('import-requests/*.json'))
    with app.read(workspace) as catalog:
        assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0


def test_actual_cli_partial_slice_reopens_same_frozen_request_after_source_removal(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual CLI continuation')
    source = tmp_path / 'three.txt'
    source.write_bytes('First Ω.\fSecond Ї.\fThird exact CLI.'.encode('utf-8'))
    plan = json.loads(invoke(app.root, workspace, 'plan', source, '--review-default-budget',
        '--source-mib', 2, '--segment-mib', 1, '--steps', 2,
        '--memory-mib', 768, '--library-mib', 64, '--temporary-mib', 128).stdout)
    first = json.loads(invoke(app.root, workspace, 'run', plan['batch_id'], status=2).stdout)
    row, = first['rows']
    assert row['state'] == 'queued' and row['result']['state'] == 'pending'
    assert row['result']['completed'] == 2
    request = load_request(app.root, row['request_id'])
    source.unlink()
    final = json.loads(invoke(app.root, workspace, 'run', plan['batch_id']).stdout)
    assert final['state'] == 'complete'
    assert final['rows'][0]['request_id'] == row['request_id']
    assert final['rows'][0]['edition_id'] == row['edition_id']
    assert final['rows'][0]['result']['id'] == row['result']['id']
    assert load_request(app.root, row['request_id'])['source_profile'] == request['source_profile']
    hit, = app.search(workspace, 'Third exact CLI')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Third exact CLI'


@pytest.mark.parametrize('arguments', [
    ('plan', 'unselected.txt', '--source-mib', '0'),
    ('plan', 'unselected.txt', '--memory-mib', '1'),
    ('plan', 'unselected.txt', '--seconds', 'nan'),
    ('plan', 'unselected.txt', '--page-files', '5001'),
    ('plan', 'unselected.txt', '--scan-entries', '20001'),
    ('plan', 'unselected.txt', '--scan-seconds', '601'),
])
def test_invalid_explicit_cli_policy_has_no_source_or_library_side_effect(tmp_path, arguments):
    root = tmp_path / 'existing-library'
    app = LibraryApp(root)
    workspace = app.create_workspace('Refused explicit choice')
    result = invoke(root, workspace, *arguments, status=1)
    assert 'Import Center:' in result.stderr
    assert not list(root.glob('import-batches/*.json'))
    assert not list(root.glob('import-requests/*.json'))
    assert not list(app.assets_dir.iterdir())
    with app.read(workspace) as catalog:
        assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0


def test_continuation_cannot_accept_a_new_resource_policy_from_cli(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Frozen CLI profile')
    source = tmp_path / 'source.txt'
    source.write_text('Exact saved source.', encoding='utf-8')
    plan = json.loads(invoke(app.root, workspace, 'plan', source,
                            '--review-default-budget').stdout)
    path = app.root / 'import-batches' / (plan['batch_id'] + '.json')
    before = path.read_bytes()
    rejected = invoke(app.root, workspace, 'continue', plan['batch_id'],
                      '--source-mib', 1024, status=2)
    assert 'unrecognized arguments' in rejected.stderr
    assert path.read_bytes() == before


@pytest.mark.parametrize('automatic', [False, True], ids=['manual', 'automatic'])
def test_actual_cli_ocr_choice_roundtrips_without_rebinding_saved_pages(tmp_path, automatic):
    from carr_library.extract.page_ocr import Config

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact CLI OCR settings')
    folder = tmp_path / 'sources'
    folder.mkdir()
    (folder / 'a.txt').write_text('First original for OCR settings.', encoding='utf-8')
    (folder / 'b.txt').write_text('Second original for OCR settings.', encoding='utf-8')
    flags = ('--auto-ocr-languages',) if automatic else ()
    first = json.loads(invoke(app.root, workspace, 'plan', folder,
        '--ocr-languages', 'eng+ukr', '--page-files', 1, *flags).stdout)
    expected = dict(languages='eng+ukr', dpi=300, timeout=30, max_pixels=20_000_000,
                    orientation=False, model_policy='default', model_profile='installed')
    if automatic:
        expected['automatic_languages'] = True
    assert first['config'] == expected
    assert ('automatic_languages' in first['config']) is automatic
    assert Config(**first['config']) == Config(languages='eng+ukr', automatic_languages=automatic)
    saved = app.root / 'import-batches' / (first['batch_id'] + '.json')
    before = saved.read_bytes()
    rejected = invoke(app.root, workspace, 'continue', first['batch_id'],
                      '--ocr-languages', 'eng', status=2)
    assert 'unrecognized arguments' in rejected.stderr
    assert saved.read_bytes() == before
    second = json.loads(invoke(app.root, workspace, 'continue', first['batch_id'],
                               '--page-files', 1).stdout)
    assert second['config'] == expected
    assert second['scan']['previous'] == first['batch_id']
    assert second['rows'][0]['file'] == 'b.txt'
    shown = json.loads(invoke(app.root, workspace, 'show', second['batch_id']).stdout)
    assert shown['config'] == expected
    assert not list(app.root.glob('import-requests/*.json'))
    assert not list(app.assets_dir.iterdir())
    assert app.browse(workspace) == ()

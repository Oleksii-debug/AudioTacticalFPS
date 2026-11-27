"""Actual headless entrypoint processes over real local research libraries."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
import carr_library
from carr_library.application import LibraryApp


PACKAGE = Path(carr_library.__file__).resolve()
BOOTSTRAP_HASHER = Path(bootstrap_hasher.__file__).resolve()


def cli_command(*arguments):
    # -B is not inherited from pytest by a newly launched interpreter. These
    # real CLI children must preserve the hash-sealed base runtime as the
    # supplied application launchers do. Keep source and installed runs bound
    # to the same distribution that their parent pytest process imported.
    return [sys.executable, '-B', '-s', '-P', '-X', 'utf8', *arguments]


def cli_environment():
    environment = dict(os.environ)
    # Source trees split src/carr_library and bootstrap across two roots;
    # wheels install both into the same site-packages directory. Derive both
    # trusted roots from imported modules instead of relying on cwd or an
    # unrelated package already installed in the test interpreter.
    roots = dict.fromkeys((PACKAGE.parent.parent, BOOTSTRAP_HASHER.parents[3]))
    environment['PYTHONPATH'] = os.pathsep.join(map(str, roots))
    environment['PYTHONUTF8'] = '1'
    return environment


def test_cli_child_uses_current_distribution_without_writing_runtime_bytecode(tmp_path):
    environment = cli_environment()
    cache = tmp_path / 'private-runtime-cache'
    environment['PYTHONPYCACHEPREFIX'] = str(cache)
    result = subprocess.run(cli_command('-c',
        'from __future__ import annotations\n'
        'import carr_library,json,pathlib,sys\n'
        'from bootstrap.vendor.cairn_extractors import hasher\n'
        'print(json.dumps({"package":str(pathlib.Path(carr_library.__file__).resolve()),'
        '"bootstrap_hasher":str(pathlib.Path(hasher.__file__).resolve()),'
        '"dont_write_bytecode":sys.dont_write_bytecode,"safe_path":sys.flags.safe_path,'
        '"no_user_site":sys.flags.no_user_site}))'),
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        encoding='utf-8', timeout=30, check=True)
    actual = json.loads(result.stdout)
    assert Path(actual['package']).samefile(PACKAGE)
    assert Path(actual['bootstrap_hasher']).samefile(BOOTSTRAP_HASHER)
    assert actual['dont_write_bytecode'] and actual['safe_path'] and actual['no_user_site']
    assert not list(cache.rglob('*.pyc'))


@pytest.fixture
def cli(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    selected = []
    for author, language in (('First author', 'uk'), ('Second author', 'sk'), ('Unselected author', 'en')):
        workspace = app.create_workspace('Identical library name')
        selected.append(workspace)
        imported = app.add_text(workspace, 'Synthetic source', 'Railway history — Straße.',
                                authors=(author,), language=language, edition='First')
        assert imported['state'] == 'complete'
    env = cli_environment()
    def run(*arguments, expected=0):
        result = subprocess.run(cli_command('-m', 'carr_library', '--data-dir', str(app.root),
                                 *map(str, arguments)), cwd=tmp_path, env=env, text=True,
                                encoding='utf-8', capture_output=True, timeout=30)
        assert result.returncode == expected, (result.stdout, result.stderr, result.returncode)
        assert 'Traceback' not in result.stderr
        return result
    return app, selected, run


def test_list_libraries_and_search_only_explicit_selection_without_desktop(cli):
    _, ids, run = cli
    rows = json.loads(run('--list-libraries').stdout)
    assert {row['id'] for row in rows} == set(ids)
    result = json.loads(run('--search', 'Straße', '--workspace', ids[1], '--workspace', ids[0]).stdout)
    assert [group['id'] for group in result['libraries']] == [ids[1], ids[0]]
    assert [group['results'][0]['author'] for group in result['libraries']] == ['Second author', 'First author']
    assert all(group['results'][0]['verified'] for group in result['libraries'])
    # Short presentation includes the surrounding complete source sentence.
    assert all(group['results'][0]['quote'] == 'Railway history — Straße.'
               for group in result['libraries'])
    assert 'Unselected author' not in json.dumps(result)


@pytest.mark.parametrize('mode', ['phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced'])
def test_existing_modes_and_metadata_filters_apply_in_each_selected_library(cli, mode):
    _, ids, run = cli
    result = json.loads(run('--search', 'history', '--workspace', ids[0], '--workspace', ids[1],
        '--search-mode', mode, '--author', 'First author', '--language', 'uk',
        '--title', 'Synthetic source', '--edition', 'First', '--limit-per-library', '1').stdout)
    assert len(result['libraries'][0]['results']) == 1
    assert result['libraries'][1]['results'] == []
    assert result['limit_per_library'] == 1
    if mode == 'semantic':
        assert any('unavailable' in text.lower() for text in result['notices'])


def test_missing_library_selection_is_an_error_and_does_not_create_data(tmp_path):
    env = cli_environment()
    destination = tmp_path / 'must-not-exist'
    result = subprocess.run(cli_command('-m', 'carr_library', '--data-dir', str(destination),
                             '--search', 'history'), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and '--workspace' in result.stderr
    assert not destination.exists()


def test_missing_last_library_or_corrupted_original_never_prints_partial_results(cli):
    app, ids, run = cli
    result = run('--search', 'history', '--workspace', ids[0], '--workspace', 'missing', expected=1)
    assert not result.stdout.strip()
    source, = app.assets_dir.glob('*.txt')
    source.write_bytes(b'Corrupted synthetic retained source')
    result = run('--search', 'history', '--workspace', ids[0], expected=1)
    assert not result.stdout.strip()


def test_conflicting_operations_and_invalid_result_limit_fail_clearly(cli, tmp_path):
    _, ids, run = cli
    run('--search', 'history', '--workspace', ids[0], '--backup', tmp_path / 'backup', expected=2)
    run('--list-libraries', '--workspace', ids[0], expected=2)
    run('--search', 'history', '--workspace', ids[0], '--limit-per-library', '0', expected=1)


@pytest.mark.parametrize('operation', ['list', 'search'])
def test_corrupt_library_database_fails_clearly_without_partial_json_or_replacing_it(cli, operation):
    app, ids, run = cli
    damaged = b'This is a damaged synthetic library, not a SQLite database.'
    app.database.write_bytes(damaged)
    arguments = ('--list-libraries',) if operation == 'list' else (
        '--search', 'history', '--workspace', ids[0])
    result = run(*arguments, expected=1)
    assert not result.stdout.strip()
    assert result.stderr.startswith('Research Library: ')
    assert 'file is not a database' in result.stderr
    assert app.database.read_bytes() == damaged


def test_advanced_cli_pages_keep_originals_and_selected_libraries(cli):
    app, ids, run = cli
    source = "\f".join(f"Memory and identity, section {index}." for index in range(5))
    imported = app.add_text(ids[0], 'Paged source', source)
    query = ('--search', 'memory NEAR/2 identity', '--search-mode', 'advanced',
             '--workspace', ids[0], '--workspace', ids[1], '--limit-per-library', '2')
    pages = [json.loads(run(*query, '--offset-per-library', str(offset)).stdout)
             for offset in (0, 2, 4)]
    assert [page['has_more'] for page in pages] == [True, True, False]
    assert [page['offset_per_library'] for page in pages] == [0, 2, 4]
    quotes = [hit['quote'] for page in pages for group in page['libraries']
              for hit in group['results']]
    assert quotes == source.split('\f')
    assert all(page['libraries'][1]['results'] == [] for page in pages)
    assert app.assets.resolve(imported['asset']).read_bytes() == source.encode('utf-8')
    invalid = run('--search', 'NOT memory', '--search-mode', 'advanced',
                  '--workspace', ids[0], expected=1)
    assert not invalid.stdout.strip()
    invalid = run('--search', 'memory', '--workspace', ids[0],
                  '--offset-per-library', '1', expected=1)
    assert not invalid.stdout.strip()

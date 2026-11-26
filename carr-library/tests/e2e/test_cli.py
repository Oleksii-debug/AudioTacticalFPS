"""Real CLI processes: persisted text research and precise failure exits."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
RAW = 'Передмова. Straße — дослівний текст.\nAfter.'


@pytest.fixture
def cli(tmp_path):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(ROOT / 'src'), str(ROOT)))
    env['PYTHONUTF8'] = '1'
    data = tmp_path / 'private-library'

    def run(*arguments, code=0, runner=None):
        prefix = [sys.executable, '-m', 'carr_library'] if runner is None else [sys.executable, '-c', runner]
        result = subprocess.run([*prefix, '--data-dir', str(data), *map(str, arguments)],
                                cwd=tmp_path, env=env, capture_output=True,
                                text=True, encoding='utf-8', timeout=30, check=False)
        assert result.returncode == code, (result.returncode, result.stdout, result.stderr)
        assert 'Traceback' not in result.stderr
        return result

    workspace = json.loads(run('create', 'Research', '--description', 'Synthetic text').stdout)['id']
    source = tmp_path / 'original.txt'
    source.write_text(RAW, encoding='utf-8', newline='')
    imported = json.loads(run('import', workspace, source, '--title', 'Synthetic title',
                              '--author', 'Synthetic Author', '--work-id', 'w',
                              '--edition-id', 'e').stdout)
    assert imported['state'] == 'complete' and imported['citation_status'] == 'byte_verified'
    topic = json.loads(run('topic', workspace, 'Evidence').stdout)['topic']
    return run, workspace, topic, source


def test_create_import_search_save_export_and_reopen_check(cli, tmp_path):
    run, workspace, topic, _ = cli
    libraries = json.loads(run('libraries').stdout)
    assert [(row['id'], row['name']) for row in libraries] == [(workspace, 'Research')]
    hits = json.loads(run('search', workspace, 'STRASSE').stdout)
    assert len(hits) == 1 and hits[0]['quote'] == 'Straße'
    assert hits[0]['author'] == 'Synthetic Author'
    assert hits[0]['page'] == 'Segment 1 (unpaginated)'
    assert json.loads(run('save', workspace, topic, 'STRASSE').stdout)['evidence']
    result = run('export', workspace, topic, '--format', 'text').stdout
    quotes = [json.loads(line.split(': ', 1)[1]) for line in result.splitlines()
              if line.startswith('Exact quotation: ')]
    assert quotes == ['Straße']
    assert 'Research note' in result and 'Synthetic Author' in result
    exported = tmp_path / 'evidence.txt'
    assert json.loads(run('export', workspace, topic, '--format', 'text',
                          '--output', exported).stdout)['saved'] == str(exported)
    assert exported.read_text(encoding='utf-8') == result
    checked = json.loads(run('check').stdout)
    assert checked['schema_version'] == 3 and checked['integrity'] == 'ok'
    assert checked['originals_verified'] == 1
    jobs = json.loads(run('jobs', workspace).stdout)
    assert len(jobs) == 1 and jobs[0]['state'] == 'complete'


def test_missing_result_and_existing_output_fail_without_data_changes(cli, tmp_path):
    run, workspace, topic, _ = cli
    for query, number in [('missing term', '1'), ('Straße', '2')]:
        failure = run('save', workspace, topic, query, '--result', number, code=2)
        assert 'does not exist' in failure.stderr
    assert 'Exact quotation:' not in run('export', workspace, topic, '--format', 'text').stdout
    target = tmp_path / 'keep.txt'
    target.write_bytes(b'Existing file must remain exact.\r\n')
    run('export', workspace, topic, '--output', target, code=2)
    assert target.read_bytes() == b'Existing file must remain exact.\r\n'
    assert json.loads(run('check').stdout)['integrity'] == 'ok'


def test_duplicate_catalog_identity_has_concise_failure_and_keeps_first_source(cli):
    run, workspace, topic, source = cli
    source.write_text('Other original text', encoding='utf-8')
    failure = run('import', workspace, source, '--title', 'Other', '--work-id', 'w',
                  '--edition-id', 'e', code=2)
    assert 'identity conflict' in failure.stderr
    hits = json.loads(run('search', workspace, 'Straße').stdout)
    assert hits[0]['title'] == 'Synthetic title'
    assert json.loads(run('search', workspace, 'Other').stdout) == []
    assert json.loads(run('check').stdout)['integrity'] == 'ok'


def test_corrupt_database_has_concise_failure(cli, tmp_path):
    run, _, _, _ = cli
    broken = tmp_path / 'broken-library'
    broken.mkdir()
    original = b'Not a SQLite database; synthetic corruption.'
    (broken / 'library.sqlite').write_bytes(original)
    failure = run('--data-dir', broken, 'check', code=2)
    assert 'database rejected' in failure.stderr
    assert (broken / 'library.sqlite').read_bytes() == original


def test_missing_optional_pdf_dependency_has_install_guidance(cli, tmp_path):
    run, workspace, _, _ = cli
    source = tmp_path / 'synthetic.pdf'
    source.write_bytes(b'%PDF-1.4\n%%EOF\n')
    # Force the genuine optional import boundary to behave like a core-only
    # installation, even when this test environment has the PDF extra present.
    runner = '''
import importlib.abc
import sys
class WithoutPDF(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pypdf' or fullname.startswith('pypdf.'):
            raise ModuleNotFoundError("No module named 'pypdf'", name='pypdf')
sys.meta_path.insert(0, WithoutPDF())
from carr_library.__main__ import main
raise SystemExit(main(sys.argv[1:]))
'''
    failure = run('import', workspace, source, code=2, runner=runner)
    assert 'carr-research-library[pdf]' in failure.stderr
    assert json.loads(run('search', workspace, 'Straße').stdout)

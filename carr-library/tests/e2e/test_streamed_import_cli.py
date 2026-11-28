"""Real isolated CLI invocations use the currently imported product only."""
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
sys.argv = ['carr_library', 'import-text', *sys.argv[2:]]
runpy.run_module('carr_library', run_name='__main__')
'''


def invoke(root, *arguments, status=0):
    product, extractor = Path(carr_library.__file__).resolve(), Path(hasher.__file__).resolve()
    contract = dict(product=str(product), hasher=str(extractor),
                    roots=list(dict.fromkeys((str(product.parent.parent), str(extractor.parents[3])))))
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', CHILD, json.dumps(contract),
        '--data-dir', str(root), *map(str, arguments)], capture_output=True, text=True, encoding='utf-8',
        timeout=45, env=environment, cwd=root.parent)
    assert result.returncode == status, (result.stdout, result.stderr)
    assert 'Traceback' not in result.stderr
    return result


def test_cli_import_and_reopened_resume_keep_profile_and_original(tmp_path):
    app = LibraryApp(tmp_path / 'library Україна')
    workspace = app.create_workspace('CLI source')
    source = tmp_path / 'Свідчення.txt'
    source.write_bytes('railway exact source\r\n\fДругий фрагмент'.encode())
    result = json.loads(invoke(app.root, '--workspace', workspace, '--file', source,
                               '--max-mib', 70, '--title', 'Original CLI source').stdout)
    assert result['state'] == 'complete' and result['isolated'] and result['verified']
    request = load_request(app.root, result['request_id'])
    assert request['source_profile']['maximum_bytes'] == 70 * 1024**2
    source.unlink()
    repeated = json.loads(invoke(app.root, '--workspace', workspace, '--resume-request', result['request_id']).stdout)
    assert repeated['id'] == result['id'] and repeated['state'] == 'complete'
    assert app.search(workspace, 'railway exact source')[0].verified


@pytest.mark.parametrize('arguments', [
    ('--file', 'large.txt', '--max-mib', '129'),
    ('--file', 'large.txt', '--max-mib', '0'),
    ('--file', 'large.pdf'),
    ('--file', 'large.txt', '--timeout', 'nan'),
    ('--resume-request', 'a' * 32, '--max-mib', '96'),
    ('--resume-request', '../arbitrary-path'),
], ids=['over-bound', 'zero', 'pdf', 'non-finite', 'rebind-resume', 'unsafe-id'])
def test_invalid_cli_choices_do_not_create_a_library_or_read_a_source(tmp_path, arguments):
    root = tmp_path / 'must stay absent'
    invoke(root, '--workspace', 'selected', *arguments, status=2)
    assert not root.exists()


def test_streamed_cli_refuses_a_saved_request_from_another_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    first, other = app.create_workspace('First'), app.create_workspace('Other')
    source = tmp_path / 'book.txt'
    source.write_text('Exact source', encoding='utf-8')
    result = json.loads(invoke(app.root, '--workspace', first, '--file', source).stdout)
    before = (app.root / 'import-requests' / (result['request_id'] + '.json')).read_bytes()
    rejected = invoke(app.root, '--workspace', other, '--resume-request', result['request_id'], status=2)
    assert json.loads(rejected.stdout)['state'] == 'failed'
    assert (app.root / 'import-requests' / (result['request_id'] + '.json')).read_bytes() == before


def test_explicit_budget_file_partial_cli_keeps_immutable_profile_on_resume(tmp_path):
    from carr_library.ingest.resource_budget import ImportBudget, MIB
    app = LibraryApp(tmp_path / 'profiled-library')
    workspace = app.create_workspace('Explicit resource file')
    source = tmp_path / 'fragments.txt'
    source.write_bytes('First exact source.\fSecond exact source.\fLast exact source.'.encode('utf-8'))
    policy = ImportBudget(source_bytes=2*MIB, segment_bytes=MIB, segments=1000,
        steps=2, seconds=120, memory_bytes=768*MIB, library_bytes=64*MIB,
        temporary_bytes=128*MIB)
    budget_file = tmp_path / 'reviewed-budget.json'
    budget_file.write_text(json.dumps(policy.to_dict()), encoding='utf-8')
    first = json.loads(invoke(app.root, '--workspace', workspace, '--file', source,
        '--budget-file', budget_file, status=2).stdout)
    assert first['state'] == 'pending' and first['completed'] == 2
    request = load_request(app.root, first['request_id'])
    assert request['source_profile']['import_budget'] == policy.to_dict()
    source.unlink()
    budget_file.write_text('{}', encoding='utf-8')
    final = json.loads(invoke(app.root, '--workspace', workspace,
        '--resume-request', first['request_id']).stdout)
    assert final['state'] == 'complete' and final['completed'] == 3
    assert final['id'] == first['id']
    assert load_request(app.root, first['request_id'])['source_profile'] == request['source_profile']
    hit, = app.search(workspace, 'Last exact source')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Last exact source'


@pytest.mark.parametrize('kind', ['oversized', 'duplicate', 'unknown', 'non-finite'])
def test_budget_file_is_bounded_strict_json_before_any_source_request(tmp_path, kind):
    from carr_library.ingest.resource_budget import ImportBudget
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Refused policy')
    source = tmp_path / 'source.txt'
    source.write_text('Original source must stay unchanged.', encoding='utf-8')
    policy = ImportBudget().to_dict()
    if kind == 'oversized':
        raw = b' ' * 64_001
    elif kind == 'duplicate':
        raw = (json.dumps(policy)[:-1] + ', "steps": 2}').encode('utf-8')
    elif kind == 'unknown':
        policy['unreviewed'] = True
        raw = json.dumps(policy).encode('utf-8')
    else:
        policy['seconds'] = float('inf')
        raw = json.dumps(policy).encode('utf-8')
    budget_file = tmp_path / 'refused.json'
    budget_file.write_bytes(raw)
    original = source.read_bytes()
    result = invoke(app.root, '--workspace', workspace, '--file', source,
                    '--budget-file', budget_file, status=2)
    assert json.loads(result.stdout)['state'] == 'failed'
    assert source.read_bytes() == original
    assert not list(app.root.glob('import-requests/*.json'))
    with app.read(workspace) as catalog:
        assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0

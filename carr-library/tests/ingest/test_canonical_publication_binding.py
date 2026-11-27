"""Actual workers cannot substitute a launch identity or its commit attempt."""
from pathlib import Path
import runpy

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


source_file = runpy.run_path(str(Path(__file__).with_name(
    'test_worker_publication_boundary.py')))['source_file']


@pytest.mark.parametrize('change', ('source', 'configuration', 'metadata', 'attempt'))
def test_actual_child_rejects_sidecar_change_after_parent_fingerprint(tmp_path, monkeypatch, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Frozen native launch')
    source = source_file(tmp_path, 'docx')
    original = source.read_bytes()
    invoke, replies = worker._invoke, []

    def changed_before_launch(message, **options):
        record = worker.load_request(app.root, message['request_id'])
        if change == 'source':
            record['source'] = str(tmp_path / 'another-source.docx')
        elif change == 'configuration':
            record['config']['dpi'] += 1
        elif change == 'metadata':
            record['metadata']['language'] = 'uk'
        else:
            record['attempt'] = 'e' * 32 if record['attempt'] != 'e' * 32 else 'f' * 32
        worker._store(app.root, message['request_id'], record)
        result = invoke(message, **options)
        replies.append(result)
        return result

    monkeypatch.setattr(worker, '_invoke', changed_before_launch)
    result = app.import_file(workspace, source)
    assert len(replies) == 1 and replies[0]['state'] == 'failed'
    assert 'changed before this worker attempt' in replies[0]['message']
    assert result['state'] == 'failed' and not result.get('publication_recovered')
    assert source.read_bytes() == original
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0


@pytest.mark.parametrize('change', ('attempt', 'source'))
def test_actual_publication_uses_frozen_attempt_instead_of_mutable_sidecar(tmp_path, monkeypatch, change):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Frozen commit ownership')
    source = source_file(tmp_path, 'docx')
    original = source.read_bytes()
    marker = tmp_path / 'before-receipt'
    from carr_library.audio import capture_process
    popen, children = capture_process.start_owned_process, []

    def launch(command, *args, **kwargs):
        if isinstance(command, list) and '-c' in command:
            index = command.index('-c') + 1
            anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"
            if anchor in command[index]:
                body = '\n'.join([
                    'def changed(root, request_id, db, workspace, revision, **options):',
                    '    record = product.load_request(root, request_id)',
                    ("    record['attempt'] = 'f' * 32 if record['attempt'] != 'f' * 32 else 'e' * 32"
                     if change == 'attempt' else
                     "    record['source'] = record['source'] + '.changed'"),
                    '    product._store(root, request_id, record)',
                    '    Path(' + repr(str(marker)) + ").write_text('reached publication', encoding='utf-8')",
                    '    return original(root, request_id, db, workspace, revision, **options)',
                ])
                hook = ('from pathlib import Path; from carr_library.ingest import worker as product; '
                        'original=product.record_publication_candidate; exec(' + repr(body) + '); '
                        'product.record_publication_candidate=changed; ')
                command = list(command)
                command[index] = command[index].replace(anchor, hook + anchor)
                process = popen(command, *args, **kwargs)
                children.append(process)
                return process
        return popen(command, *args, **kwargs)

    monkeypatch.setattr(capture_process, 'start_owned_process', launch)
    result = app.import_file(workspace, source)
    assert marker.exists() and len(children) == 1 and children[0].poll() is not None
    assert result['state'] == 'failed' and not result.get('publication_recovered')
    assert source.read_bytes() == original
    with app.read(workspace) as db:
        for table in ('revision', 'sealed', 'page', 'catalog_edition', 'import_publication'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0

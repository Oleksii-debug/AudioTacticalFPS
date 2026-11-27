"""Assistant inspection is bounded and cannot reuse unavailable permission paths."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.state import atomic_json
from carr_library.assistant import state as assistant_state
from carr_library.audio import jobs as audio_jobs
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer


def test_corrupt_and_disconnected_jobs_do_not_hide_other_jobs(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs = tmp_path / 'external-inputs'
    inputs.mkdir()
    service = AssistantService(app)
    disconnected = service.submit(service.preview('List libraries', Scope(input_roots=(inputs,))))
    corrupt = service.submit(service.preview('List libraries', Scope()))
    healthy = service.submit(service.preview('List libraries', Scope()))
    service.store.path(corrupt).write_bytes(b'{ broken JSON')
    inputs.rmdir()
    reports = {row.id: row for row in service.jobs()}
    assert reports[disconnected].status == reports[corrupt].status == 'attention'
    assert reports[healthy].status == 'queued'
    assert reports[corrupt].total == 0 and 'damaged' in reports[corrupt].message
    assert service.run(healthy).status == 'complete'
    with pytest.raises(FileNotFoundError):
        service.resume(disconnected)
    assert not service.store.path(disconnected).with_suffix('.control.json').exists()


def test_publication_rejects_duplicate_json_in_live_reads(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    raw = service.store.path(identity).read_text(encoding='utf-8').rstrip()
    service.store.path(identity).write_text(raw[:-1] + ',"status":"complete"}', encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        service.resume(identity)
    assert service.jobs()[0].status == 'attention'


def test_reapproval_draft_preserves_step_arguments_without_transferring_permissions(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    source = inputs / 'source.txt'
    source.write_text('Exact source', encoding='utf-8')
    workspace = app.create_workspace('Reviewed source scope')
    service = AssistantService(app)
    identity = service.submit(service.preview('Import "' + str(source) + '"', Scope(workspace, (inputs,))))
    saved = service.store.load(identity)
    saved['requires_reapproval'] = True
    service.store.save(saved)
    before = service.store.path(identity).read_bytes()
    draft = json.loads(service.review_instruction(identity))
    assert 'scope' not in draft
    assert draft['steps'] == saved['plan']['steps']
    with pytest.raises(PermissionError):
        service.preview(json.dumps(draft), Scope(workspace))
    with pytest.raises(PermissionError, match='review and submit'):
        service.run(identity)
    assert service.store.path(identity).read_bytes() == before


def test_job_and_control_publication_fail_without_partial_changes_while_backup_owns_root(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    value = service.store.load(identity)
    previous = deepcopy(value)
    raw = service.store.path(identity).read_bytes()
    with exclusive_writer(app.root):
        with pytest.raises(WriterBusy):
            service.store.save(value)
        with pytest.raises(WriterBusy):
            service.store.control(identity, 'pause')
    assert value == previous and service.store.path(identity).read_bytes() == raw
    assert not service.store.path(identity).with_suffix('.control.json').exists()
    service.store.control(identity, 'pause')
    assert service.store.control(identity) == 'pause'


def test_unmanaged_state_directory_is_rejected_before_creation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    other = app.root / 'unmanaged'
    with pytest.raises(ValueError, match='managed'):
        AssistantService(app, state_dir=other)
    assert not other.exists()


def test_job_inventory_stops_at_its_explicit_budget(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    for _ in range(2):
        service.submit(service.preview('List libraries', Scope()))
    monkeypatch.setattr('carr_library.assistant.state.MAX_JOBS', 1)
    with pytest.raises(ValueError, match='Too many'):
        service.jobs()


_CHECKPOINT_WRITER = r'''
import json, os, pathlib, sys
library = pathlib.Path(sys.argv[1])
identity, kind = sys.argv[2:4]
package_root = pathlib.Path(sys.argv[4])
sys.path.insert(0, str(package_root))
from carr_library.assistant import state
assert pathlib.Path(state.__file__).resolve().is_relative_to(package_root / 'carr_library')
try:
    store = state.JobStore(library / 'state' / 'assistant', writer_root=library)
    if kind == 'job':
        value = store.load(identity)
        value['message'] = 'Published by an independent process.'
        store.save(value)
    else:
        store.control(identity, 'cancel')
except OSError as error:
    print(json.dumps({'published': False, 'winerror': getattr(error, 'winerror', None)}))
    raise SystemExit(2)
else:
    print(json.dumps({'published': True}))
'''


def _publish_checkpoint_in_child(app, identity, kind='job'):
    package_root = Path(assistant_state.__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, '-I', '-B', '-X', 'utf8', '-c', _CHECKPOINT_WRITER,
         str(app.root), identity, kind, str(package_root)],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode in (0, 2), result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('kind', ['job', 'control'])
def test_live_checkpoint_snapshot_allows_actual_independent_publisher(tmp_path, monkeypatch, kind):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    old = service.store.load(identity)
    service.store.control(identity, 'pause')
    target = service.store.path(identity)
    if kind == 'control':
        target = target.with_suffix('.control.json')

    # Actual negative control: the ordinary CRT reader caused the observed
    # Windows schedule failure. No native syscall or writer lock is mocked.
    with target.open('rb'):
        ordinary = _publish_checkpoint_in_child(app, identity, kind)
    if os.name == 'nt':
        assert ordinary['published'] is False
        assert ordinary['winerror'] in (5, 32, 33)
    else:
        assert ordinary == {'published': True}
    service.store.save(old)
    service.store.control(identity, 'pause')

    original, observed, streams = audio_jobs._open_json_file, [], []
    def publish_while_open(path):
        stream = original(path)
        if path != target or observed:
            return stream
        streams.append(stream)
        try:
            assert not os.get_inheritable(stream.fileno())
            observed.append(_publish_checkpoint_in_child(app, identity, kind))
            assert observed[-1] == {'published': True}
        except BaseException:
            stream.close()
            raise
        return stream

    monkeypatch.setattr(audio_jobs, '_open_json_file', publish_while_open)
    if kind == 'job':
        assert service.store.load(identity)['message'] == old['message']
    else:
        assert service.store.control(identity) == 'pause'
    assert observed == [{'published': True}]
    assert streams and all(stream.closed for stream in streams)
    monkeypatch.setattr(audio_jobs, '_open_json_file', original)
    if kind == 'job':
        assert service.store.load(identity)['message'] == 'Published by an independent process.'
    else:
        assert service.store.control(identity) == 'cancel'
    assert not tuple(service.store.directory.glob('.*.tmp'))


def test_strict_assistant_snapshot_rejects_replacement_without_blocking_writer(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    target = service.store.path(identity)
    original, observed, streams = assistant_state._open_json_file, [], []
    def publish_while_open(path):
        stream = original(path)
        streams.append(stream)
        try:
            observed.append(_publish_checkpoint_in_child(app, identity))
            assert observed[-1] == {'published': True}
        except BaseException:
            stream.close()
            raise
        return stream

    monkeypatch.setattr(assistant_state, '_open_json_file', publish_while_open)
    with pytest.raises(ValueError, match='changed'):
        assistant_state.read_bytes(target)
    assert observed == [{'published': True}]
    assert all(stream.closed for stream in streams)
    assert service.store.load(identity)['message'] == 'Published by an independent process.'


def test_immutable_payload_reader_keeps_its_existing_native_protection(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    target = service.store.path(identity)
    before = target.read_bytes()
    if os.name == 'nt':
        with audio_jobs.verified_reader(target) as stream:
            assert stream.read() == before
            outcome = _publish_checkpoint_in_child(app, identity)
            assert outcome['published'] is False
            assert outcome['winerror'] in (5, 32, 33)
        assert target.read_bytes() == before
    else:
        # POSIX permits renaming an open immutable payload. Its existing exit
        # check must still detect that change instead of accepting a snapshot.
        with pytest.raises(RuntimeError, match='changed'):
            with audio_jobs.verified_reader(target) as stream:
                assert stream.read() == before
                assert _publish_checkpoint_in_child(app, identity) == {'published': True}


@pytest.mark.parametrize('raw', [
    b'{"v":1,"v":2}', b'{"v":NaN}', b'{"v":1e9999}', b'{invalid'
], ids=['duplicate', 'nonfinite-literal', 'overflow-number', 'malformed'])
def test_live_assistant_snapshot_preserves_strict_json_decoder(tmp_path, raw):
    path = tmp_path / 'state.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        assistant_state.read_json(path)


@pytest.mark.parametrize('grow', [False, True])
def test_live_assistant_snapshot_checks_byte_budget_before_and_after_open(tmp_path, monkeypatch, grow):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{}' if grow else b'{"text":"over budget"}')
    original, streams = audio_jobs._open_json_file, []
    def capture(selected):
        if grow:
            selected.write_bytes(b'{"text":"over budget"}')
        stream = original(selected)
        streams.append(stream)
        return stream
    monkeypatch.setattr(audio_jobs, '_open_json_file', capture)
    with pytest.raises(ValueError, match='too large'):
        assistant_state.read_json(path, maximum=8)
    assert all(stream.closed for stream in streams)


def test_assistant_publication_failure_preserves_previous_and_removes_own_stage(tmp_path, monkeypatch):
    target, unrelated = tmp_path / 'state.json', tmp_path / 'unrelated.json'
    target.write_bytes(b'{"old":1}')
    unrelated.write_bytes(b'{"keep":1}')
    def fail(source, destination):
        assert Path(destination) == target
        assert json.loads(Path(source).read_bytes()) == {'new': 1}
        raise OSError('synthetic atomic state publication failure')
    monkeypatch.setattr(assistant_state, 'replace_json_file', fail)
    with pytest.raises(OSError, match='publication failure'):
        assistant_state.atomic_json(target, {'new': 1})
    assert target.read_bytes() == b'{"old":1}'
    assert unrelated.read_bytes() == b'{"keep":1}'
    assert set(tmp_path.iterdir()) == {target, unrelated}

"""Fresh, bounded control snapshots; no model, worker or process launch is needed."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

import pytest

from carr_library import durable_state
from carr_library.transcription import service as module


IDENTITY = 'a' * 32


@pytest.fixture
def control(tmp_path):
    service = module.TranscriptionService(tmp_path / 'library' / 'state' / 'transcription')
    job_root = service.jobs_root / IDENTITY
    job_root.mkdir()
    path = job_root / 'control.json'
    durable_state.atomic_json(path, {'action': 'run'})
    return service, path


def test_every_control_read_observes_fresh_atomic_replacement(control):
    service, path = control
    for action in ('run', 'pause', 'cancel', 'run'):
        durable_state.atomic_json(path, {'action': action}, replace=True)
        assert service._control(IDENTITY) == action
        assert durable_state.read_json(path) == {'action': action}


def test_atomic_replacement_between_stat_and_open_keeps_coherent_reader(control, monkeypatch):
    service, path = control
    original_open = durable_state._open_json_file
    opened = []

    def replace_once(selected):
        assert Path(selected) == path
        if not opened:
            durable_state.atomic_json(path, {'action': 'cancel'}, replace=True)
        stream = original_open(selected)
        opened.append(stream)
        return stream

    monkeypatch.setattr(durable_state, '_open_json_file', replace_once)
    assert service._control(IDENTITY) == 'cancel'
    assert len(opened) == 2
    assert all(stream.closed for stream in opened)


@pytest.mark.parametrize('identity', [
    None, False, 7, '', 'a' * 31, 'A' * 32, '../' + 'a' * 32, 'a' * 32 + '/control.json',
], ids=['none', 'boolean', 'number', 'empty', 'short', 'uppercase', 'traversal', 'suffix'])
def test_invalid_identity_cannot_enter_the_snapshot_reader(control, monkeypatch, identity):
    service, path = control
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('Invalid identity reached state-byte admission.')

    monkeypatch.setattr(module, 'read_json_snapshot', forbidden)
    with pytest.raises(ValueError, match='identity'):
        service._control(identity)
    assert calls == []
    assert durable_state.read_json(path) == {'action': 'run'}


@pytest.mark.parametrize('location', ['outside', 'dotdot-outside', 'dotdot-inside'])
def test_lexical_scope_rejects_escape_before_any_state_byte_read(control, tmp_path, monkeypatch, location):
    service, path = control
    outside = tmp_path / 'outside'
    (outside / IDENTITY).mkdir(parents=True)
    (outside / IDENTITY / 'control.json').write_bytes(b'{"action":"cancel"}')
    service.jobs_root = {
        'outside': outside,
        'dotdot-outside': service.root / '..' / '..' / '..' / 'outside',
        'dotdot-inside': service.root / 'jobs' / '..' / 'jobs',
    }[location]
    reads = []

    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError('Out-of-scope control reached the byte reader.')

    monkeypatch.setattr(module, 'read_json_snapshot', forbidden)
    with pytest.raises(ValueError):
        service._control(IDENTITY)
    assert reads == []
    assert path.read_bytes() == b'{"action":"run"}\n'
    assert (outside / IDENTITY / 'control.json').read_bytes() == b'{"action":"cancel"}'


def test_missing_control_defaults_only_after_fresh_job_root_guard(control, monkeypatch):
    service, path = control
    path.unlink()
    assert service._control(IDENTITY) == 'run'
    failure = ValueError('Current job root was revoked.')

    def revoked(identity):
        assert identity == IDENTITY
        raise failure

    monkeypatch.setattr(service, '_job_root', revoked)
    with pytest.raises(ValueError) as caught:
        service._control(IDENTITY)
    assert caught.value is failure


def test_missing_control_does_not_hide_a_removed_service_root(control):
    service, path = control
    retained = service.root.with_name('retained-transcription')
    service.root.rename(retained)
    with pytest.raises(FileNotFoundError):
        service._control(IDENTITY)
    assert (retained / 'jobs' / IDENTITY / 'control.json').is_file()
    assert not service.root.exists()


@pytest.mark.parametrize('moment', ['before-open', 'after-open'])
def test_disappearance_after_positive_admission_is_not_missing_default(control, monkeypatch, moment):
    service, path = control
    original_open = durable_state._open_json_file
    opened = []

    @contextmanager
    def disappear(selected):
        assert Path(selected) == path
        if moment == 'before-open':
            path.unlink()
        with original_open(selected) as stream:
            opened.append(stream)
            if moment == 'after-open':
                path.unlink()
            yield stream

    def no_missing_fallback(identity):
        raise AssertionError('A read error became the missing-control default.')

    monkeypatch.setattr(durable_state, '_open_json_file', disappear)
    monkeypatch.setattr(service, '_job_root', no_missing_fallback)
    # Windows DeleteFileW can leave the name delete-pending until this shared
    # handle closes; the strict post-open path check may report access denied.
    expected = ((FileNotFoundError, PermissionError)
                if os.name == 'nt' and moment == 'after-open' else FileNotFoundError)
    with pytest.raises(expected) as caught:
        service._control(IDENTITY)
    if isinstance(caught.value, PermissionError):
        assert os.name == 'nt' and moment == 'after-open'
        assert caught.value.winerror == 5
    assert not path.exists()
    assert all(stream.closed for stream in opened)
    assert len(opened) == (0 if moment == 'before-open' else 1)


@pytest.mark.parametrize('error', [PermissionError('Denied current control.'), OSError('Control I/O failed.')],
                         ids=['permission', 'io'])
def test_control_io_errors_are_never_defaulted(control, monkeypatch, error):
    service, path = control

    def denied(selected):
        assert Path(selected) == path
        raise error

    monkeypatch.setattr(durable_state, '_open_json_file', denied)
    with pytest.raises(type(error)) as caught:
        service._control(IDENTITY)
    assert caught.value is error
    assert path.read_bytes() == b'{"action":"run"}\n'


@pytest.mark.parametrize('raw', [
    b'{"action":"run","action":"cancel"}', b'{', b'{"action":"unknown"}',
    b'{"action":NaN}', b'\xff',
], ids=['duplicate', 'malformed', 'invalid-action', 'non-finite', 'invalid-utf8'])
def test_existing_control_json_and_action_guards_remain(control, raw):
    service, path = control
    durable_state.atomic_bytes(path, raw, replace=True)
    with pytest.raises(ValueError):
        service._control(IDENTITY)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('kind', ['directory', 'hardlink'])
def test_nonregular_control_is_rejected_before_open(control, tmp_path, monkeypatch, kind):
    service, path = control
    if kind == 'directory':
        path.unlink()
        path.mkdir()
    else:
        os.link(path, tmp_path / 'linked-control.json')
        assert path.stat().st_nlink == 2

    def forbidden(selected):
        raise AssertionError('Nonregular state reached an open handle.')

    monkeypatch.setattr(durable_state, '_open_json_file', forbidden)
    with pytest.raises(ValueError, match='regular local'):
        service._control(IDENTITY)


@pytest.mark.parametrize('kind', ['leaf-symlink', 'leaf-reparse', 'parent-reparse', 'missing-parent-reparse'])
def test_link_metadata_is_rejected_by_real_reader_guards(control, monkeypatch, kind):
    service, path = control
    target = path.parent if kind in ('parent-reparse', 'missing-parent-reparse') else path
    if kind == 'missing-parent-reparse':
        path.unlink()
    original_lstat = Path.lstat

    class MarkedStat:
        def __init__(self, value):
            self.value = value

        def __getattr__(self, name):
            return getattr(self.value, name)

        @property
        def st_mode(self):
            return ((stat.S_IFLNK | stat.S_IMODE(self.value.st_mode))
                    if kind == 'leaf-symlink' else self.value.st_mode)

        @property
        def st_file_attributes(self):
            return getattr(self.value, 'st_file_attributes', 0) | (0x400 if 'reparse' in kind else 0)

    def marked_lstat(selected, *args, **kwargs):
        value = original_lstat(selected, *args, **kwargs)
        return MarkedStat(value) if Path(selected) == target else value

    def forbidden(selected):
        raise AssertionError('Linked state reached an open handle.')

    # Native metadata is modeled without requiring symlink privileges. The
    # production ancestor, regular-file and reader checks execute unchanged.
    monkeypatch.setattr(Path, 'lstat', marked_lstat)
    monkeypatch.setattr(durable_state, '_open_json_file', forbidden)
    with pytest.raises(ValueError, match='regular local'):
        service._control(IDENTITY)


def test_json_null_control_is_not_the_missing_default(control):
    service, path = control
    durable_state.atomic_bytes(path, b'null', replace=True)
    with pytest.raises(AttributeError):
        service._control(IDENTITY)
    assert path.read_bytes() == b'null'


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_control_appearing_during_fresh_root_guard_wins_over_missing_default(control, monkeypatch, action):
    service, path = control
    path.unlink()
    original_job_root = service._job_root
    guarded = []

    def publish_after_guard(identity):
        root = original_job_root(identity)
        guarded.append(identity)
        durable_state.atomic_json(path, {'action': action})
        return root

    monkeypatch.setattr(service, '_job_root', publish_after_guard)
    assert service._control(IDENTITY) == action
    assert guarded == [IDENTITY]
    assert durable_state.read_json(path) == {'action': action}

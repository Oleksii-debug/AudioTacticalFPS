"""Real filesystem publication boundaries for durable bytes and citation DOCX.

Platform branches use their actual filesystem primitives. A POSIX process exit
after linking still leaves its second name; this suite records that limitation
and never presents the Windows rename contract as a Linux execution result.
"""
from hashlib import sha256
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library import durable_state


PAYLOAD = 'Synthetic persistent state — Україна\r\n'.encode('utf-8') + b'\x00\xff'


def test_create_only_recovers_a_transient_post_publication_cleanup_failure(tmp_path, monkeypatch):
    target = tmp_path / 'state.bin'
    unlink = os.unlink
    failures = []

    def locked_once(path, *args, **kwargs):
        if Path(path).name.startswith('.pending-') and not failures:
            assert target.read_bytes() == PAYLOAD
            failures.append(path)
            raise PermissionError('Synthetic transient staging lock')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', locked_once)
    assert durable_state.atomic_bytes(target, PAYLOAD) == target
    assert target.read_bytes() == PAYLOAD and target.stat().st_nlink == 1
    assert not list(tmp_path.glob('.pending-*'))
    assert len(failures) == (0 if os.name == 'nt' else 1)


def test_permanent_post_publication_cleanup_failure_reports_the_published_file(tmp_path, monkeypatch):
    target = tmp_path / 'state.bin'
    unlink = os.unlink
    attempts = []

    def locked_stage(path, *args, **kwargs):
        if Path(path).name.startswith('.pending-'):
            assert target.read_bytes() == PAYLOAD
            attempts.append(path)
            raise PermissionError('Synthetic permanent staging lock')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', locked_stage)
    if os.name == 'nt':
        assert durable_state.atomic_bytes(target, PAYLOAD) == target
        assert not attempts and not list(tmp_path.glob('.pending-*'))
    else:
        with pytest.raises(OSError, match='File was published, but its temporary link could not be removed'):
            durable_state.atomic_bytes(target, PAYLOAD)
        assert len(attempts) == 2
        stage, = tmp_path.glob('.pending-*')
        assert os.path.samefile(stage, target)
        # Remove only the known link created by this test, using the real unlink.
        unlink(stage)
    assert target.read_bytes() == PAYLOAD and target.stat().st_nlink == 1


def test_competing_file_at_the_actual_create_only_primitive_is_preserved(tmp_path, monkeypatch):
    target = tmp_path / 'state.bin'
    primitive = 'rename' if os.name == 'nt' else 'link'
    original = getattr(os, primitive)
    calls = []

    def competing_create(*args, **kwargs):
        calls.append(args)
        target.write_bytes(b'Another writer won publication')
        return original(*args, **kwargs)

    monkeypatch.setattr(os, primitive, competing_create)
    with pytest.raises(FileExistsError):
        durable_state.atomic_bytes(target, PAYLOAD)
    assert len(calls) == 1
    assert target.read_bytes() == b'Another writer won publication'
    assert target.stat().st_nlink == 1 and not list(tmp_path.glob('.pending-*'))


def test_create_only_primitive_failure_cleans_stage_without_publishing(tmp_path, monkeypatch):
    target = tmp_path / 'state.bin'
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise OSError('Synthetic unsupported no-replacement operation')

    monkeypatch.setattr(os, 'rename' if os.name == 'nt' else 'link', unavailable)
    with pytest.raises(OSError, match='unsupported no-replacement'):
        durable_state.atomic_bytes(target, PAYLOAD)
    assert len(calls) == 1 and not target.exists()
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('fail', [False, True])
def test_replace_true_keeps_the_existing_atomic_replacement_contract(tmp_path, monkeypatch, fail):
    target = tmp_path / 'state.bin'
    target.write_bytes(b'Previous state')
    original = os.replace
    calls = []

    def replacement(*args, **kwargs):
        calls.append(args)
        if fail:
            raise PermissionError('Synthetic replacement refusal')
        return original(*args, **kwargs)

    monkeypatch.setattr(os, 'replace', replacement)
    if fail:
        with pytest.raises(PermissionError, match='replacement refusal'):
            durable_state.atomic_bytes(target, PAYLOAD, replace=True)
        assert target.read_bytes() == b'Previous state'
    else:
        assert durable_state.atomic_bytes(target, PAYLOAD, replace=True) == target
        assert target.read_bytes() == PAYLOAD
    assert len(calls) == 1
    assert target.stat().st_nlink == 1 and not list(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('kind', ['bytes', 'citation-docx'])
def test_real_process_exit_immediately_after_create_only_publication(tmp_path, kind):
    target = tmp_path / ('citation.docx' if kind == 'citation-docx' else 'state.bin')
    script = r'''
from hashlib import sha256
import os
from pathlib import Path
import sys
from carr_library import durable_state

target = Path(sys.argv[1])
assert sha256(Path(durable_state.__file__).read_bytes()).hexdigest() == sys.argv[3]
if sys.argv[2] == 'citation-docx':
    from carr_library.academic_integration import CitationRequest, export_citation_docx, render_citations
    result = render_citations([{'id': 'durable-reference', 'type': 'book',
        'title': 'Synthetic durable citation', 'author': [{'literal': 'Research Group'}],
        'issued': {'date-parts': [[2026]]}}], citations=[CitationRequest('durable-reference')])
    publish = lambda: export_citation_docx(result, target)
else:
    publish = lambda: durable_state.atomic_bytes(target, bytes.fromhex(sys.argv[4]))

primitive = 'rename' if os.name == 'nt' else 'link'
original = getattr(os, primitive)
def exit_after_publication(source, destination, *args, **kwargs):
    assert Path(destination) == target
    original(source, destination, *args, **kwargs)
    os._exit(74)
setattr(os, primitive, exit_after_publication)
publish()
'''
    environment = os.environ.copy()
    # Both source and installed tests must execute the exact parent package,
    # never a different editable checkout inherited by the child interpreter.
    environment['PYTHONPATH'] = str(Path(durable_state.__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, '-B', '-c', script, str(target), kind,
        sha256(Path(durable_state.__file__).read_bytes()).hexdigest(), PAYLOAD.hex()],
        capture_output=True, check=False, timeout=30, env=environment)
    assert result.returncode == 74, result.stderr.decode('utf-8', errors='replace')
    assert target.exists()
    stages = list(tmp_path.glob('.pending-*'))
    if os.name == 'nt':
        assert stages == [] and target.stat().st_nlink == 1
        durable_state.regular(target)
    else:
        stage, = stages
        assert target.stat().st_nlink == 2 and os.path.samefile(stage, target)
        with pytest.raises(ValueError, match='without links'):
            durable_state.regular(target)
        with pytest.raises(ValueError, match='without links'):
            durable_state.atomic_bytes(target, b'Must not replace a linked file', replace=True)
        stage.unlink()
    if kind == 'citation-docx':
        from carr_library.academic_integration import read_citation_docx

        snapshot = read_citation_docx(target)
        assert snapshot['references'][0]['id'] == 'durable-reference'
        assert snapshot['references'][0]['title'] == 'Synthetic durable citation'
    else:
        assert target.read_bytes() == PAYLOAD
    # Windows needs no residual-link cleanup before later replacement. POSIX
    # reaches this assertion only after the explicit, known-link cleanup above.
    published = target.read_bytes()
    assert durable_state.atomic_bytes(target, published, replace=True) == target
    assert target.read_bytes() == published and target.stat().st_nlink == 1
    assert not list(tmp_path.glob('.pending-*'))


def test_json_reader_keeps_generation_and_obeys_native_replacement_lifetime(tmp_path, monkeypatch):
    target = tmp_path / 'Стан завдання.json'
    before, after = {'state': 'running', 'done': 0}, {'state': 'completed', 'done': 1}
    durable_state.atomic_json(target, before)
    original = durable_state._open_json_file
    replaced = []

    @contextmanager
    def replace_while_open(path):
        with original(path) as stream:
            assert not os.get_inheritable(stream.fileno())
            if os.name == 'nt':
                with pytest.raises(PermissionError) as error:
                    durable_state.atomic_json(target, after, replace=True)
                assert error.value.winerror in (5, 32, 33)
                replaced.append(False)
            else:
                durable_state.atomic_json(target, after, replace=True)
                replaced.append(True)
            yield stream

    with monkeypatch.context() as scoped:
        scoped.setattr(durable_state, '_open_json_file', replace_while_open)
        assert durable_state.read_json(target) == before
    assert replaced == [os.name != 'nt']
    assert not list(tmp_path.glob('.pending-*'))
    durable_state.atomic_json(target, after, replace=True)
    assert durable_state.read_json(target) == after
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('raw', [
    b'{"state":"running","state":"completed"}',
    b'{"done":NaN}', b'{"done":Infinity}', b'{"state":', b'\xff',
], ids=['duplicate-key', 'nan', 'infinite', 'truncated', 'invalid-utf8'])
def test_json_reader_keeps_strict_parsing_and_closes_after_failure(tmp_path, raw):
    target = tmp_path / 'status.json'
    target.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        durable_state.read_json(target)
    durable_state.atomic_json(target, {'state': 'recovered'}, replace=True)
    assert durable_state.read_json(target) == {'state': 'recovered'}


@pytest.mark.parametrize('grow_after_check', [False, True])
def test_json_reader_preserves_initial_and_concurrent_size_bounds(tmp_path, monkeypatch, grow_after_check):
    target = tmp_path / 'status.json'
    target.write_bytes(b'{}')
    monkeypatch.setattr(durable_state, 'MAX_JSON_BYTES', 64)
    oversized = b'{"message":"' + b'x' * 64 + b'"}'
    if grow_after_check:
        original = durable_state._open_json_file
        def growing(path):
            target.write_bytes(oversized)
            return original(path)
        monkeypatch.setattr(durable_state, '_open_json_file', growing)
    else:
        target.write_bytes(oversized)
    with pytest.raises(ValueError, match='JSON budget'):
        durable_state.read_json(target)


def test_json_reader_rejects_a_real_hard_link_before_opening(tmp_path):
    original, linked = tmp_path / 'original.json', tmp_path / 'linked.json'
    original.write_bytes(b'{"state":"running"}')
    os.link(original, linked)
    with pytest.raises(ValueError, match='without links'):
        durable_state.read_json(linked)
    assert original.read_bytes() == linked.read_bytes() == b'{"state":"running"}'


def test_atomic_replacement_finishes_after_a_real_snapshot_reader_closes(tmp_path, monkeypatch):
    target = tmp_path / 'стан.json'
    before, after = {'state': 'running'}, {'state': 'finished'}
    durable_state.atomic_json(target, before)
    original = target.read_bytes()
    reader = target.open('rb')
    assert reader.read() == original
    native, failures = os.replace, []

    def release_after_native_refusal(*args, **kwargs):
        try:
            return native(*args, **kwargs)
        except PermissionError as error:
            failures.append(error)
            reader.close()
            raise

    monkeypatch.setattr(os, 'replace', release_after_native_refusal)
    try:
        durable_state.atomic_json(target, after, replace=True)
    finally:
        reader.close()
    assert len(failures) == (1 if os.name == 'nt' else 0)
    if failures:
        assert failures[0].winerror in (5, 32, 33)
    assert durable_state.read_json(target) == after
    assert target.stat().st_nlink == 1
    assert not list(tmp_path.glob('.pending-*'))


def test_persistent_native_snapshot_lock_remains_an_explicit_bounded_failure(tmp_path):
    target = tmp_path / 'стан.json'
    before, after = {'state': 'running'}, {'state': 'finished'}
    durable_state.atomic_json(target, before)
    original = target.read_bytes()
    with target.open('rb') as reader:
        started = time.monotonic()
        if os.name == 'nt':
            with pytest.raises(PermissionError) as caught:
                durable_state.atomic_json(target, after, replace=True)
            elapsed = time.monotonic() - started
            assert caught.value.winerror in (5, 32, 33)
            assert .5 <= elapsed < 3
            assert durable_state.read_json(target) == before
        else:
            durable_state.atomic_json(target, after, replace=True)
            assert durable_state.read_json(target) == after
        assert reader.read() == original
    assert target.stat().st_nlink == 1
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('grow_after_check', [False, True])
def test_json_reader_enforces_callers_smaller_budget_at_opened_handle(tmp_path, monkeypatch, grow_after_check):
    target = tmp_path / 'small-status.json'
    target.write_bytes(b'{}')
    oversized = b'{"message":"' + b'x' * 64 + b'"}'
    if grow_after_check:
        original = durable_state._open_json_file
        def growing(path):
            target.write_bytes(oversized)
            return original(path)
        monkeypatch.setattr(durable_state, '_open_json_file', growing)
    else:
        target.write_bytes(oversized)
    with pytest.raises(ValueError, match='JSON budget'):
        durable_state.read_json(target, maximum=64)
    # The existing default remains independent of the smaller caller budget.
    assert durable_state.read_json(target)['message'] == 'x' * 64


@pytest.mark.parametrize('maximum', [0, -1, True, 1.5, '64', 32 * 1024 * 1024 + 1])
def test_json_reader_refuses_invalid_or_expanded_caller_budget_before_opening(tmp_path, monkeypatch, maximum):
    target = tmp_path / 'status.json'
    target.write_bytes(b'{}')
    monkeypatch.setattr(durable_state, '_open_json_file',
                        lambda path: pytest.fail('opened before validating the caller budget'))
    with pytest.raises(ValueError, match='byte budget'):
        durable_state.read_json(target, maximum=maximum)

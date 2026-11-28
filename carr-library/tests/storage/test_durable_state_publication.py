"""Real filesystem publication boundaries for durable bytes and citation DOCX.

Platform branches use their actual filesystem primitives. A POSIX process exit
after linking still leaves its second name; this suite records that limitation
and never presents the Windows rename contract as a Linux execution result.
"""
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import sys

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


@pytest.mark.parametrize('exists', [False, True])
def test_local_path_checks_each_real_ancestor_without_a_duplicate_existence_probe(
        tmp_path, monkeypatch, exists):
    target = tmp_path.resolve() / 'nested' / 'state.json'
    target.parent.mkdir()
    target.write_bytes(b'{"action":"run"}')
    checked, existence = [], []
    actual_regular, actual_lexists = durable_state.regular, os.path.lexists

    def observe_regular(path, *, directory=False):
        checked.append((Path(path), directory))
        return actual_regular(path, directory=directory)

    def observe_exists(path):
        existence.append(Path(path))
        return actual_lexists(path)

    monkeypatch.setattr(durable_state, 'regular', observe_regular)
    monkeypatch.setattr(os.path, 'lexists', observe_exists)
    assert durable_state.local_path(target, exists=exists) == target
    assert checked == [(parent, True) for parent in reversed(target.parents)] + [(target, False)]
    assert existence == [target], 'Ancestor validation must not precede its lstat with another lstat.'
    assert target.read_bytes() == b'{"action":"run"}'


@pytest.mark.parametrize('exists', [False, True])
def test_local_path_preserves_missing_ancestor_and_leaf_policy(tmp_path, exists):
    target = tmp_path.resolve() / 'missing' / 'nested' / 'state.json'
    if exists:
        with pytest.raises(FileNotFoundError):
            durable_state.local_path(target, exists=True)
    else:
        assert durable_state.local_path(target) == target
    assert not (tmp_path / 'missing').exists()
    leaf = tmp_path.resolve() / 'absent.json'
    with pytest.raises(FileNotFoundError):
        durable_state.local_path(leaf, exists=True)
    assert durable_state.local_path(leaf) == leaf and not leaf.exists()


def test_local_path_rejects_an_actual_file_in_the_ancestor_chain(tmp_path):
    parent = tmp_path / 'ordinary-file'
    parent.write_bytes(b'An authored ordinary file, not a directory.')
    with pytest.raises(ValueError, match='regular local files'):
        durable_state.local_path(parent / 'state.json')
    assert parent.read_bytes() == b'An authored ordinary file, not a directory.'


def test_local_path_retains_actual_multiple_link_refusal(tmp_path):
    source, target = tmp_path / 'original.json', tmp_path / 'linked.json'
    source.write_bytes(b'{"action":"run"}')
    os.link(source, target)
    try:
        assert source.stat().st_nlink == target.stat().st_nlink == 2
        with pytest.raises(ValueError, match='without links'):
            durable_state.local_path(target, exists=True)
        assert source.read_bytes() == target.read_bytes() == b'{"action":"run"}'
    finally:
        target.unlink()


def test_local_path_does_not_hide_an_ancestor_read_error(tmp_path, monkeypatch):
    target = tmp_path.resolve() / 'nested' / 'state.json'
    target.parent.mkdir()
    target.write_bytes(b'{"action":"run"}')
    original = Path.lstat
    observed = []

    def denied(path, *args, **kwargs):
        if path == target.parent:
            observed.append(path)
            raise PermissionError('Authored ancestor denial')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(PermissionError, match='Authored ancestor denial'):
        durable_state.local_path(target, exists=True)
    assert observed == [target.parent]


def test_each_control_snapshot_still_reopens_and_checks_all_ancestors_twice(
        tmp_path, monkeypatch):
    target = tmp_path.resolve() / 'nested' / 'control.json'
    target.parent.mkdir()
    target.write_bytes(b'{"action":"run"}')
    original_regular, original_open = durable_state.regular, durable_state._open_json_file
    checked, opened = [], []

    def observe_regular(path, *, directory=False):
        checked.append((Path(path), directory))
        return original_regular(path, directory=directory)

    def observe_open(path):
        opened.append(Path(path))
        return original_open(path)

    monkeypatch.setattr(durable_state, 'regular', observe_regular)
    monkeypatch.setattr(durable_state, '_open_json_file', observe_open)
    assert durable_state.read_json(target, maximum=4096) == {'action': 'run'}
    target.write_bytes(b'{"action":"pause"}')
    assert durable_state.read_json(target, maximum=4096) == {'action': 'pause'}
    assert opened == [target, target]
    for parent in target.parents:
        assert checked.count((parent, True)) == 4
    assert target.read_bytes() == b'{"action":"pause"}'

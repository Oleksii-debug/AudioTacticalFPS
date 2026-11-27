"""Real private-file/publication controls; synthetic worker output is not Office qualification."""
from contextlib import contextmanager
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace

import pytest

from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.models import AudioCleanupPending
from carr_library.conversion import ConversionBudget, ConversionError, ConversionService
from carr_library.conversion import office, service as module


def _inert_office(root):
    # Reuse the exact admission-only fixture. Its component files are never run.
    path = Path(__file__).with_name('test_office_selection.py')
    spec = importlib.util.spec_from_file_location('_workspace_office_admission_fixture', path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture.installed(root)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    source = tmp_path / 'Справжні збережені байти.doc'
    source.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'unchanged authored source' * 200)
    outputs = tmp_path / 'Читальна копія' / 'Довгий каталог результатів' / 'Остаточний текст'
    outputs.mkdir(parents=True)
    converter = ConversionService(outputs, input_roots=(source,), office=_inert_office(tmp_path),
        budget=ConversionBudget(max_source_bytes=4 * 1024 * 1024,
                                max_text_bytes=256 * 1024, timeout=120))
    # Exercise the Windows adapter on either host without changing os.name.
    converter._office = converter._office_workspace
    with TemporaryDirectory(prefix='carr-lo-test-') as folder:
        temporary = Path(folder).resolve()
        monkeypatch.setattr(module, 'gettempdir', lambda: str(temporary))
        yield converter, source, outputs, temporary


def _synthetic_worker(monkeypatch, converter, original, observed):
    def execute(argv, **kwargs):
        assert argv[1:5] == ['-I', '-B', '-m', 'carr_library.conversion.worker']
        request_path = Path(argv[-1])
        request = json.loads(request_path.read_text(encoding='utf-8'))
        native = request_path.parent
        assert request['operation'] == 'office'
        assert kwargs['owned_tree'] is True and Path(kwargs['cwd']) == native
        assert kwargs['watch'] == (native,)
        assert len(str(native).encode('utf-16-le')) // 2 <= 96
        for name in ('source', 'output', 'report'):
            assert Path(request[name]).parent == native
        assert Path(request['source']).read_bytes() == original
        assert 0 < request['budget']['timeout'] <= converter.budget.timeout
        assert kwargs['budget'].timeout == request['budget']['timeout']
        assert all(kwargs['env'][key] == str(native) for key in ('TMPDIR', 'TEMP', 'TMP'))
        assert kwargs['cancelled']() is False
        text = 'Synthetic worker reading: незмінне джерело.\n'.encode('utf-8')
        details = {'engine': 'synthetic-office-workspace-control', 'sections': [],
            'metadata': {'selected_components_sha256': sha256(json.dumps(
                request['office'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()},
            'warnings': ['Authored synthetic worker; no native Office execution is claimed.']}
        Path(request['output']).write_bytes(text)
        Path(request['report']).write_text(json.dumps(details, ensure_ascii=False), encoding='utf-8')
        observed.append((native, request, details, text))
        return SimpleNamespace(stdout=b'', stderr=b'', returncode=0)
    monkeypatch.setattr(module, 'run_process', execute)


def test_short_native_workspace_preserves_deep_unicode_target_publication(workspace, monkeypatch):
    converter, source, outputs, temporary = workspace
    original, observed = source.read_bytes(), []
    _synthetic_worker(monkeypatch, converter, original, observed)
    result = converter.extract(source, 'прочитаний текст.txt')
    assert len(observed) == 1
    native, request, details, text = observed[0]
    assert native.parent == temporary and not native.is_relative_to(outputs)
    assert not native.exists() and not list(temporary.iterdir())
    assert source.read_bytes() == original
    assert result.path.read_bytes() == text and result.path.parent == outputs
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['text_sha256'] == sha256(text).hexdigest()
    assert result.report['metadata'] == details['metadata']
    assert result.report['verified_for_quotation'] is False
    assert result.report['original_modified'] is False
    assert json.loads(result.report_path.read_bytes()) == result.report
    assert set(outputs.iterdir()) == {result.path, result.report_path}
    assert result.path.stat().st_nlink == result.report_path.stat().st_nlink == 1


def test_native_temporary_volume_has_its_own_capacity_admission(workspace, monkeypatch):
    converter, source, outputs, temporary = workspace
    original, checked = source.read_bytes(), []
    real_usage = module.shutil.disk_usage
    def usage(path):
        path = Path(path)
        checked.append(path)
        return SimpleNamespace(free=0) if path == temporary else real_usage(path)
    monkeypatch.setattr(module.shutil, 'disk_usage', usage)
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('Low-space native volume launched.'))
    with pytest.raises(ConversionError, match='Office temporary volume'):
        converter.extract(source, 'reading.txt')
    assert outputs in checked and temporary in checked
    assert source.read_bytes() == original
    assert not list(outputs.iterdir()) and not list(temporary.iterdir())


def test_overlong_local_temporary_root_is_refused_without_launch(workspace, monkeypatch):
    converter, source, outputs, temporary = workspace
    longer = temporary
    while len(str(longer).encode('utf-16-le')) // 2 <= 100:
        longer = longer / 'local-private-segment'
    longer.mkdir(parents=True)
    monkeypatch.setattr(module, 'gettempdir', lambda: str(longer))
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('Overlong native root launched.'))
    original = source.read_bytes()
    with pytest.raises(ConversionError, match='shorter local temporary directory'):
        converter.extract(source, 'reading.txt')
    assert not list(longer.iterdir()) and not list(outputs.iterdir())
    assert source.read_bytes() == original


@pytest.mark.parametrize('changed', ['source-snapshot', 'native-source', 'report'])
def test_worker_return_cannot_hide_changed_transfer_files(workspace, monkeypatch, changed):
    converter, source, outputs, temporary = workspace
    original, observed = source.read_bytes(), []
    _synthetic_worker(monkeypatch, converter, original, observed)
    real_worker = converter._office_worker
    def worker(native_source, output, report, native, cancelled, **kwargs):
        details = real_worker(native_source, output, report, native, cancelled, **kwargs)
        if changed == 'report':
            value = json.loads(report.read_bytes())
            value['warnings'].append('Changed after the exact worker report was captured.')
            report.write_text(json.dumps(value), encoding='utf-8')
        else:
            target = (next(outputs.glob('.pending-conversion-*/source.doc'))
                      if changed == 'source-snapshot' else native_source)
            target.write_bytes(b'X' * target.stat().st_size)
        return details
    monkeypatch.setattr(converter, '_office_worker', worker)
    with pytest.raises(ConversionError, match='changed'):
        converter.extract(source, 'reading.txt')
    assert len(observed) == 1 and not observed[0][0].exists()
    assert source.read_bytes() == original
    assert not list(outputs.iterdir()) and not list(temporary.iterdir())


def test_cancellation_between_real_copy_chunks_prevents_worker_and_publication(workspace, monkeypatch):
    converter, source, outputs, temporary = workspace
    source.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'A' * (2 * 1024 * 1024))
    original, observed = source.read_bytes(), []
    monkeypatch.setattr(module, 'run_process', lambda *a, **k: pytest.fail('Cancelled copy launched a worker.'))
    def cancelled():
        paths = tuple(temporary.glob('.carr-lo-*/source.doc'))
        if paths and paths[0].stat().st_size >= 1024 * 1024:
            observed.append((paths[0], paths[0].stat().st_size))
            return True
        return False
    with pytest.raises(InterruptedError):
        converter.extract(source, 'reading.txt', cancelled=cancelled)
    assert observed and observed[0][1] == 1024 * 1024
    assert source.read_bytes() == original
    assert not list(outputs.iterdir()) and not list(temporary.iterdir())


def test_copy_uses_captured_size_plus_one_and_rehashes_real_bytes(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.bin', tmp_path / 'copy.bin'
    data = b'A' * (1024 * 1024) + b'captured tail'
    source.write_bytes(data)
    real_reader = office.verified_reader
    reads, seeks = [], []
    class Traced:
        def __init__(self, stream):
            self.stream = stream
        def read(self, size):
            reads.append((self.stream.tell(), size))
            return self.stream.read(size)
        def seek(self, *args):
            seeks.append(args)
            return self.stream.seek(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
    @contextmanager
    def reader(path, **kwargs):
        with real_reader(path, **kwargs) as stream:
            yield Traced(stream)
    monkeypatch.setattr(office, 'verified_reader', reader)
    digest, identity = module._copy_private_file(source, target, len(data),
        lambda: False, time.monotonic() + 30)
    assert digest == sha256(data).hexdigest()
    assert identity == office.file_identity(source.stat())
    assert source.read_bytes() == target.read_bytes() == data
    assert seeks == [(0,)]
    assert reads == [(0, 1024 * 1024), (1024 * 1024, len(b'captured tail') + 1),
                     (len(data), 1)] * 2


def test_copy_rejects_source_growth_before_open_and_never_overwrites(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.bin', tmp_path / 'copy.bin'
    source.write_bytes(b'captured')
    real_reader = office.verified_reader
    @contextmanager
    def grow_before_open(path, **kwargs):
        with source.open('ab') as stream:
            stream.write(b'growth')
        with real_reader(path, **kwargs) as stream:
            yield stream
    monkeypatch.setattr(office, 'verified_reader', grow_before_open)
    with pytest.raises(ConversionError, match='changed'):
        module._copy_private_file(source, target, 64, lambda: False, time.monotonic() + 30)
    assert not target.exists()
    monkeypatch.setattr(office, 'verified_reader', real_reader)
    target.write_bytes(b'previous private result')
    with pytest.raises(FileExistsError):
        module._copy_private_file(source, target, 64, lambda: False, time.monotonic() + 30)
    assert target.read_bytes() == b'previous private result'


def test_copy_rejects_same_size_target_corruption_before_acceptance(tmp_path, monkeypatch):
    source, target = tmp_path / 'source.bin', tmp_path / 'copy.bin'
    source.write_bytes(b'exact captured private input')
    original, real_digest = source.read_bytes(), module._digest
    def altered(path, *args, **kwargs):
        if Path(path) == target:
            target.write_bytes(b'X' * len(original))
        return real_digest(path, *args, **kwargs)
    monkeypatch.setattr(module, '_digest', altered)
    with pytest.raises(ConversionError, match='copy or its destination changed'):
        module._copy_private_file(source, target, 1024, lambda: False, time.monotonic() + 30)
    assert source.read_bytes() == original
    assert target.read_bytes() == b'X' * len(original)


def test_same_pending_owner_retains_both_roots_until_real_child_has_exited(workspace, monkeypatch):
    converter, source, outputs, temporary = workspace
    original = source.read_bytes()
    state = {'child': None, 'allow_close': False}
    def shutdown():
        child = state['child']
        if not state['allow_close']:
            return False
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()
        return child.poll() is not None
    owner = CleanupOwner(shutdown)
    pending = AudioCleanupPending('The actual owned child still holds private files.', cleanup_owner=owner)
    def execute(argv, **kwargs):
        native = Path(kwargs['cwd'])
        outer = next(outputs.glob('.pending-conversion-*'))
        assert kwargs['owned_tree'] is True and native.parent == temporary
        marker = native / 'owned-child.ready'
        script = (
            "import os, pathlib, sys, time\n"
            "one = open(sys.argv[1], 'rb'); two = open(sys.argv[2], 'rb')\n"
            "marker = pathlib.Path(sys.argv[3]); stage = marker.with_suffix('.pending')\n"
            "with stage.open('xb') as out:\n"
            " out.write(b'ready:' + str(os.getpid()).encode('ascii')); out.flush(); os.fsync(out.fileno())\n"
            "stage.rename(marker)\n"
            "time.sleep(120)\n"
        )
        # This stdlib-only child needs no venv imports: own the real Python,
        # rather than a Windows venv redirector that can exit before its child.
        executable = ((getattr(sys, '_base_executable', None) or sys.executable)
                      if os.name == 'nt' else sys.executable)
        child = subprocess.Popen([executable, '-I', '-B', '-c', script,
            str(native / 'source.doc'), str(outer / 'source.doc'), str(marker)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        state.update(child=child, native=native, outer=outer)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists() and child.poll() is None:
            time.sleep(.01)
        assert marker.read_bytes() == b'ready:' + str(child.pid).encode('ascii')
        assert child.poll() is None
        raise pending
    monkeypatch.setattr(module, 'run_process', execute)
    try:
        with pytest.raises(AudioCleanupPending) as raised:
            converter.extract(source, 'reading.txt')
        assert raised.value is pending and raised.value.cleanup_owner is owner
        assert state['child'].poll() is None and not owner.quiescent
        assert state['native'].is_dir() and state['outer'].is_dir()
        assert (state['native'] / 'source.doc').read_bytes() == original
        assert (state['outer'] / 'source.doc').read_bytes() == original
        assert not (outputs / 'reading.txt').exists()
        assert owner.close() is False
        assert state['native'].is_dir() and state['outer'].is_dir()
        state['allow_close'] = True
        assert owner.close() is True and owner.quiescent
        assert state['child'].poll() is not None
        assert not state['native'].exists() and not state['outer'].exists()
        assert not list(outputs.iterdir()) and not list(temporary.iterdir())
        assert source.read_bytes() == original
    finally:
        state['allow_close'] = True
        if state['child'] is not None:
            owner.close()
            if state['child'].poll() is None:
                state['child'].kill()
                state['child'].wait(timeout=10)

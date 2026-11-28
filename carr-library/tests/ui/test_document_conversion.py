"""Real host functions with native picker doubles; no wx/native-runtime qualification."""
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.conversion import ConversionError, ConversionResult
from carr_library.ui import document_conversion as module


@pytest.fixture
def owner(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected library')
    source = tmp_path / 'source.rtf'
    source.write_bytes(br'{\rtf1\ansi Exact original.}')
    runtime = tmp_path / 'selected-pandoc'
    runtime.write_bytes(b'Synthetic metadata, never executed.')
    output = tmp_path / 'output'
    output.mkdir()
    messages, jobs, dialogs = [], [], []
    frame = SimpleNamespace(library=app, active=[workspace], _closing=False, working=False,
        cancel_event=threading.Event(), report=messages.append, book=None)
    frame._workspace = lambda: frame.active[0]
    def run_job(title, operation, done=None, failed=None, **options):
        future = Future()
        jobs.append((title, operation, done, failed, future))
        return future
    frame.run_job = run_job
    changes = {'stage': None, 'change': lambda: None, 'cancel': None}
    class Picker:
        def __init__(self, parent, title, **options):
            self.title = title
            self.stage = ('input' if title.startswith('Choose documents') else
                          'runtime' if title.startswith('Choose the installed') else 'output')
            self.reads, self.destroyed = 0, False
            dialogs.append(self)
        def ShowModal(self):
            if changes['stage'] == self.stage:
                changes['change']()
            return 2 if changes['cancel'] == self.stage else 1
        def GetPaths(self):
            self.reads += 1
            return [str(source)]
        def GetPath(self):
            self.reads += 1
            return str(runtime if self.stage == 'runtime' else output)
        def Destroy(self):
            self.destroyed = True
    wx = SimpleNamespace(FileDialog=Picker, DirDialog=Picker, ID_OK=1, ID_CANCEL=2,
        FD_OPEN=1, FD_FILE_MUST_EXIST=2, FD_MULTIPLE=4)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return SimpleNamespace(frame=frame, app=app, workspace=workspace, source=source,
        runtime=runtime, output=output, jobs=jobs, messages=messages, dialogs=dialogs,
        changes=changes, tmp_path=tmp_path)


@pytest.mark.parametrize('kind', ['application', 'workspace', 'directory', 'closing'])
def test_capture_binds_exact_application_workspace_and_root(owner, kind):
    case = owner
    app, workspace, guard = module._capture_owner(case.frame)
    assert app is case.app and workspace == case.workspace
    if kind == 'application':
        case.frame.library = LibraryApp(case.tmp_path / 'another-library')
    elif kind == 'workspace':
        case.frame.active = [case.app.create_workspace('Another workspace')]
    elif kind == 'directory':
        case.app.root.rename(case.tmp_path / 'retired-library')
        case.app.root.mkdir()
    else:
        case.frame._closing = True
    with pytest.raises((ValueError, OSError)):
        guard()


@pytest.mark.parametrize('stage', ['input', 'runtime', 'output'])
def test_picker_rechecks_owner_before_getting_native_selected_path(owner, stage):
    case = owner
    case.changes.update(stage=stage, change=lambda: setattr(case.frame, 'library',
        LibraryApp(case.tmp_path / 'replacement-library')))
    module.convert_selected_documents(case.frame)
    selected = next(dialog for dialog in case.dialogs if dialog.stage == stage)
    assert selected.reads == 0 and selected.destroyed
    assert all(dialog.destroyed for dialog in case.dialogs)
    assert case.jobs == [] and list(case.output.iterdir()) == []
    assert not (case.app.root / 'state').exists()


@pytest.mark.parametrize('stage', ['input', 'runtime', 'output'])
def test_explicit_picker_cancel_never_admits_a_job(owner, stage):
    case = owner
    case.changes['cancel'] = stage
    module.convert_selected_documents(case.frame)
    assert all(dialog.destroyed for dialog in case.dialogs)
    assert next(dialog for dialog in case.dialogs if dialog.stage == stage).reads == 0
    assert case.jobs == [] and list(case.output.iterdir()) == []
    assert not (case.app.root / 'state').exists()


@pytest.mark.parametrize('kind', ['scope', 'runtime', 'source'])
def test_queued_operation_keeps_captured_selection_before_native_admission(owner, monkeypatch, kind):
    case = owner
    from carr_library.conversion import selected
    monkeypatch.setattr(selected, 'run_process', lambda *a, **k: pytest.fail('Changed queued selection reached a child.'))
    module.convert_selected_documents(case.frame)
    assert len(case.jobs) == 1
    if kind == 'scope':
        case.frame.active = [case.app.create_workspace('Another workspace')]
    elif kind == 'runtime':
        case.runtime.write_bytes(b'Changed selected executable.')
    else:
        case.source.write_bytes(b'Changed selected source.')
    with pytest.raises((ValueError, OSError, RuntimeError)):
        case.jobs[0][1]()
    assert list(case.output.iterdir()) == []


def test_actual_saved_receipt_is_preserved_if_worker_reports_late_attention(owner, monkeypatch):
    case = owner
    result = ConversionResult(case.output / 'reading.txt', {}, case.output / 'report.json')
    result.path.write_bytes(b'Actually saved reading fixture.')
    def saved_then_attention(self, *args, **options):
        error = ConversionError('Saved; later cleanup requires attention.')
        error.completed_conversion = result
        raise error
    monkeypatch.setattr(module.ConversionService, 'extract', saved_then_attention)
    shown = []
    monkeypatch.setattr(module, 'show_reading_copies', lambda *a, **k: shown.append((a, k)))
    module.convert_selected_documents(case.frame)
    with pytest.raises(ConversionError) as caught:
        case.jobs[0][1]()
    assert caught.value.completed_conversions == (result,)
    case.jobs[0][3](caught.value)
    assert shown[0][0][3] == (result,)
    assert 'actually saved' in shown[0][1]['notice']
    assert result.path.read_bytes() == b'Actually saved reading fixture.'


@pytest.mark.parametrize('callback', ['done', 'failed'])
def test_old_scope_completion_does_not_open_results_in_a_new_library(owner, monkeypatch, callback):
    case = owner
    module.convert_selected_documents(case.frame)
    monkeypatch.setattr(module, 'show_reading_copies', lambda *a, **k: pytest.fail('Late results opened in another owner.'))
    result = ConversionResult(case.output / 'reading.txt', {}, case.output / 'report.json')
    case.frame.library = LibraryApp(case.tmp_path / 'new-owner')
    if callback == 'done':
        case.jobs[0][2]((result,))
    else:
        error = ConversionError('Saved before later attention.')
        error.completed_conversions = (result,)
        case.jobs[0][3](error)


def test_supported_format_help_does_not_create_an_untyped_state_directory(owner, monkeypatch):
    case = owner
    shown = []
    desktop = SimpleNamespace(show_text=lambda *args: shown.append(args))
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app', desktop)
    from carr_library.extract import ocr_runtime
    monkeypatch.setattr(ocr_runtime, 'discover_ocr_runtime', lambda: None)
    before = set(case.app.root.rglob('*'))
    module.show_conversion_formats(case.frame)
    assert set(case.app.root.rglob('*')) == before
    assert shown and 'limited reading conversion' in shown[0][-1]
    assert '.rtf: unavailable' in shown[0][-1]


def test_fresh_library_creates_exact_state_then_runs_real_builtin_conversion(owner, monkeypatch):
    from hashlib import sha256
    import json
    case = owner
    assert not (case.app.root / 'state').exists()
    source = case.tmp_path / 'fresh.txt'
    original = 'Fresh library — exact Україна.\n'.encode('utf-8')
    source.write_bytes(original)
    from carr_library.conversion import service as service_module, worker as worker_module
    worker_path = Path(worker_module.__file__).resolve(strict=True)
    checkout_source = Path(__file__).resolve().parents[2] / 'src'
    source_mode = worker_path == checkout_source / 'carr_library' / 'conversion' / 'worker.py'
    actual_run, commands = service_module.run_process, []
    def real_worker(command, **kwargs):
        assert command[1:5] == ['-I', '-B', '-m', 'carr_library.conversion.worker']
        assert len(command) == 6
        commands.append(tuple(command))
        if source_mode:
            # Source-only pytest has no installed application in its isolated child.
            # Activate only these exact current roots; the installed route stays -m.
            assert (checkout_source.parent / 'bootstrap' / '__init__.py').is_file()
            launcher = ("import sys,runpy,pathlib,hashlib,importlib.util; "
                "root,expected,digest,request=sys.argv[1:]; "
                "assert sys.flags.isolated and sys.flags.dont_write_bytecode and __debug__; "
                "sys.path[:0]=[root,str(pathlib.Path(root).parent)]; "
                "origin=pathlib.Path(importlib.util.find_spec('carr_library.conversion.worker').origin).resolve(strict=True); "
                "assert origin==pathlib.Path(expected); "
                "assert hashlib.sha256(origin.read_bytes()).hexdigest()==digest; "
                "sys.argv=['carr_library.conversion.worker',request]; "
                "runpy.run_module('carr_library.conversion.worker',run_name='__main__')")
            command = [*command[:3], '-c', launcher, str(checkout_source),
                       str(worker_path), sha256(worker_path.read_bytes()).hexdigest(), command[-1]]
        return actual_run(command, **kwargs)
    monkeypatch.setattr(service_module, 'run_process', real_worker)
    monkeypatch.setattr(sys.modules['wx'].FileDialog, 'GetPaths',
                        lambda self: [str(source)])
    module.convert_selected_documents(case.frame)
    assert len(case.jobs) == 1 and (case.app.root / 'state').is_dir()
    assert [picker.stage for picker in case.dialogs] == ['input', 'output']
    with ThreadPoolExecutor(max_workers=1) as worker:
        result, = worker.submit(case.jobs[0][1]).result(timeout=60)
    assert len(commands) == 1
    assert result.path.read_bytes() == original == source.read_bytes()
    assert result.report['source_sha256'] == sha256(original).hexdigest()
    assert result.report['original_modified'] is False
    assert result.report['verified_for_quotation'] is False
    assert json.loads(result.report_path.read_text(encoding='utf-8')) == result.report
    assert not list(case.output.glob('.pending-conversion-*'))


def test_fresh_state_admission_rejects_replaced_existing_library_before_mkdir(owner):
    case = owner
    root = case.app.root
    moved = case.tmp_path / 'retired-before-state'
    identity = root.stat().st_dev, root.stat().st_ino
    original = case.source.read_bytes()
    calls = 0
    def guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            root.rename(moved)
            root.mkdir()
            (root / 'canary.txt').write_bytes(b'Unrelated replacement root')
    try:
        with pytest.raises(ConversionError, match='changed before state initialization'):
            module.ConversionService(case.output, input_roots=(case.source,),
                                     state_root=root / 'state', guard=guard)
        assert calls == 2
        assert (moved.stat().st_dev, moved.stat().st_ino) == identity
        assert set(path.name for path in root.iterdir()) == {'canary.txt'}
        assert (root / 'canary.txt').read_bytes() == b'Unrelated replacement root'
        assert not (moved / 'state').exists()
        assert case.source.read_bytes() == original and not list(case.output.iterdir())
    finally:
        if moved.exists():
            (root / 'canary.txt').unlink()
            root.rmdir()
            moved.rename(root)


def test_fresh_state_creation_never_replaces_an_existing_regular_file(owner):
    case = owner
    state = case.app.root / 'state'
    state.write_bytes(b'Existing unrelated state file')
    module.convert_selected_documents(case.frame)
    assert not case.jobs and not list(case.output.iterdir())
    assert state.read_bytes() == b'Existing unrelated state file'
    assert case.messages and case.messages[-1].startswith('Conversion not started: ')

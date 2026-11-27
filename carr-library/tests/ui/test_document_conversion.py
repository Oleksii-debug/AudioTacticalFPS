"""Real host functions with native picker doubles; no wx/native-runtime qualification."""
from concurrent.futures import Future
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
    (app.root / 'state').mkdir(exist_ok=True)
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


@pytest.mark.parametrize('stage', ['input', 'runtime', 'output'])
def test_explicit_picker_cancel_never_admits_a_job(owner, stage):
    case = owner
    case.changes['cancel'] = stage
    module.convert_selected_documents(case.frame)
    assert all(dialog.destroyed for dialog in case.dialogs)
    assert next(dialog for dialog in case.dialogs if dialog.stage == stage).reads == 0
    assert case.jobs == [] and list(case.output.iterdir()) == []


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

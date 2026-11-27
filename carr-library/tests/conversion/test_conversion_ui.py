"""Portable orchestration checks; these are not native keyboard acceptance."""
import sys
from threading import Event
from types import SimpleNamespace

from carr_library.ui import document_conversion as ui


def fake_ui(monkeypatch, sources, destination, frame):
    destroyed, messages = [], []
    class Dialog:
        def __init__(self, values):
            self.values = values
        def ShowModal(self):
            return 1
        def GetPaths(self):
            return [str(value) for value in self.values]
        def GetPath(self):
            return str(self.values[0])
        def Destroy(self):
            destroyed.append(self)
    wx = SimpleNamespace(ID_OK=1, FD_OPEN=1, FD_FILE_MUST_EXIST=2, FD_MULTIPLE=4,
        FileDialog=lambda *a, **k: Dialog(sources), DirDialog=lambda *a, **k: Dialog((destination,)))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app',
                        SimpleNamespace(show_text=lambda *args: messages.append(args)))
    from carr_library.ui import ocr_options
    from carr_library.extract.page_ocr import Config
    monkeypatch.setattr(ocr_options, 'choose_ocr_config', lambda *args: Config())
    frame.report = messages.append
    return destroyed, messages


def test_ui_selects_exact_files_and_uses_existing_job_cancel_event(tmp_path, monkeypatch):
    source = tmp_path / 'selected.txt'
    neighbor = tmp_path / 'not-selected.txt'
    source.write_bytes(b'original')
    neighbor.write_bytes(b'neighbor')
    destination = tmp_path / 'output'
    destination.mkdir()
    frame = SimpleNamespace(_closing=False, working=False, library=None, cancel_event=Event())
    destroyed, messages = fake_ui(monkeypatch, (source,), destination, frame)
    captured = {}
    real_service = ui.ConversionService

    class InspectService(real_service):
        def extract_many(self, sources, *, cancelled, progress):
            captured['inputs'] = self.input_roots
            captured['cancelled'] = cancelled()
            assert self.input_roots == (source,)
            assert cancelled() is True
            return super().extract_many(sources, cancelled=cancelled, progress=progress)

    monkeypatch.setattr(ui, 'ConversionService', InspectService)
    def run_job(title, operation, done, failed, *, progress_tracker, progress_outcome):
        captured['title'] = title
        assert progress_tracker.snapshot().total == 1
        assert progress_outcome(()) == 'completed'
        frame.cancel_event.set()
        try:
            operation()
        except InterruptedError as error:
            failed(error)
        else:
            raise AssertionError('The existing cancellation event was ignored.')
    frame.run_job = run_job
    ui.convert_documents(frame)
    assert captured['cancelled'] is True
    assert len(destroyed) == 2
    assert list(destination.iterdir()) == []
    assert any(isinstance(message, str) and 'stopped' in message for message in messages)


def test_ui_does_not_write_unregistered_files_inside_library(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    library.mkdir()
    source = tmp_path / 'selected.txt'
    source.write_bytes(b'original')
    frame = SimpleNamespace(_closing=False, working=False, library=SimpleNamespace(root=library),
                            cancel_event=Event())
    destroyed, messages = fake_ui(monkeypatch, (source,), library, frame)
    frame.run_job = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Invalid destination reached worker.'))
    ui.convert_documents(frame)
    assert len(destroyed) == 2
    assert any(isinstance(message, str) and 'outside the research library' in message for message in messages)
    assert list(library.iterdir()) == []

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
    def run_job(title, operation, done, failed):
        captured['title'] = title
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


def test_image_selection_uses_existing_model_dialog_and_cancel_starts_no_job(tmp_path, monkeypatch):
    from carr_library.ui import ocr_options
    source, destination = tmp_path / 'selected.tiff', tmp_path / 'output'
    source.write_bytes(b'not yet parsed')
    destination.mkdir()
    frame = SimpleNamespace(_closing=False, working=False, library=None, cancel_event=Event())
    fake_ui(monkeypatch, (source,), destination, frame)
    choices = []
    monkeypatch.setattr(ocr_options, 'choose_model_config',
                        lambda *a, **k: choices.append(k) or None)
    frame.run_job = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Cancelled model choice started a worker.'))
    ui.convert_documents(frame)
    assert choices == [{'preset_root': None}]
    assert list(destination.iterdir()) == []


def test_rtf_converter_picker_cancel_does_not_activate_a_path_tool(tmp_path, monkeypatch):
    source, destination = tmp_path / 'selected.rtf', tmp_path / 'output'
    source.write_bytes(b'{\\rtf1\\ansi Original}')
    destination.mkdir()
    frame = SimpleNamespace(_closing=False, working=False, library=None, cancel_event=Event())
    fake_ui(monkeypatch, (source,), destination, frame)
    monkeypatch.setattr(ui, 'choose_pandoc', lambda frame: None)
    frame.run_job = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Cancelled converter choice started a worker.'))
    ui.convert_documents(frame)
    assert list(destination.iterdir()) == []


def test_native_flow_passes_both_explicit_selections_to_the_current_service(tmp_path, monkeypatch):
    from carr_library.ui import ocr_options
    from carr_library.extract.page_ocr import Config
    sources = (tmp_path / 'selected.rtf', tmp_path / 'selected.png')
    for source in sources:
        source.write_bytes(b'selected document')
    destination = tmp_path / 'output'
    destination.mkdir()
    executable = tmp_path / 'chosen-pandoc'
    executable.write_bytes(b'selected executable')
    frame = SimpleNamespace(_closing=False, working=False, library=None, cancel_event=Event())
    fake_ui(monkeypatch, sources, destination, frame)
    chosen_config = Config(languages='eng', dpi=240)
    monkeypatch.setattr(ocr_options, 'choose_model_config', lambda *a, **k: chosen_config)
    monkeypatch.setattr(ui, 'choose_pandoc', lambda frame: executable)
    captured = {}
    class Service:
        def __init__(self, root, **options):
            captured.update(root=root, **options)
        def extract_many(self, selected, **options):
            captured['executed_sources'] = selected
            assert options['cancelled']() is False
            return ()
    monkeypatch.setattr(ui, 'ConversionService', Service)
    frame.run_job = lambda title, operation, **callbacks: operation()
    ui.convert_documents(frame)
    assert captured['root'] == destination and captured['input_roots'] == sources
    assert captured['image_ocr'] is True and captured['ocr_config'] is chosen_config
    assert captured['pandoc'] == executable and captured['executed_sources'] == sources

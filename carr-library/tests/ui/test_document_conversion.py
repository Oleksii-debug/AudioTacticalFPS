"""Native handler contracts with real conversion; actual wx is a separate gate."""
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.ui import document_conversion as ui


@pytest.fixture
def screen(tmp_path, monkeypatch):
    source = tmp_path / 'selected.txt'
    source.write_bytes('Original reading text.\r\nТочний оригінал.'.encode())
    output = tmp_path / 'exports'
    output.mkdir()
    config = SimpleNamespace(sources=[source], destination=output, select=True,
                             choose_output=True, destroyed=[], jobs=[], messages=[], reports=[],
                             on_modal=lambda title: None, on_destroy=lambda title: None,
                             queued=False, pending=[])
    class Dialog:
        def __init__(self, parent, title, **kwargs):
            self.title = title
        def ShowModal(self):
            config.on_modal(self.title)
            selected = config.select if 'documents to convert' in self.title else config.choose_output
            return 1 if selected else 0
        def GetPaths(self):
            return [str(path) for path in config.sources]
        def GetPath(self):
            return str(config.destination)
        def Destroy(self):
            config.destroyed.append(self.title)
            config.on_destroy(self.title)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(FileDialog=Dialog, DirDialog=Dialog,
        FD_OPEN=1, FD_FILE_MUST_EXIST=2, FD_MULTIPLE=4, ID_OK=1))
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app', SimpleNamespace(
        show_text=lambda frame, title, text: config.messages.append((title, text))))
    class Frame:
        library = LibraryApp(tmp_path / 'library')
        cancel_event = Event()
        _closing = False
        def report(self, message):
            config.reports.append(message)
        def run_job(self, name, operation, done):
            config.jobs.append(name)
            if config.queued:
                config.pending.append((operation, done))
                return
            done(operation())
    return Frame(), config, source, output


def test_selected_source_becomes_a_real_separate_unverified_derivative(screen):
    frame, config, source, output = screen
    ui.convert_selected_documents(frame)
    text, = output.glob('*.txt')
    receipt = Path(str(text) + '.provenance.json')
    assert text.read_bytes() == source.read_bytes()
    assert json.loads(receipt.read_bytes())['verified_for_quotation'] is False
    assert len(config.jobs) == 1 and len(config.messages) == 1
    assert len(config.destroyed) == 2
    assert not config.reports


@pytest.mark.parametrize('decision', ['select', 'choose_output'])
def test_cancelled_native_dialog_does_not_start_work(screen, decision):
    frame, config, _, output = screen
    setattr(config, decision, False)
    ui.convert_selected_documents(frame)
    assert not config.jobs and not list(output.iterdir())


def test_native_handler_preserves_existing_outputs(screen):
    frame, config, _, output = screen
    target = output / '0001-selected-reading.txt'
    target.write_bytes(b'existing other work')
    with pytest.raises(FileExistsError):
        ui.convert_selected_documents(frame)
    assert target.read_bytes() == b'existing other work'
    assert not config.messages


def test_native_conversion_cannot_write_into_private_library(screen):
    frame, config, _, output = screen
    config.destination = frame.library.root
    with pytest.raises(ValueError, match='outside'):
        ui.convert_selected_documents(frame)
    assert not list(frame.library.root.glob('*reading.txt*'))
    assert not config.messages


def test_format_inventory_does_not_create_an_unregistered_private_state_folder(screen):
    frame, config, _, _ = screen
    ui.show_conversion_formats(frame)
    assert config.messages and '.txt: available' in config.messages[0][1]
    assert not (frame.library.root / 'state' / 'conversion').exists()


def test_image_handler_reuses_existing_verified_model_selector(screen, monkeypatch):
    from PIL import Image, ImageDraw, ImageFont
    from carr_library.extract.page_ocr import Config
    import carr_library.ui.ocr_selection as selection
    frame, config, source, output = screen
    picture = source.with_suffix('.png')
    image = Image.new('RGB', (1100, 180), 'white')
    ImageDraw.Draw(image).text((30, 45), 'REAL UI IMAGE CONVERSION', fill='black',
                              font=ImageFont.load_default(size=48))
    image.save(picture)
    config.sources = [picture]
    selected = []
    monkeypatch.setattr(selection, 'choose_model_config',
                        lambda parent: selected.append(parent) or Config(languages='eng'))
    ui.convert_selected_documents(frame)
    text, = output.glob('*.txt')
    assert 'REAL UI IMAGE CONVERSION' in text.read_text(encoding='utf-8')
    report = json.loads(Path(str(text) + '.provenance.json').read_bytes())
    assert selected == [frame]
    assert report['metadata']['languages'] == ['eng']
    assert report['verified_for_quotation'] is False


def test_unsupported_image_orientation_is_explicit_instead_of_silently_dropped(screen, monkeypatch):
    from carr_library.extract.page_ocr import Config
    import carr_library.ui.ocr_selection as selection
    frame, config, source, output = screen
    config.sources = [source.with_suffix('.png')]
    monkeypatch.setattr(selection, 'choose_model_config', lambda parent: Config(orientation=True))
    ui.convert_selected_documents(frame)
    assert not config.jobs and not list(output.iterdir())
    assert config.reports and 'orientation off' in config.reports[0]


def change_binding(frame, kind):
    previous = frame.library
    if kind == 'library':
        frame.library = LibraryApp(previous.root.with_name('replacement-library'))
    elif kind == 'root':
        previous.root = previous.root.with_name('changed-root-path')
    elif kind == 'directory':
        path = previous.root
        path.rename(path.with_name('former-root'))
        LibraryApp(path)
    elif kind == 'closing':
        frame._closing = True
    elif kind == 'assets':
        previous.assets_dir = previous.assets_dir.with_name('changed-assets')
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize('modal', ['source', 'destination', 'destroy'])
@pytest.mark.parametrize('kind', ['library', 'root', 'directory', 'closing'])
def test_every_modal_rejects_a_changed_or_closed_library(screen, modal, kind):
    frame, config, _, output = screen
    original_root = frame.library.root
    # This destination could become incorrectly allowed if the handler consulted
    # a replacement frame.library only after the modal returned.
    config.destination = original_root
    def mutate(title):
        is_source = 'documents to convert' in title
        if (modal == 'source' and is_source or modal == 'destination' and not is_source
                or modal == 'destroy' and is_source):
            change_binding(frame, kind)
    if modal == 'destroy':
        config.on_destroy = mutate
    else:
        config.on_modal = mutate
    ui.convert_selected_documents(frame)
    assert not config.jobs and not config.messages
    assert not list(original_root.glob('*reading.txt*'))
    assert not list(output.iterdir())


def test_model_selection_modal_rechecks_the_original_library(screen, monkeypatch):
    from carr_library.extract.page_ocr import Config
    import carr_library.ui.ocr_selection as selection
    frame, config, source, output = screen
    config.sources = [source.with_suffix('.png')]
    def choose(_):
        change_binding(frame, 'library')
        return Config()
    monkeypatch.setattr(selection, 'choose_model_config', choose)
    ui.convert_selected_documents(frame)
    assert not config.jobs and not config.messages and not list(output.iterdir())
    assert len(config.destroyed) == 1


@pytest.mark.parametrize('kind', ['library', 'root', 'directory', 'closing', 'assets'])
def test_queued_conversion_rechecks_scope_before_constructing_the_service(screen, kind):
    frame, config, _, output = screen
    original_root = frame.library.root
    config.destination = original_root
    config.queued = True
    ui.convert_selected_documents(frame)
    operation, _ = config.pending.pop()
    change_binding(frame, kind)
    with pytest.raises(InterruptedError, match='library changed|closed'):
        operation()
    assert not config.messages and not list(original_root.glob('*reading.txt*'))
    assert not list(output.iterdir())


def test_scope_change_after_real_parsing_cancels_before_publication(screen, monkeypatch):
    import carr_library.conversion.service as service
    frame, config, source, output = screen
    before = source.read_bytes()
    actual = service.run_process
    def changed(*args, **kwargs):
        result = actual(*args, **kwargs)
        change_binding(frame, 'library')
        return result
    monkeypatch.setattr(service, 'run_process', changed)
    with pytest.raises(InterruptedError):
        ui.convert_selected_documents(frame)
    assert source.read_bytes() == before and not list(output.iterdir())
    assert not config.messages


def test_scope_change_between_files_preserves_first_receipt_and_stops_the_batch(screen, monkeypatch):
    frame, config, source, output = screen
    second = source.with_name('second.txt')
    second.write_bytes(b'The second source must remain unprocessed.')
    config.sources = [source, second]
    actual = ui.ConversionService.extract
    def changed(converter, *args, **kwargs):
        result = actual(converter, *args, **kwargs)
        change_binding(frame, 'library')
        return result
    monkeypatch.setattr(ui.ConversionService, 'extract', changed)
    with pytest.raises(InterruptedError, match='Previously completed'):
        ui.convert_selected_documents(frame)
    text, = output.glob('*.txt')
    assert text.read_bytes() == source.read_bytes()
    assert json.loads(Path(str(text) + '.provenance.json').read_bytes())['verified_for_quotation'] is False
    assert not list(output.glob('*second*')) and not config.messages


@pytest.mark.parametrize('kind', ['library', 'root', 'directory', 'closing'])
def test_completed_conversion_does_not_open_a_stale_result_view(screen, kind):
    frame, config, source, output = screen
    config.queued = True
    ui.convert_selected_documents(frame)
    operation, done = config.pending.pop()
    summary = operation()
    text, = output.glob('*.txt')
    assert text.read_bytes() == source.read_bytes()
    change_binding(frame, kind)
    done(summary)
    assert not config.messages
    assert json.loads(Path(str(text) + '.provenance.json').read_bytes())['verified_for_quotation'] is False


def test_observed_scope_change_cannot_be_revived_by_switching_back(screen, monkeypatch):
    frame, config, _, output = screen
    original_library = frame.library
    actual = ui.ConversionService.extract
    def changed(converter, *args, **kwargs):
        change_binding(frame, 'library')
        assert kwargs['cancelled']()
        frame.library = original_library
        return actual(converter, *args, **kwargs)
    monkeypatch.setattr(ui.ConversionService, 'extract', changed)
    with pytest.raises(InterruptedError):
        ui.convert_selected_documents(frame)
    assert not list(output.iterdir()) and not config.messages


def test_cancelled_format_inventory_does_not_recreate_a_removed_library_root(screen):
    frame, config, _, _ = screen
    original_root = frame.library.root
    config.queued = True
    ui.show_conversion_formats(frame)
    operation, _ = config.pending.pop()
    original_root.rename(original_root.with_name('moved-library'))
    with pytest.raises(InterruptedError):
        operation()
    assert not original_root.exists() and not config.messages


@pytest.mark.parametrize('kind', ['library', 'directory', 'closing'])
def test_format_inventory_does_not_show_a_stale_result(screen, kind):
    frame, config, _, _ = screen
    config.queued = True
    ui.show_conversion_formats(frame)
    operation, done = config.pending.pop()
    summary = operation()
    change_binding(frame, kind)
    done(summary)
    assert not config.messages


@pytest.mark.parametrize('handler', [ui.convert_selected_documents, ui.show_conversion_formats])
def test_closing_frame_does_not_open_selectors_or_start_work(screen, handler):
    frame, config, _, output = screen
    frame._closing = True
    frame.library = None
    handler(frame)
    assert not config.destroyed and not config.jobs and not config.messages
    assert not list(output.iterdir())


def test_conversion_menu_installation_registers_stable_commands_exactly_once(screen, monkeypatch):
    frame, _, _, _ = screen
    import wx
    from carr_library.ui.command_registry import CommandRegistry
    class Menu:
        def __init__(self):
            self.items = []
        def Append(self, identity, label):
            item = SimpleNamespace(identity=identity, label=label)
            self.items.append(item)
            return item
    class MenuBar:
        def __init__(self):
            self.menus = []
        def Append(self, menu, label):
            self.menus.append((menu, label))
    monkeypatch.setattr(wx, 'Menu', Menu, raising=False)
    monkeypatch.setattr(wx, 'ID_ANY', -1, raising=False)
    monkeypatch.setattr(wx, 'EVT_MENU', object(), raising=False)
    bindings = []
    frame.menu_bar = MenuBar()
    frame.Bind = lambda *args: bindings.append(args)
    items = ui.install_conversion_actions(frame)
    assert ui.install_conversion_actions(frame) is items
    assert len(frame.menu_bar.menus) == 1 and len(bindings) == 2
    pending = frame._pending_keyboard_commands
    registry = CommandRegistry(spec for spec, _ in pending)
    assert set(registry.specs) == {'conversion.documents', 'conversion.formats'}
    assert registry.get('conversion.documents').context == 'main'
    assert registry.get('conversion.formats').context == 'main'
    assert tuple(item for _, item in pending) == items

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
                             choose_output=True, destroyed=[], jobs=[], messages=[], reports=[])
    class Dialog:
        def __init__(self, parent, title, **kwargs):
            self.title = title
        def ShowModal(self):
            selected = config.select if 'documents to convert' in self.title else config.choose_output
            return 1 if selected else 0
        def GetPaths(self):
            return [str(path) for path in config.sources]
        def GetPath(self):
            return str(config.destination)
        def Destroy(self):
            config.destroyed.append(self.title)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(FileDialog=Dialog, DirDialog=Dialog,
        FD_OPEN=1, FD_FILE_MUST_EXIST=2, FD_MULTIPLE=4, ID_OK=1))
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app', SimpleNamespace(
        show_text=lambda frame, title, text: config.messages.append((title, text))))
    class Frame:
        library = LibraryApp(tmp_path / 'library')
        cancel_event = Event()
        def report(self, message):
            config.reports.append(message)
        def run_job(self, name, operation, done):
            config.jobs.append(name)
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

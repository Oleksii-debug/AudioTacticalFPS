"""The real conversion handler borrows its captured application's coordinator."""
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

import carr_library.conversion.service as service_module
import carr_library.ui.document_conversion as conversion_ui
from carr_library.operations import ResourceBroker, ResourceLimits


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.cancel_event = threading.Event()
        self._closing = False
        self.working = False
        self.jobs = []
        self.messages = []

    def run_job(self, title, operation, done):
        assert not self.working and not self._closing
        self.working = True
        # Match DesktopFrame: admission acknowledges a new explicit operation.
        self.cancel_event.clear()
        self.jobs.append((title, operation, done))

    def report(self, message):
        self.messages.append(message)


def _handler(tmp_path, monkeypatch, after_picker=lambda _kind, _frame: None, *, suffix='.txt'):
    library, other, inputs, output = (tmp_path / name for name in ('library', 'other', 'inputs', 'output'))
    for path in (library, other, inputs, output):
        path.mkdir()
    source = inputs / ('chosen' + suffix)
    raw = '  Exact Український source 😀.\r\nNo normalization.\t '.encode('utf-8')
    source.write_bytes(raw)
    frame, shown, destroyed = Frame(library), [], []

    class FilePicker:
        kind = 'source'

        def __init__(self, owner, title, **kwargs):
            assert owner is frame
            self.owner = owner
            shown.append(self.kind)

        def ShowModal(self):
            after_picker(self.kind, self.owner)
            return 5100

        def GetPaths(self):
            return [str(source)]

        def Destroy(self):
            destroyed.append(self.kind)

    class DirectoryPicker(FilePicker):
        kind = 'destination'

        def GetPath(self):
            return str(output)

    wx = SimpleNamespace(FileDialog=FilePicker, DirDialog=DirectoryPicker,
                         ID_OK=5100, FD_OPEN=1, FD_FILE_MUST_EXIST=2, FD_MULTIPLE=4)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return SimpleNamespace(frame=frame, library=library, other=other, source=source,
                           raw=raw, output=output, shown=shown, destroyed=destroyed)


def _no_work(monkeypatch, case):
    opening = Path.open

    def guarded(path, mode='r', *args, **kwargs):
        if path == case.source and 'r' in mode:
            pytest.fail('An inactive native selection read its source bytes.')
        return opening(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)
    monkeypatch.setattr(service_module, 'run_process',
                        lambda *_a, **_k: pytest.fail('An inactive selection started a native worker.'))
    monkeypatch.setattr(ResourceBroker, 'for_app', classmethod(
        lambda *_a, **_k: pytest.fail('An inactive selection created a coordinator.')))


def _change(case, kind):
    if kind == 'object':
        case.frame.library = SimpleNamespace(root=case.other)
    elif kind == 'path':
        case.frame.library.root = case.other
    elif kind == 'close':
        case.frame._closing = True
    else:
        assert kind == 'cancel'
        case.frame.cancel_event.set()


def test_real_handler_uses_library_broker_on_worker_and_preserves_late_committed_result(tmp_path, monkeypatch):
    case = _handler(tmp_path, monkeypatch)
    original_factory = ResourceBroker.for_app
    roots, results = [], []

    def tracked(cls, root, **kwargs):
        assert kwargs == {}
        roots.append(Path(root))
        return original_factory(root, limits=ResourceLimits(cpu_slots=1,
            memory_bytes=2 * 1024**3, disk_bytes=512 * 1024**2, min_free_disk_bytes=0))

    monkeypatch.setattr(ResourceBroker, 'for_app', classmethod(tracked))
    conversion_ui.convert_selected_documents(case.frame)
    assert roots == []  # Neither a picker nor its GUI continuation opens the broker.
    assert case.shown == case.destroyed == ['source', 'destination']
    assert len(case.frame.jobs) == 1
    title, work, done = case.frame.jobs[0]
    assert title == 'Convert documents for reading'
    summary = work()  # Real service, isolated parser, source hash and create-only receipt.
    assert roots == [case.library]
    assert not (case.output / 'runtime').exists()
    assert not (case.source.parent / 'runtime').exists()
    text = case.output / '0001-chosen-reading.txt'
    receipt = text.with_name(text.name + '.provenance.json')
    assert text.read_bytes() == case.raw and case.source.read_bytes() == case.raw
    report = json.loads(receipt.read_text(encoding='utf-8'))
    assert report['source_sha256'] == report['text_sha256'] == sha256(case.raw).hexdigest()
    assert report['verified_for_quotation'] is False
    assert str(text) in summary and str(receipt) in summary
    assert original_factory(case.library).snapshot()['leases'] == []
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app',
                        SimpleNamespace(show_text=lambda *args: results.append(args)))
    case.frame.cancel_event.set()  # A later Cancel does not erase committed files.
    done(summary)
    assert len(results) == 1 and results[0][0] is case.frame
    assert str(text) in results[0][2] and str(receipt) in results[0][2]
    assert text.read_bytes() == case.raw


@pytest.mark.parametrize('picker,change', [
    ('source', 'object'), ('destination', 'object'), ('destination', 'path'),
    ('source', 'close'), ('destination', 'close')])
def test_library_change_or_close_during_picker_never_queues_conversion(
        tmp_path, monkeypatch, picker, change):
    holder = {}

    def after(kind, frame):
        if kind == picker:
            _change(holder['case'], change)

    case = _handler(tmp_path, monkeypatch, after)
    holder['case'] = case
    with monkeypatch.context() as patch:
        _no_work(patch, case)
        conversion_ui.convert_selected_documents(case.frame)
    assert case.frame.jobs == []
    assert case.shown == case.destroyed
    assert case.shown == (['source'] if picker == 'source' else ['source', 'destination'])
    assert tuple(case.output.iterdir()) == ()
    assert not (case.library / 'runtime').exists()
    assert not (case.other / 'runtime').exists()
    assert case.source.read_bytes() == case.raw


@pytest.mark.parametrize('change', ['object', 'path', 'close', 'cancel'])
def test_queued_handler_rechecks_captured_selection_before_source_or_broker(tmp_path, monkeypatch, change):
    case = _handler(tmp_path, monkeypatch)
    conversion_ui.convert_selected_documents(case.frame)
    assert len(case.frame.jobs) == 1
    _change(case, change)
    with monkeypatch.context() as patch:
        _no_work(patch, case)
        with pytest.raises(InterruptedError, match='no longer active'):
            case.frame.jobs[0][1]()
    assert tuple(case.output.iterdir()) == ()
    assert not (case.library / 'runtime').exists()
    assert not (case.other / 'runtime').exists()
    assert case.source.read_bytes() == case.raw


def test_captured_library_guard_remains_active_after_broker_factory(tmp_path, monkeypatch):
    case = _handler(tmp_path, monkeypatch)
    original_factory = ResourceBroker.for_app
    brokers = []

    def changed(cls, root, **kwargs):
        assert Path(root) == case.library and kwargs == {}
        broker = original_factory(root)
        brokers.append(broker)
        _change(case, 'object')
        return broker

    monkeypatch.setattr(ResourceBroker, 'for_app', classmethod(changed))
    conversion_ui.convert_selected_documents(case.frame)
    opening = Path.open

    def guarded(path, mode='r', *args, **kwargs):
        if path == case.source and 'r' in mode:
            pytest.fail('A revoked source was read after resource construction.')
        return opening(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        patch.setattr(service_module, 'run_process',
                      lambda *_a, **_k: pytest.fail('A revoked selection launched a child.'))
        with pytest.raises(InterruptedError, match='stopped'):
            case.frame.jobs[0][1]()
    assert len(brokers) == 1 and brokers[0].snapshot()['leases'] == []
    assert not (case.other / 'runtime').exists()
    assert tuple(case.output.iterdir()) == ()
    assert case.source.read_bytes() == case.raw


def test_ocr_language_picker_cannot_continue_under_a_replaced_library(tmp_path, monkeypatch):
    case = _handler(tmp_path, monkeypatch, suffix='.png')

    def choose(owner):
        assert owner is case.frame
        _change(case, 'object')
        return SimpleNamespace(languages='eng')

    monkeypatch.setitem(sys.modules, 'carr_library.ui.ocr_options',
                        SimpleNamespace(choose_installed_languages=choose))
    with monkeypatch.context() as patch:
        _no_work(patch, case)
        conversion_ui.convert_selected_documents(case.frame)
    assert case.shown == case.destroyed == ['source']
    assert case.frame.jobs == [] and tuple(case.output.iterdir()) == ()
    assert case.source.read_bytes() == case.raw

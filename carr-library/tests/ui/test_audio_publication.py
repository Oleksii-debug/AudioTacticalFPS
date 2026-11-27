"""Portable contracts for the native publication choices and shared host.

These tests exercise the real action adapter with controlled dialog selections.
They do not simulate successful native GUI, physical playback or NVDA evidence.
"""
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.audio.jobs import RenderCancelled
from carr_library.ui import audio_publication as ui


def receipt(status='complete'):
    checksum = 'a' * 64
    return {'schema': 'carr-audio-publication/1', 'id': 'b' * 32, 'status': status,
        'project_root': '/synthetic/audio', 'output': '/synthetic/book.epub',
        'artifact': {'size': 128, 'sha256': checksum},
        'source': {'job_id': checksum, 'project_id': checksum, 'job_sha256': checksum,
            'project_sha256': checksum, 'source_script_sha256': checksum,
            'artifacts': [{'name': 'part-' + str(n) + '.json', 'size': 1, 'sha256': checksum}
                          for n in range(6)],
            'origin': 'carr-audio-production/1', 'input_kind': 'narration',
            'approved_library_evidence': False, 'frame_rate': 16000, 'frame_intervals': []},
        'created': '2026-11-27T00:00:00Z', 'format': 'epub', 'notice': 'Synthetic receipt'}


@pytest.fixture
def frame(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    calls, reports = [], []
    value = SimpleNamespace(library=SimpleNamespace(root=root), cancel_event=Event(),
                            working=False, busy=False, _closing=False, report=reports.append)
    value.calls, value.reports = calls, reports
    value.run_job = lambda *args, **kwargs: calls.append((args, kwargs))
    return value


def test_only_valid_complete_receipt_can_announce_completion():
    assert ui.publication_outcome(receipt()) == 'completed'
    with pytest.raises(ValueError, match='complete'):
        ui.publication_outcome(receipt('prepared'))
    forged = receipt()
    forged['source']['approved_library_evidence'] = True
    with pytest.raises(ValueError, match='provenance'):
        ui.publication_outcome(forged)


@pytest.mark.parametrize('attribute', ['working', 'busy', '_closing'])
def test_busy_or_closing_host_does_not_open_another_publication_workflow(frame, attribute):
    setattr(frame, attribute, True)
    actions = ui.PublicationActions(frame)
    actions._service = lambda: pytest.fail('No chooser may open while the host is unavailable.')
    actions.publish()
    actions.verify()
    assert frame.calls == []


@pytest.mark.parametrize('kind', [0, 1])
def test_export_uses_existing_host_and_typed_durable_observer(frame, tmp_path, kind):
    observed = []
    def export(*args, **options):
        observed.append((args, options))
        options['progress']('Synthetic measured phase')
        return receipt()
    service = SimpleNamespace(private_root=frame.library.root.resolve(),
                              export_epub=export, export_daisy=export)
    actions = ui.PublicationActions(frame)
    approval = object()
    actions._start_export(service, 'a' * 64, tmp_path / 'book.epub', kind,
                          {'language': 'uk'}, approval, frame.library)
    (args, options), = frame.calls
    assert options['done_is_observer'] is True
    assert options['progress_outcome'] is ui.publication_outcome
    result = args[1]()
    assert result['status'] == 'complete'
    _, sent = observed[0]
    assert sent['approval'] is approval and sent['language'] == 'uk'
    assert sent['cancelled']() is False
    frame.cancel_event.set()
    assert sent['cancelled']() is True
    assert options['progress_outcome'](result) == 'completed'
    args[2](result)
    assert 'Publication saved' in frame.reports[-1]


def test_cancelled_service_is_reported_as_interruption_not_completed(frame, tmp_path):
    def cancelled(*args, **kwargs):
        raise RenderCancelled('Public synthetic cancellation')
    service = SimpleNamespace(private_root=frame.library.root.resolve(), export_epub=cancelled)
    ui.PublicationActions(frame)._start_export(
        service, 'a' * 64, tmp_path / 'book.epub', 0, {}, object(), frame.library)
    (args, _), = frame.calls
    with pytest.raises(InterruptedError, match='cancellation'):
        args[1]()


@pytest.mark.parametrize('method', ['export', 'verify'])
def test_library_change_before_queued_work_requires_fresh_selection(frame, tmp_path, method):
    previous = frame.library
    frame.library = SimpleNamespace(root=previous.root)
    service = SimpleNamespace(private_root=previous.root.resolve())
    actions = ui.PublicationActions(frame)
    if method == 'export':
        actions._start_export(service, 'a' * 64, tmp_path / 'book.epub', 0, {}, object(), previous)
    else:
        actions._start_verification(service, 'b' * 32, tmp_path / 'book.epub', previous)
    assert not frame.calls and 'changed' in frame.reports[-1]


def test_verification_forwards_explicit_file_and_cancellation_to_existing_service(frame, tmp_path):
    calls = []
    def verify(identity, **options):
        calls.append((identity, options))
        return receipt()
    service = SimpleNamespace(private_root=frame.library.root.resolve(), verify=verify)
    selected = tmp_path / 'explicitly-selected.epub'
    ui.PublicationActions(frame)._start_verification(service, 'b' * 32, selected, frame.library)
    (args, options), = frame.calls
    assert options['done_is_observer'] is True
    assert options['progress_outcome'] is ui.publication_outcome
    args[1]()
    assert calls[0][1]['output'] == selected
    frame.cancel_event.set()
    assert calls[0][1]['cancelled']()


def test_destination_is_captured_before_queued_export(frame, tmp_path, monkeypatch):
    from carr_library.audio.publishing import PublicationService
    root = tmp_path / 'audio'
    root.mkdir()
    service = PublicationService(root, private_root=frame.library.root)
    output = tmp_path / 'selected.epub'
    queued = []
    class Dialog:
        def __init__(self, *args, **kwargs):
            self.name = None
        def SetName(self, value):
            self.name = value
        def ShowModal(self):
            return 1
        def GetPath(self):
            return str(output)
        def Destroy(self):
            pass
    fake = SimpleNamespace(FileDialog=Dialog, ID_OK=1, FD_SAVE=2,
                           CallAfter=lambda *args: queued.append(args))
    monkeypatch.setitem(sys.modules, 'wx', fake)
    actions = ui.PublicationActions(frame)
    choices = iter((0, 0))
    actions._choice = lambda *args: next(choices)
    actions._text = lambda title, *args: 'uk' if title == 'Publication language' else ''
    actions._choose_export(service, ({'title': 'Saved production', 'id': 'a' * 64},), frame.library)
    item, = queued
    assert item[0] == actions._start_export
    approval = item[-2]
    assert approval.target == output and approval.require_absent
    assert frame.calls == []
    output.write_bytes(b'Another writer won before queued work')
    with pytest.raises((ValueError, FileExistsError)):
        service._destination(output, approval)
    assert output.read_bytes() == b'Another writer won before queued work'


def test_existing_audio_menu_receives_native_actions_without_another_registry(frame, monkeypatch):
    appended, bound = [], []
    class Menu:
        def Append(self, identity, label):
            item = object()
            appended.append((label, item))
            return item
    frame.Bind = lambda event, handler, item: bound.append((event, handler, item))
    fake = SimpleNamespace(ID_ANY=-1, EVT_MENU=object())
    monkeypatch.setitem(sys.modules, 'wx', fake)
    actions = ui.install_publication_actions(frame, Menu())
    assert frame.publication_actions is actions
    assert len(appended) == len(bound) == 2
    assert 'EPUB or DAISY' in appended[0][0]
    assert [row[1] for row in bound] == [actions.publish, actions.verify]
    assert [row[2] for row in bound] == [row[1] for row in appended]

"""Actual desktop export handler with real saved research and a controlled dialog.

No window is created. Native wx is imported so this checks the shipped handler,
while the dialog supplies deterministic synthetic paths without user interaction.
"""
from types import SimpleNamespace
from threading import Event

import pytest

pytest.importorskip('wx')

from carr_library.application import LibraryApp
from carr_library.research import export_file
from carr_library.ui import desktop_app


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Export boundary fixture')
    topic = app.create_topic(workspace, 'Synthetic research')
    imported = app.add_text(workspace, 'Synthetic source',
                            'An unchanged source exact quotation.',
                            authors=('Synthetic Author',))
    hit, = app.search(workspace, 'source exact quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Separate research note')
    return SimpleNamespace(app=app, workspace=workspace, topic=topic,
                           original=app.assets.resolve(imported['asset']))


def export_using_dialog(research, destination, monkeypatch, *, format_index=1, consent=True):
    class FileDialog:
        def __init__(self, *args, **kwargs):
            pass

        def ShowModal(self):
            return desktop_app.wx.ID_OK

        def GetPath(self):
            return str(destination)

        def GetFilterIndex(self):
            return format_index

        def Destroy(self):
            pass

    monkeypatch.setattr(desktop_app.wx, 'FileDialog', FileDialog)
    # The real handler/helper capture the destination before this explicit
    # dialog answer. This headless protocol never opens a native question.
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace',
                        lambda *_: consent)
    reports = []

    def run_job(title, operation, done=None, failed=None):
        try:
            result = operation()
        except Exception as error:
            if failed is None:
                raise
            failed(error)
        else:
            if done is not None:
                done(result)

    frame = SimpleNamespace(library=research.app,
                            _topic=lambda: (research.workspace, research.topic),
                            active=(research.workspace, research.topic),
                            report=reports.append, cancel_event=Event(), run_job=run_job)
    desktop_app.DesktopFrame.export_topic(frame, None)
    return reports


@pytest.mark.parametrize('target', ['database', 'original'])
def test_desktop_export_cannot_overwrite_private_library(research, target, monkeypatch):
    app = research.app
    destination = app.database if target == 'database' else research.original
    before_database = app.database.read_bytes()
    before_original = research.original.read_bytes()

    reports = export_using_dialog(research, destination, monkeypatch)

    assert len(reports) == 1 and reports[0].startswith('Export not completed:')
    assert 'outside the private library folder' in reports[0]
    assert app.database.read_bytes() == before_database
    assert research.original.read_bytes() == before_original
    reopened = LibraryApp(app.root)
    assert reopened.evidence(research.workspace, research.topic)[0].anchor.quote == 'source exact quotation'


def test_desktop_export_to_external_file_keeps_source_and_notes_separate(research, tmp_path, monkeypatch):
    destination = tmp_path / 'research-topic.txt'
    destination.write_text('An older export explicitly selected for replacement.', encoding='utf-8')
    expected = research.app.export_topic(research.workspace, research.topic, format='text')
    before_original = research.original.read_bytes()

    reports = export_using_dialog(research, destination, monkeypatch)

    assert reports == ['Topic exported with quotations and research notes kept separate.']
    assert destination.read_text(encoding='utf-8') == expected
    assert 'source exact quotation' in expected and 'Separate research note' in expected
    assert research.original.read_bytes() == before_original
    assert not list(tmp_path.glob('.carr-export-*'))


def test_declined_desktop_overwrite_preserves_existing_output(research, tmp_path, monkeypatch):
    destination = tmp_path / 'research-topic.txt'
    destination.write_bytes(b'Unapproved existing export')
    reports = export_using_dialog(research, destination, monkeypatch, consent=False)
    assert reports == []
    assert destination.read_bytes() == b'Unapproved existing export'


def test_failed_export_preserves_existing_output_and_library(research, tmp_path, monkeypatch):
    destination = tmp_path / 'research-topic.txt'
    old_output = b'An existing export must survive failed replacement.\n'
    destination.write_bytes(old_output)
    before_database = research.app.database.read_bytes()
    before_original = research.original.read_bytes()

    def reject_replace(source, target):
        raise OSError('Synthetic replacement failure')

    monkeypatch.setattr(export_file.os, 'replace', reject_replace)
    reports = export_using_dialog(research, destination, monkeypatch)

    assert reports == ['Export not completed: Synthetic replacement failure']
    assert destination.read_bytes() == old_output
    assert research.app.database.read_bytes() == before_database
    assert research.original.read_bytes() == before_original
    assert not list(tmp_path.glob('.carr-export-*'))


@pytest.mark.parametrize('format_index,extension', [(5, 'docx'), (6, 'html')])
def test_desktop_document_export_uses_the_selected_format(
        research, tmp_path, monkeypatch, format_index, extension):
    from io import BytesIO
    from docx import Document

    destination = tmp_path / ('research-topic.' + extension)
    before_original = research.original.read_bytes()
    reports = export_using_dialog(research, destination, monkeypatch,
                                  format_index=format_index)
    assert len(reports) == 1 and reports[0].startswith('Research document exported')
    if extension == 'docx':
        text = '\n'.join(p.text for p in Document(BytesIO(destination.read_bytes())).paragraphs)
    else:
        text = destination.read_text(encoding='utf-8')
        assert '<html' in text
    assert 'source exact quotation' in text and 'Separate research note' in text
    assert research.original.read_bytes() == before_original
    assert not list(tmp_path.glob('.research-export-*'))

"""The shipped export handler, controlled dialogs, and real research/file guards.

Only the actual method is compiled from its AST, so no native wx display is
needed. The backend reauthorizes real saved research through the existing text
export, then supplies deterministic serialization bytes/text: document rendering
has its own tests. The file writers, LibraryApp and SQLite are not replaced.
"""
import ast
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library import application
from carr_library.application import LibraryApp
from carr_library.research import export_file
from carr_library.research.export_file import write_binary_export, write_export
from carr_library.ui.export_destination import approve_export_destination


FORMATS = [(5, 'docx', '.docx'), (6, 'html', '.html')]
PAYLOADS = {
    'docx': b'PK\x03\x04\x00deterministic document bytes\r\n\xff',
    'html': '<!doctype html>\n<html lang="en"><body>Дослідження</body></html>\n',
}


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Academic export fixture')
    topic = app.create_topic(workspace, 'Selected topic')
    other_topic = app.create_topic(workspace, 'Another topic')
    foreign_workspace = app.create_workspace('Other library')
    imported = app.add_text(workspace, 'Synthetic source',
                            'An exact research quotation for a synthetic dossier.',
                            authors=('Synthetic Author',))
    hit, = app.search(workspace, 'exact research quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Separate researcher note.')
    return SimpleNamespace(app=app, workspace=workspace, topic=topic,
                           other_topic=other_topic, foreign_workspace=foreign_workspace,
                           original=app.assets.resolve(imported['asset']))


@pytest.fixture
def harness(research, monkeypatch):
    def create(destination, index, *, cancel_dialog=False):
        h = SimpleNamespace(library=research.app, active=(research.workspace, research.topic),
                            cancel_event=Event(), reports=[], jobs=[], dialogs=[],
                            after_render=lambda: None)
        h.report = h.reports.append
        h._topic = Mock(side_effect=lambda: h.active)
        h.run_job = lambda *args: h.jobs.append(args)
        wx = SimpleNamespace(ID_OK=100, ID_CANCEL=101, YES=102, NO=103,
                             FD_SAVE=1, FD_OVERWRITE_PROMPT=2,
                             YES_NO=4, NO_DEFAULT=8, ICON_QUESTION=16)
        wx.MessageBox = Mock(return_value=wx.YES)

        class FileDialog:
            def __init__(self, *args, **kwargs):
                self.options = kwargs
                self.destroyed = False
                self.path_reads = 0
                h.dialogs.append(self)

            def ShowModal(self):
                return wx.ID_CANCEL if cancel_dialog else wx.ID_OK

            def GetPath(self):
                self.path_reads += 1
                return str(destination)

            def GetFilterIndex(self):
                return index

            def Destroy(self):
                self.destroyed = True

        wx.FileDialog = FileDialog
        h.wx = wx
        actual_export = research.app.export_topic

        def authorized_payload(workspace, topic, *, format, cancelled):
            actual_export(workspace, topic, format='text', cancelled=cancelled)
            h.after_render()
            return PAYLOADS[format]

        h.backend = Mock(side_effect=authorized_payload)
        monkeypatch.setattr(research.app, 'export_topic', h.backend)
        source = Path(application.__file__).parent / 'ui' / 'desktop_app.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        frame = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
        method = next(node for node in frame.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'export_topic')
        def approve(parent, destination, *, private_root):
            return approve_export_destination(parent, destination, private_root=private_root,
                confirm=lambda owner, target: wx.MessageBox(
                    f'Replace the existing export {target.name}?', 'Replace export',
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, owner) == wx.YES)

        namespace = {'Path': Path, 'wx': wx, 'write_export': write_export,
                     'write_binary_export': write_binary_export,
                     'approve_export_destination': approve, 'export_completion': export_file.export_completion}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        h.invoke = lambda: namespace['export_topic'](h, None)

        def run():
            assert len(h.jobs) == 1
            _title, operation, done, failed = h.jobs.pop()
            try:
                result = operation()
            except Exception as error:
                failed(error)
            else:
                done(result)

        h.run = run
        return h
    return create


def payload_bytes(format):
    value = PAYLOADS[format]
    return value if isinstance(value, bytes) else value.encode('utf-8')


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_document_filter_appends_its_suffix_and_publishes_exact_payload(
        harness, research, tmp_path, index, format, suffix):
    requested = tmp_path / 'research-topic'
    target = requested.with_suffix(suffix)
    original = research.original.read_bytes()
    h = harness(requested, index)
    h.invoke()
    assert len(h.jobs) == 1 and not target.exists()
    dialog, = h.dialogs
    assert dialog.destroyed and dialog.options['defaultFile'] == 'research-topic'
    assert dialog.options['style'] & h.wx.FD_SAVE
    assert not dialog.options['style'] & h.wx.FD_OVERWRITE_PROMPT
    assert dialog.options['wildcard'].split('|')[index * 2 + 1] == '*' + suffix
    h.wx.MessageBox.assert_not_called()
    h.run()
    assert target.read_bytes() == payload_bytes(format)
    assert not requested.exists() and research.original.read_bytes() == original
    assert h.backend.call_args.kwargs['format'] == format
    assert h.reports == ['Research document exported with navigable headings, source citations, '
                         'and quotations kept separate from research notes.']


@pytest.mark.parametrize('index,format,suffix', FORMATS)
@pytest.mark.parametrize('allow', [False, True])
def test_appended_existing_destination_is_confirmed_before_work(
        harness, tmp_path, index, format, suffix, allow):
    requested = tmp_path / 'existing export'
    target = requested.with_suffix(suffix)
    target.write_bytes(b'Keep the existing document until confirmed.')
    h = harness(requested, index)
    h.wx.MessageBox.return_value = h.wx.YES if allow else h.wx.NO
    h.invoke()
    h.wx.MessageBox.assert_called_once()
    message, caption, style, parent = h.wx.MessageBox.call_args.args
    assert target.name in message and caption == 'Replace export' and parent is h
    assert style & h.wx.NO_DEFAULT
    assert target.read_bytes() == b'Keep the existing document until confirmed.'
    h.backend.assert_not_called()
    if allow:
        h.run()
        assert target.read_bytes() == payload_bytes(format)
    else:
        assert not h.jobs and not h.reports
    assert not requested.exists()


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_cancelled_save_dialog_never_reads_path_or_starts_export(
        harness, tmp_path, index, format, suffix):
    target = tmp_path / ('cancelled' + suffix)
    h = harness(target, index, cancel_dialog=True)
    h.invoke()
    dialog, = h.dialogs
    assert dialog.destroyed and dialog.path_reads == 0
    assert not h.jobs and not h.reports and not target.exists()
    h.backend.assert_not_called()
    h.wx.MessageBox.assert_not_called()


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_mismatched_suffix_preserves_existing_file_without_starting_work(
        harness, tmp_path, index, format, suffix):
    target = tmp_path / 'wrong-format.md'
    target.write_bytes(b'Existing Markdown must not become a different file format.')
    h = harness(target, index)
    h.invoke()
    assert not h.jobs and target.read_bytes().startswith(b'Existing Markdown')
    h.backend.assert_not_called()
    assert len(h.reports) == 1 and f'needs a {suffix} filename' in h.reports[0]


@pytest.mark.parametrize('index', [-1, 7])
def test_invalid_filter_index_does_not_select_another_format(harness, tmp_path, index):
    h = harness(tmp_path / 'invalid', index)
    h.invoke()
    assert not h.jobs
    h.backend.assert_not_called()
    assert h.reports == ['Export not completed: Choose an export format.']


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_existing_uppercase_suffix_is_kept_and_uses_bound_overwrite_question(
        harness, tmp_path, index, format, suffix):
    target = tmp_path / ('Existing' + suffix.upper())
    target.write_bytes(b'Native file dialog accepted replacement of this exact path.')
    h = harness(target, index)
    h.invoke()
    h.wx.MessageBox.assert_called_once()
    h.run()
    assert target.read_bytes() == payload_bytes(format)


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_cancellation_after_render_preserves_existing_destination(
        harness, tmp_path, index, format, suffix):
    target = tmp_path / ('cancelled' + suffix)
    target.write_bytes(b'Old output remains intact.')
    h = harness(target, index)
    h.after_render = h.cancel_event.set
    h.invoke()
    h.run()
    assert target.read_bytes() == b'Old output remains intact.'
    assert h.reports == ['Export not completed: Export cancelled; the destination was not changed.']
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_native_handler_keeps_cancellation_live_during_file_publication(
        harness, tmp_path, monkeypatch, index, format, suffix):
    target = tmp_path / ('cancelled-while-saving' + suffix)
    target.write_bytes(b'Previous exported document.')
    h = harness(target, index)
    monkeypatch.setattr(export_file.os, 'fsync', lambda _fd: h.cancel_event.set())
    h.invoke()
    h.run()
    assert target.read_bytes() == b'Previous exported document.'
    assert len(h.reports) == 1 and 'cancelled' in h.reports[0].lower()
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('change', ['library', 'workspace', 'topic'])
def test_changed_binding_before_worker_rejects_without_reading_research(
        harness, research, tmp_path, change):
    target = tmp_path / 'selected.docx'
    target.write_bytes(b'Previous export.')
    h = harness(target, 5)
    h.invoke()
    h._topic = Mock(side_effect=AssertionError('Worker must not access the widget-derived scope.'))
    if change == 'library':
        h.library = LibraryApp(tmp_path / 'other-root')
    elif change == 'workspace':
        h.active = (research.foreign_workspace, research.topic)
    else:
        h.active = (research.workspace, research.other_topic)
    h.run()
    h.backend.assert_not_called()
    h._topic.assert_not_called()
    assert target.read_bytes() == b'Previous export.'
    assert len(h.reports) == 1 and 'library or topic changed' in h.reports[0]


@pytest.mark.parametrize('change', ['library', 'topic'])
def test_changed_binding_after_render_rejects_before_publication(
        harness, research, tmp_path, change):
    target = tmp_path / 'selected.html'
    target.write_bytes(b'Previous export.')
    h = harness(target, 6)
    replacement = LibraryApp(tmp_path / 'other-root') if change == 'library' else None

    def switch():
        if change == 'library':
            h.library = replacement
        else:
            h.active = (research.workspace, research.other_topic)

    h.after_render = switch
    h.invoke()
    h.run()
    assert target.read_bytes() == b'Previous export.'
    assert len(h.reports) == 1 and 'library or topic changed' in h.reports[0]
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('index,format,suffix', FORMATS)
@pytest.mark.parametrize('folder', ['root', 'assets'])
def test_new_document_cannot_be_written_inside_private_library(
        harness, research, index, format, suffix, folder):
    parent = research.app.root if folder == 'root' else research.app.assets_dir
    target = parent / ('new-document' + suffix)
    database = research.app.database.read_bytes()
    original = research.original.read_bytes()
    h = harness(target, index)
    h.invoke()
    assert not h.jobs
    h.backend.assert_not_called()
    assert not target.exists()
    assert research.app.database.read_bytes() == database
    assert research.original.read_bytes() == original
    assert len(h.reports) == 1 and 'outside the private library folder' in h.reports[0]


@pytest.mark.parametrize('index,format,suffix', FORMATS)
@pytest.mark.parametrize('existing', [False, True], ids=['new', 'confirmed-existing'])
@pytest.mark.parametrize('phase', ['queued', 'rendering'])
def test_topic_document_keeps_external_destination_changes(
        harness, tmp_path, index, format, suffix, existing, phase):
    target = tmp_path / ('selected' + suffix)
    if existing:
        target.write_bytes(b'Approved previous export')
    h = harness(target, index)
    h.invoke()
    assert len(h.jobs) == 1
    change = lambda: target.write_bytes(b'Newer document from another application')
    if phase == 'queued':
        change()
    else:
        h.after_render = change
    h.run()
    assert target.read_bytes() == b'Newer document from another application'
    assert len(h.reports) == 1 and 'destination changed' in h.reports[0]
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('index,format,suffix', FORMATS)
def test_topic_document_reports_actual_publication_cleanup_notice(harness, tmp_path, monkeypatch,
                                                                 index, format, suffix):
    # The actual handler keeps its editable DOCX/HTML renderer contract and
    # carries the publisher's receipt to the native completion callback.
    target = tmp_path / ('selected' + suffix)
    module = sys.modules[__name__]
    name = 'write_binary_export' if format == 'docx' else 'write_export'
    writer, calls = getattr(module, name), []
    def saved(*args, **kwargs):
        receipt = writer(*args, **kwargs)
        calls.append(receipt)
        return export_file.ExportReceipt(receipt.path, 'Saved; temporary cleanup needs attention.')
    monkeypatch.setattr(module, name, saved)
    h = harness(target, index)
    h.invoke()
    h.run()
    assert target.read_bytes() == payload_bytes(format)
    assert len(calls) == 1 and 'Saved; temporary cleanup needs attention.' in h.reports[-1]

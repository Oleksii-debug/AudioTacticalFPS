"""Current native translation callbacks with controlled layout service boundaries.

Actual DOCX/EPUB workers and publication are exercised by the assistant suites.
These tests use the existing owner-thread controls and real shared worker fixture.
"""
from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.assistant.library_translation import LibrarySelection, capture_library_sources
from carr_library.assistant.translation_partition import make_partition
from carr_library.ui import translation_dialog as ui

_spec = importlib.util.spec_from_file_location('layout_current_ui_fixture',
    Path(__file__).with_name('test_library_translation_dialog.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
form = _base.form


@pytest.fixture
def layout_form(form, tmp_path, monkeypatch):
    selected = capture_library_sources(form.app, form.workspace, (LibrarySelection(form.book['edition']),))
    captures, reviews, prepares, exports, pickers = [], [], [], [], []
    modal = [lambda: None]
    output = tmp_path / 'NEW translated.docx'
    last = []
    def capture(library, workspace, edition, *, kind, **kwargs):
        form.service.record('layout.capture', library, workspace, edition, kind, kwargs)
        assert library is form.app and workspace == form.workspace and edition == form.book['edition']
        kwargs['guard']()
        if kwargs['cancelled']():
            raise InterruptedError('Cancelled before layout capture.')
        profile = 'carr-docx-body-layout/1' if kind == 'docx' else 'carr-epub-xhtml-layout/1'
        partition = make_partition(selected.text, profile, 'a' * 64, [len(selected.text)])
        value = SimpleNamespace(kind=kind, source_text=selected.text, binding=deepcopy(selected.binding),
            source_partition=partition, summary=lambda: {'format': kind, 'notice': 'Explicit fixture layout limits.'})
        captures.append(value)
        last[:] = [value]
        return value
    def review(service, profile, document, source, target, budget, **kwargs):
        service.record('layout.review', document, source, target, budget, kwargs)
        assert service is form.service and profile.to_dict() == form.profile.to_dict() and document is last[0]
        kwargs['guard']()
        if kwargs['cancelled']():
            raise InterruptedError('Cancelled before layout review.')
        reviews.append((source, target, budget, kwargs))
        return {'translation': {'estimated_cost': None}, 'review_sha256': 'b' * 64,
                'provider_calls_started': False, 'layout': document.summary()}
    def prepare(service, profile, document, source, target, budget, **kwargs):
        service.record('layout.prepare', document, source, target, budget, kwargs)
        assert document is last[0] and kwargs['approved_review'] == 'b' * 64
        kwargs['guard']()
        if kwargs['cancelled']():
            raise InterruptedError('Cancelled before prepared job.')
        prepares.append(kwargs)
        form.job.update(source_text=document.source_text, source_binding=deepcopy(document.binding),
            source_partition=deepcopy(document.source_partition), source_language=source,
            target_language=target, authorization={'budget': budget}, profile=profile.to_dict())
        return form.job['id']
    def export(service, identity, destination, **kwargs):
        service.record('layout.export', identity, destination, kwargs)
        assert identity == form.job['id']
        kwargs['guard']()
        if kwargs['cancelled']():
            raise InterruptedError('Cancelled before new-file export.')
        exports.append((identity, destination, kwargs))
        Path(destination).write_bytes(b'UI publisher boundary fixture; not a document.')
        return {'format': last[0].kind if last else 'docx', 'path': str(destination),
                'notice': 'Explicit structural limitation; no visual-layout claim.'}
    class FilePicker(form.wx.DirDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pickers.append(self)
        def ShowModal(self):
            self.native()
            modal[0]()
            return form.wx.ID_OK
        def GetPath(self):
            self.native()
            return str(output)
    form.wx.FileDialog, form.wx.FD_SAVE = FilePicker, 0
    monkeypatch.setattr(ui, 'capture_layout', capture)
    monkeypatch.setattr(ui, 'review_layout', review)
    monkeypatch.setattr(ui, 'prepare_layout', prepare)
    monkeypatch.setattr(ui, 'export_layout', export)
    form.layout = SimpleNamespace(captures=captures, reviews=reviews, prepares=prepares, exports=exports,
        pickers=pickers, modal=modal, output=output, selected=selected, last=last, export=export)
    _base.fill(form)
    return form


def select_layout(form, mode=4):
    form.controls['input_scope'].SetSelection(mode)
    form.dialog.on_scope_changed()
    begin = len(form.frame.calls)
    form.dialog.on_sources()
    form.frame.finish(begin)
    form.frame.finish(begin + 1)


def review_layout(form):
    index = len(form.frame.calls)
    form.dialog.on_preview()
    form.frame.finish(index)


def run_layout(form):
    index = len(form.frame.calls)
    form.dialog.on_run()
    form.frame.finish(index)


@pytest.mark.parametrize('mode,kind', [(4, 'docx'), (5, 'epub')])
def test_explicit_layout_capture_review_and_prepare_reuse_the_current_worker_and_approval(layout_form, mode, kind):
    form = layout_form
    select_layout(form, mode)
    assert not form.controls['selected'].editable
    assert form.controls['selected'].GetValue() == form.layout.selected.text.replace('\r\n', '\n')
    assert len(form.layout.captures) == 1 and form.layout.captures[0].kind == kind
    assert not form.layout.reviews and not form.layout.prepares and not form.messages
    review_layout(form)
    assert len(form.layout.reviews) == 1 and not form.layout.prepares
    assert 'No request sent' in form.controls['status'].GetValue()
    run_layout(form)
    assert len(form.messages) == 1 and len(form.layout.prepares) == 1
    assert len([row for row in form.service.calls if row[0] == 'resume']) == 1
    assert not any(row[0] in ('preview', 'submit') for row in form.service.calls)
    assert form.job['source_partition'] == form.layout.captures[0].source_partition
    assert form.controls['format_export'].enabled


@pytest.mark.parametrize('boundary', ['close', 'library', 'topic', 'cancel', 'mode'])
def test_revoked_queued_layout_capture_cannot_replace_the_current_display(layout_form, boundary):
    form = layout_form
    form.controls['selected'].ChangeValue('Keep this text until a current capture completes.')
    form.controls['input_scope'].SetSelection(4)
    form.dialog.on_scope_changed()
    form.dialog.on_sources()
    form.frame.finish(0)
    if boundary == 'close':
        form.dialog.on_close()
    elif boundary == 'library':
        form.frame.library = SimpleNamespace(root=form.app.root)
    elif boundary == 'topic':
        form.frame.active = (form.workspace, 'changed-topic')
    elif boundary == 'cancel':
        form.dialog.translation_cancel.set()
    else:
        form.controls['input_scope'].SetSelection(5)
    form.frame.finish(1)
    assert form.controls['selected'].GetValue() == 'Keep this text until a current capture completes.'
    assert not form.layout.prepares and not form.messages


@pytest.mark.parametrize('field,value', [('target', 'de'), ('budget', '2'), ('rate', '9'),
                                         ('input_scope', 5)])
def test_changed_visible_layout_review_cannot_authorize_new_requests(layout_form, field, value):
    form = layout_form
    select_layout(form)
    review_layout(form)
    if field == 'input_scope':
        form.controls[field].SetSelection(value)
    else:
        form.controls[field].ChangeValue(value)
    before = len(form.frame.calls)
    form.dialog.on_run()
    assert len(form.frame.calls) == before and not form.messages
    assert not form.layout.prepares and 'Preview this exact' in form.controls['status'].GetValue()


@pytest.mark.parametrize('boundary', ['profile', 'close', 'topic', 'cancel'])
def test_current_approval_is_rechecked_before_queued_formatted_submission(layout_form, boundary):
    form = layout_form
    select_layout(form)
    review_layout(form)
    index = len(form.frame.calls)
    form.dialog.on_run()
    assert len(form.messages) == 1
    if boundary == 'profile':
        form.profiles.save(replace(form.profile, name='Changed account declaration'))
    elif boundary == 'close':
        form.dialog.on_close()
    elif boundary == 'topic':
        form.frame.active = (form.workspace, 'changed-topic')
    else:
        form.dialog.translation_cancel.set()
    form.frame.finish(index)
    assert not form.layout.prepares
    assert not any(row[0] == 'resume' for row in form.service.calls)


def test_explicit_saved_formatted_job_restores_partition_passively_and_exports_offline(layout_form):
    form = layout_form
    selected = form.layout.selected
    partition = make_partition(selected.text, 'carr-docx-body-layout/1', 'a' * 64, [len(selected.text)])
    form.job.update(source_text=selected.text, source_binding=deepcopy(selected.binding),
                    source_partition=partition)
    form.dialog.on_jobs()
    form.frame.finish(0)
    form.frame.finish(1)
    assert form.controls['input_scope'].GetSelection() == 4
    assert form.controls['format_export'].enabled and not form.controls['selected'].editable
    assert not form.layout.captures and not form.layout.reviews and not form.layout.prepares
    form.dialog.on_format_export()
    form.frame.finish(2)
    assert len(form.layout.pickers) == 1 and form.layout.pickers[0].destroyed
    form.frame.finish(3)
    assert form.layout.output.exists() and len(form.layout.exports) == 1
    assert not form.messages and not any(row[0] in ('submit', 'resume') for row in form.service.calls)
    assert 'Saved and verified new DOCX' in form.controls['status'].GetValue()


@pytest.mark.parametrize('boundary', ['close', 'topic', 'settings', 'cancel'])
def test_file_picker_rechecks_current_form_before_starting_new_file_publication(layout_form, boundary):
    form = layout_form
    select_layout(form)
    review_layout(form)
    run_layout(form)
    def change():
        if boundary == 'close':
            form.dialog.on_close()
        elif boundary == 'topic':
            form.frame.active = (form.workspace, 'other-topic')
        elif boundary == 'settings':
            form.controls['target'].ChangeValue('de')
        else:
            form.dialog.translation_cancel.set()
    form.layout.modal[0] = change
    index = len(form.frame.calls)
    form.dialog.on_format_export()
    form.frame.finish(index)
    assert len(form.frame.calls) == index + 1 and not form.layout.exports
    assert not form.layout.output.exists() and all(p.destroyed for p in form.layout.pickers)


def test_completed_export_is_reported_when_stop_arrives_after_publisher_return(layout_form, monkeypatch):
    form = layout_form
    select_layout(form)
    review_layout(form)
    run_layout(form)
    def published(*args, **kwargs):
        result = form.layout.export(*args, **kwargs)
        form.dialog.translation_cancel.set()
        return result
    monkeypatch.setattr(ui, 'export_layout', published)
    index = len(form.frame.calls)
    form.dialog.on_format_export()
    form.frame.finish(index)
    form.frame.finish(index + 1)
    assert form.layout.output.exists() and len(form.layout.exports) == 1
    assert 'Saved and verified new DOCX' in form.controls['status'].GetValue()
    assert 'Operation stopped' not in form.controls['status'].GetValue()


def test_changed_scope_after_export_queue_prevents_worker_publication(layout_form):
    form = layout_form
    select_layout(form)
    review_layout(form)
    run_layout(form)
    index = len(form.frame.calls)
    form.dialog.on_format_export()
    form.frame.finish(index)
    form.frame.active = (form.workspace, 'new-topic')
    form.frame.finish(index + 1)
    assert not form.layout.exports and not form.layout.output.exists()


def test_ordinary_text_mode_keeps_the_existing_preview_and_submit_route(layout_form):
    form = layout_form
    form.dialog.on_preview()
    form.frame.finish(0)
    form.dialog.on_run()
    form.frame.finish(1)
    assert [name for name, _ in form.service.calls] == ['preview', 'submit', 'resume']
    assert not form.layout.captures and not form.layout.prepares
    assert not form.controls['format_export'].enabled


def _reject_dead_native_calls(form, monkeypatch):
    original = form.wx.Dialog.native
    def native(control):
        assert not control.destroyed, 'A destroyed native control was accessed.'
        assert not form.dialog.destroyed, 'A control outlived its destroyed translation owner.'
        return original(control)
    monkeypatch.setattr(form.wx.Dialog, 'native', native)


@pytest.mark.parametrize('operation_name', ['capture', 'export'])
@pytest.mark.parametrize('phase', ['queued', 'completed', 'failed'])
def test_direct_owner_destroy_revokes_queued_and_late_layout_delivery(
        layout_form, monkeypatch, operation_name, phase):
    import threading
    form = layout_form
    if operation_name == 'capture':
        form.controls['input_scope'].SetSelection(4)
        form.dialog.on_scope_changed()
        form.dialog.on_sources()
        form.frame.finish(0)
        index = 1
    else:
        select_layout(form)
        review_layout(form)
        run_layout(form)
        index = len(form.frame.calls)
        form.dialog.on_format_export()
        form.frame.finish(index)
        index += 1
    before = (form.controls['selected'].value, form.controls['status'].value)
    operation, done, failed = form.frame.calls[index]
    result = []
    if phase != 'queued':
        def run():
            try:
                result.append((True, operation()))
            except BaseException as error:
                result.append((False, error))
        worker = threading.Thread(target=run)
        worker.start()
        worker.join(10)
        assert not worker.is_alive() and result and result[0][0]
    _reject_dead_native_calls(form, monkeypatch)
    form.dialog.Destroy()
    assert form.dialog._translation_destroyed and form.dialog.translation_cancel.is_set()
    if phase == 'queued':
        form.frame.finish(index)
    else:
        form.frame.working = False
        (done(result[0][1]) if phase == 'completed' else failed(RuntimeError('Late worker failure.')))
    assert (form.controls['selected'].value, form.controls['status'].value) == before
    assert not form.frame.working
    assert form.layout.output.exists() == (operation_name == 'export' and phase != 'queued')
    if operation_name == 'capture' and phase == 'queued':
        assert not form.layout.captures


@pytest.mark.parametrize('boundary', ['picker-destroy', 'parent-destroy', 'library-swap'])
def test_formatted_picker_checks_native_ownership_before_reading_its_path(
        layout_form, monkeypatch, boundary):
    form = layout_form
    select_layout(form)
    review_layout(form)
    run_layout(form)
    reads, original = [], form.wx.FileDialog.GetPath
    def path(picker):
        reads.append(picker)
        return original(picker)
    monkeypatch.setattr(form.wx.FileDialog, 'GetPath', path)
    _reject_dead_native_calls(form, monkeypatch)
    def change():
        picker = form.layout.pickers[-1]
        if boundary in ('picker-destroy', 'parent-destroy'):
            picker.Destroy()
        if boundary == 'parent-destroy':
            form.dialog.Destroy()
        elif boundary == 'library-swap':
            form.frame.library = SimpleNamespace(root=form.app.root)
    form.layout.modal[0] = change
    index = len(form.frame.calls)
    form.dialog.on_format_export()
    form.frame.finish(index)
    assert not reads and not form.layout.exports and not form.layout.output.exists()
    assert len(form.frame.calls) == index + 1
    assert form.layout.pickers[-1].destroyed


def test_child_destroy_notifications_do_not_revoke_owner_or_current_picker(layout_form):
    form = layout_form
    skipped = []
    form.dialog.handlers[form.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
        GetId=lambda: form.dialog.GetId() + 1, Skip=lambda: skipped.append('owner child')))
    assert not form.dialog._translation_destroyed and not form.dialog.translation_cancel.is_set()
    select_layout(form)
    review_layout(form)
    run_layout(form)
    def child_event():
        picker = form.layout.pickers[-1]
        picker.handlers[form.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
            GetId=lambda: picker.GetId() + 1, Skip=lambda: skipped.append('picker child')))
    form.layout.modal[0] = child_event
    index = len(form.frame.calls)
    form.dialog.on_format_export()
    form.frame.finish(index)
    form.frame.finish(index + 1)
    assert skipped == ['owner child', 'picker child']
    assert len(form.layout.exports) == 1 and form.layout.output.exists()


@pytest.mark.parametrize('mode', [4, 5])
@pytest.mark.parametrize('count', [0, 2])
def test_formatted_capture_refuses_non_single_selection_before_worker_admission(layout_form, mode, count):
    import inspect
    form = layout_form
    form.controls['input_scope'].SetSelection(mode)
    form.dialog.on_scope_changed()
    form.dialog.on_sources()
    finish = form.frame.calls[0][1]
    chooser = inspect.getclosurevars(finish).nonlocals['complete']
    capture = inspect.getclosurevars(chooser).nonlocals['capture']
    choices = tuple(LibrarySelection(form.book['edition']) for _ in range(count))
    with pytest.raises(ValueError, match='exactly one'):
        capture(choices)
    assert len(form.frame.calls) == 1 and not form.layout.captures


def test_modal_return_after_direct_destroy_does_not_destroy_native_owner_again(layout_form, monkeypatch):
    form = layout_form
    destroyed, original = [], form.dialog.Destroy
    def destroy():
        destroyed.append(True)
        return original()
    monkeypatch.setattr(form.dialog, 'Destroy', destroy)
    monkeypatch.setattr(form.dialog, 'ShowModal', lambda: form.dialog.Destroy(), raising=False)
    monkeypatch.setattr(ui, 'create_translation_dialog', lambda frame: form.dialog)
    _reject_dead_native_calls(form, monkeypatch)
    ui.show_translation(form.frame)
    assert destroyed == [True] and form.dialog._translation_destroyed


@pytest.mark.parametrize('mode', [4, 5])
@pytest.mark.parametrize('boundary', ['picker-destroy', 'parent-destroy', 'library-swap', 'child-id'])
def test_formatted_source_picker_rechecks_native_and_library_ownership_before_selection(
        layout_form, monkeypatch, mode, boundary):
    form = layout_form
    pickers, reads, skipped = [], [], []
    original_show = form.wx.SingleChoiceDialog.ShowModal
    original_selection = form.wx.SingleChoiceDialog.GetSelection
    def selection(picker):
        reads.append(picker)
        return original_selection(picker)
    def modal(picker):
        pickers.append(picker)
        result = original_show(picker)
        if boundary in ('picker-destroy', 'parent-destroy'):
            picker.Destroy()
        if boundary == 'parent-destroy':
            form.dialog.Destroy()
        elif boundary == 'library-swap':
            form.frame.library = SimpleNamespace(root=form.app.root)
        elif boundary == 'child-id':
            picker.handlers[form.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
                GetId=lambda: id(picker) + 1, Skip=lambda: skipped.append(True)))
        return result
    monkeypatch.setattr(form.wx.SingleChoiceDialog, 'GetSelection', selection)
    monkeypatch.setattr(form.wx.SingleChoiceDialog, 'ShowModal', modal)
    _reject_dead_native_calls(form, monkeypatch)
    form.controls['input_scope'].SetSelection(mode)
    form.dialog.on_scope_changed()
    form.dialog.on_sources()
    form.frame.finish(0)
    assert len(pickers) == 1 and pickers[0].destroyed
    if boundary == 'child-id':
        assert reads == pickers and skipped == [True]
        form.frame.finish(1)
        assert len(form.layout.captures) == 1
    else:
        assert not reads and not form.layout.captures
        assert len(form.frame.calls) == 1


@pytest.mark.parametrize('route', ['saved-job', 'ordinary-export', 'source-range'])
@pytest.mark.parametrize('boundary', ['picker-destroy', 'parent-destroy', 'library-swap', 'child-id'])
def test_adjacent_translation_modals_revoke_dead_native_returns_without_losing_valid_flow(
        layout_form, monkeypatch, route, boundary):
    form = layout_form
    if route == 'ordinary-export':
        _base.choose_saved(form)
        picker_class, getter = form.wx.DirDialog, 'GetPath'
    elif route == 'source-range':
        form.controls['input_scope'].SetSelection(1)
        form.dialog.on_scope_changed()
        picker_class, getter = form.wx.TextEntryDialog, 'GetValue'
    else:
        picker_class, getter = form.wx.SingleChoiceDialog, 'GetSelection'
    original_show, original_get = picker_class.ShowModal, getattr(picker_class, getter)
    pickers, reads, skips = [], [], []
    def value(picker):
        reads.append(picker)
        return original_get(picker)
    def modal(picker):
        result = original_show(picker)
        pickers.append(picker)
        if boundary in ('picker-destroy', 'parent-destroy'):
            picker.Destroy()
        if boundary == 'parent-destroy':
            form.dialog.Destroy()
        elif boundary == 'library-swap':
            form.frame.library = SimpleNamespace(root=form.app.root)
        elif boundary == 'child-id':
            picker.handlers[form.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
                GetId=lambda: id(picker) + 1, Skip=lambda: skips.append(True)))
        return result
    monkeypatch.setattr(picker_class, getter, value)
    monkeypatch.setattr(picker_class, 'ShowModal', modal)
    _reject_dead_native_calls(form, monkeypatch)
    index = len(form.frame.calls)
    if route == 'saved-job':
        form.dialog.on_jobs()
    elif route == 'ordinary-export':
        form.dialog.on_export()
    else:
        form.dialog.on_sources()
        form.frame.finish(index)
        index += 1
    form.frame.finish(index)
    assert len(pickers) == 1 and pickers[0].destroyed
    if boundary == 'child-id':
        assert reads == pickers and skips == [True]
        assert len(form.frame.calls) == index + 2
        form.frame.finish(index + 1)
        if route == 'saved-job':
            assert form.controls['selected'].GetValue() == form.job['source_text'].replace('\r\n', '\n')
        elif route == 'ordinary-export':
            assert any(name == 'export' for name, _ in form.service.calls)
        else:
            assert 'Original CRLF.' in form.controls['selected'].GetValue()
            assert not form.controls['selected'].editable
    else:
        assert not reads and len(form.frame.calls) == index + 1
    assert not form.layout.exports

"""EPUB route through the same actual document controller and host lifetime."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.ui import translation_layout as view

_spec = importlib.util.spec_from_file_location('epub_ui_document_cases',
    Path(__file__).parents[1] / 'assistant/test_translation_epub_layout.py')
_document = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_document)
bound, ready = _document.bound, _document.ready
_spec = importlib.util.spec_from_file_location('epub_ui_shared_controls',
    Path(__file__).with_name('test_translation_layout.py'))
_controls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_controls)
screen = _controls.screen
click, drain = _controls.click, _controls.drain


def test_epub_workstation_uses_one_reviewed_job_and_preserves_default_no_offline_export(screen):
    h = screen
    h.output = h.output.with_suffix('.epub')
    def plan():
        click('Choose retained EPUB')
        drain()
        h.named['EPUB source language; auto detects'].SetValue('en')
        h.named['EPUB total approved USD budget; local service requires 0'].SetValue('0')
        click('Preview EPUB translation')
        drain()
        before = list(h.bound.state['calls'])
        click('Approve and save EPUB jobs')
        assert not h.queue and not h.bound.service.list()
        assert h.messages[-1][2] & h.wx.NO_DEFAULT
        h.answer = h.wx.YES
        click('Approve and save EPUB jobs')
        drain()
        jobs = h.bound.service.list()
        assert len(jobs) == 1 and jobs[0]['source_partition']['profile'] == _document.worker.PROFILE
        assert h.bound.state['calls'] == before
        click('Choose saved paragraph jobs')
        drain()
        selected = json.loads(h.named['Selected retained EPUB and paragraph jobs'].GetValue())
        assert selected['explicit_saved_jobs'] == [jobs[0]['id']] and selected['partitioned_jobs'] == 1
        click('Run / resume EPUB jobs')
        drain()
        assert h.bound.service.get(jobs[0]['id'])['status'] == 'complete'
        _document.ProfileStore(h.bound.app.root / 'state/providers').remove(h.bound.profile.id)
        click('Export new translated EPUB')
        drain()
        assert h.output.is_file()
        receipt = json.loads(h.named['Formatted translation status and limitations'].GetValue())
        assert receipt['status'] == 'complete' and receipt['format'] == 'epub'
        assert h.bound.source.read_bytes() == h.bound.original
        assert h.focus is h.named['Formatted translation status and limitations']
    h.plan = plan
    view.show_epub_translation(h.frame)


@pytest.mark.parametrize('mode', ['destroy', 'object', 'replaced-root'])
def test_epub_branch_revokes_queued_source_work_and_late_ui_after_scope_change(screen, monkeypatch, mode):
    h, read = screen, []
    monkeypatch.setattr(_document.layout, 'capture_layout', lambda *a, **k: read.append(True))
    def plan():
        click('Choose retained EPUB')
        assert len(h.queue) == 1
        if mode == 'destroy':
            h.dialog.Destroy()
        elif mode == 'object':
            h.frame.library = LibraryApp(h.bound.source.parent / 'different-library')
        else:
            old = h.frame.library.root
            old.rename(old.with_name('prior-library'))
            old.mkdir()
        marks = [len(item.updates) for item in h.widgets]
        drain()
        assert not read and marks == [len(item.updates) for item in h.widgets]
    h.plan = plan
    view.show_epub_translation(h.frame)


def test_current_document_workstation_has_separate_explicit_epub_route(screen, monkeypatch):
    from carr_library.ui.translation_documents import show_translation_documents
    h, opened = screen, []
    monkeypatch.setattr(view, 'show_epub_translation', lambda frame: opened.append(frame))
    def plan():
        before = list(h.bound.state['calls'])
        click('Translate EPUB with layout')
        assert opened == [h.frame] and h.bound.state['calls'] == before and not h.queue
    h.plan = plan
    show_translation_documents(h.frame)


def test_unknown_format_cannot_schedule_an_alternate_parser_or_source():
    with pytest.raises(ValueError, match='supported retained'):
        view.show_translation_layout(object(), document_format='arbitrary-plugin')

"""Rich DOCX through the existing actual controller and controlled widgets."""
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import translation_docx_rich as rich
from carr_library.ui import translation_layout as view

_spec = importlib.util.spec_from_file_location('rich_ui_document_cases',
    Path(__file__).parents[1] / 'assistant/test_translation_rich_docx.py')
_document = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_document)
bound, ready = _document.bound, _document.ready
_spec = importlib.util.spec_from_file_location('rich_ui_shared_controls',
    Path(__file__).with_name('test_translation_layout.py'))
_controls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_controls)
screen = _controls.screen
click, drain = _controls.click, _controls.drain


def test_actual_rich_source_route_reviews_notes_fragments_then_one_job_and_offline_export(screen):
    h = screen
    def plan():
        click('Choose retained DOCX')
        drain()
        inspected = json.loads(h.named['Formatted translation status and limitations'].GetValue())
        assert inspected['schema'] == rich.PROFILE
        assert inspected['canonical_paragraphs'] == 7 and inspected['note_paragraphs'] == 3
        assert inspected['text_fragments'] > inspected['canonical_paragraphs']
        assert inspected['notice'] == rich.NOTICE and 'fixed inline markers' in inspected['notice']
        assert not h.named['Approve and save DOCX jobs'].enabled
        h.named['DOCX source language; auto detects'].SetValue('en')
        h.named['DOCX total approved USD budget; local service requires 0'].SetValue('0')
        click('Preview DOCX translation')
        drain()
        before = list(h.bound.state['calls'])
        click('Approve and save DOCX jobs')
        assert not h.queue and not h.bound.service.list()
        assert h.messages[-1][2] & h.wx.NO_DEFAULT
        h.answer = h.wx.YES
        click('Approve and save DOCX jobs')
        drain()
        jobs = h.bound.service.list()
        assert len(jobs) == 1 and jobs[0]['source_partition']['profile'] == rich.PROFILE
        assert h.bound.state['calls'] == before
        click('Choose saved paragraph jobs')
        drain()
        selected = json.loads(h.named['Selected retained DOCX and paragraph jobs'].GetValue())
        assert selected['explicit_saved_jobs'] == [jobs[0]['id']] and selected['partitioned_jobs'] == 1
        click('Run / resume DOCX jobs')
        drain()
        assert h.bound.service.get(jobs[0]['id'])['status'] == 'complete'
        _document.ProfileStore(h.bound.app.root / 'state/providers').remove(h.bound.profile.id)
        click('Export new translated DOCX')
        drain()
        receipt = json.loads(h.named['Formatted translation status and limitations'].GetValue())
        assert receipt['status'] == 'complete' and receipt['format'] == 'docx'
        assert receipt['notice'] == rich.NOTICE and receipt['quotation_authority'] is False
        assert len(receipt['changed_members']) == 3 and h.output.is_file()
        assert rich.describe(h.output.read_bytes())['media'] == rich.describe(h.bound.original)['media']
        assert h.bound.source.read_bytes() == h.bound.original
        assert h.focus is h.named['Formatted translation status and limitations']
    h.plan = plan
    view.show_translation_layout(h.frame)


@pytest.mark.parametrize('mode', ['destroy', 'object', 'replaced-root'])
def test_rich_view_revokes_queued_capture_and_late_updates_on_host_scope_change(screen, monkeypatch, mode):
    h, inspected = screen, []
    monkeypatch.setattr(view, 'capture_layout', lambda *a, **k: inspected.append(True))
    original = h.frame.library.root
    saved = original.with_name('previous-rich-ui-library')
    before = list(h.bound.state['calls'])
    def plan():
        click('Choose retained DOCX')
        assert len(h.queue) == 1
        if mode == 'destroy':
            h.dialog.Destroy()
        elif mode == 'object':
            h.frame.library = LibraryApp(h.bound.source.parent / 'another-rich-library')
        else:
            original.rename(saved)
            original.mkdir()
        marks = [len(item.updates) for item in h.widgets]
        drain()
        assert not inspected and marks == [len(item.updates) for item in h.widgets]
        assert h.bound.state['calls'] == before
    h.plan = plan
    try:
        view.show_translation_layout(h.frame)
    finally:
        if saved.exists():
            shutil.rmtree(original)
            saved.rename(original)


def test_rich_view_restores_saved_partition_only_after_exact_retained_source_is_selected(screen):
    h = screen
    selected, ids = _document.completed(h.bound)
    before = list(h.bound.state['calls'])
    def plan():
        assert not h.named['Choose saved paragraph jobs'].enabled
        click('Choose retained DOCX')
        drain()
        click('Choose saved paragraph jobs')
        drain()
        value = json.loads(h.named['Selected retained DOCX and paragraph jobs'].GetValue())
        assert value['explicit_saved_jobs'] == list(ids) and value['notice'] == rich.NOTICE
        click('Export new translated DOCX')
        drain()
        assert h.output.is_file() and h.bound.state['calls'] == before
    h.plan = plan
    view.show_translation_layout(h.frame)

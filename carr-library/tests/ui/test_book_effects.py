"""Actual Book dialog closures use the existing typed effect editor and immutable review."""
import importlib.util
from pathlib import Path

import pytest

from carr_library.audio import Effect
from carr_library.audio.envelopes import envelope_effect
from carr_library.ui import book_audio_dialog as ui
from carr_library.ui import effects_dialog

_spec = importlib.util.spec_from_file_location('book_effects_handlers', Path(__file__).with_name('test_book_audio_dialog.py'))
_handlers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handlers)
handlers = _handlers.handlers


def test_explicit_role_editor_invalidates_old_review_then_saves_actual_schema_three_jobs(handlers, monkeypatch):
    e = handlers
    _handlers.preview(e)
    before = e.find('Book split preview and saved job status').value
    chain = (Effect('gain', gain_db=-9),
             envelope_effect('pan_envelope', [(0, -1), (.1, 1)], interpolation='hold'))
    calls = []
    def editor(parent, root, current, **options):
        options['guard']()
        assert root == e.app.root and current == ()
        assert 'gain_envelope' in options['available'] and 'pan_envelope' in options['available']
        assert 'tempo' not in options['available'] and 'width_envelope' not in options['available']
        calls.append(options['sample_rate'])
        return chain
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', editor)
    e.press('Role effects and presets...')
    assert calls == [24000] and not e.find('Prepare saved items').enabled
    assert e.find('Book split preview and saved job status').value != before
    assert not e.production.jobs()
    e.press('Preview split and names')
    e.complete()
    text = e.find('Book split preview and saved job status').value
    assert 'gain' in text and 'pan envelope' in text and 'Reviewed role effects, in order' in text
    e.press('Prepare saved items')
    e.complete()
    saved, = e.service.saved()
    assert saved.schema == 2 and saved.role.effects == chain
    assert all(job['project']['schema'] == 3 and
        job['project']['roles'][0]['effects'] == [effect.to_dict() for effect in chain]
        for job in e.production.jobs())


def test_cancelled_role_editor_preserves_exact_existing_preview(handlers, monkeypatch):
    e = handlers
    _handlers.preview(e)
    before = e.find('Book split preview and saved job status').value
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', lambda *args, **kwargs: None)
    e.press('Role effects and presets...')
    assert e.find('Prepare saved items').enabled
    assert e.find('Book split preview and saved job status').value == before


def test_saved_effect_plan_has_no_implicit_edit_path(handlers, monkeypatch):
    e = handlers
    _handlers.preview(e)
    chain = (Effect('gain', gain_db=-6),)
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', lambda *args, **kwargs: chain)
    e.press('Role effects and presets...')
    e.press('Preview split and names')
    e.complete()
    e.press('Prepare saved items')
    e.complete()
    plan, = e.service.saved()
    e.close()
    e.state['widgets'].clear()
    dialog, close = ui.create_book_audio_dialog(e.frame, service=e.service,
        initial_plan=plan, initial_output=str(e.output), wx_module=e.wx)
    try:
        assert not e.find('Role effects and presets...').enabled
        assert not e.find('Installed local voice').enabled
        assert e.find('Measured duration parts...').enabled
        e.press('Preview split and names')
        e.complete()
        assert 'gain' in e.find('Book split preview and saved job status').value
        assert e.service.load(plan.id).fingerprint == plan.fingerprint
    finally:
        close()
        dialog.Destroy()


def test_nested_role_editor_scope_change_cannot_update_host_or_accept_chain(handlers, monkeypatch):
    e = handlers
    _handlers.preview(e)
    before = e.find('Book split preview and saved job status').value
    def editor(*args, **kwargs):
        e.frame.active = (e.workspace, 'changed-topic')
        return (Effect('gain', gain_db=-6),)
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', editor)
    e.press('Role effects and presets...')
    assert e.find('Book split preview and saved job status').value == before
    assert not e.production.jobs() and not e.state['queue']


def test_measured_route_reuses_parent_service_and_exact_scope_guard(handlers, monkeypatch):
    from carr_library.ui import book_duration_dialog
    e = handlers
    _handlers.preview(e)
    calls = []
    def show(frame, *, service, plan, owner_guard, owner_cancelled):
        assert frame is e.frame and service is e.service
        owner_guard()
        assert not owner_cancelled()
        calls.append(plan)
    monkeypatch.setattr(book_duration_dialog, 'show_book_duration', show)
    e.press('Measured duration parts...')
    assert len(calls) == 1 and not e.state['queue'] and not e.production.jobs()
    assert not e.service._closed.is_set()

"""Actual foreign-import handlers on the reader's existing worker and store."""
from pathlib import Path
import runpy
from threading import get_ident
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import PronunciationRule

_fixture = runpy.run_path(str(Path(__file__).with_name('test_pronunciation_handlers.py')))
view, add_rule, preview = (_fixture[name] for name in ('view', 'add_rule', 'preview'))


def authored(kind, *, replacement='Doctor'):
    if kind == 'dic':
        return ('Dr.=' + replacement + '\n').encode('utf-8')
    if kind == 'rex':
        return (r'Dr\.=' + replacement + '\n').encode('utf-8')
    return ('\ufeffVersion=1\r\nDelimiter=\x03\r\nAuthored fixture\x03Tests\x03No borrowed content\r\n'
            + '\x03'.join(['A' * 32, '18', '1', '1', '0', '0', '0', 'Dr.', replacement, ''])
            + '\r\n').encode('utf-8')


@pytest.mark.parametrize('kind', ['dic', 'rex', 'bxd'])
def test_actual_foreign_import_preview_apply_and_explicit_save(view, tmp_path, kind):
    dialog = view.dialog
    original = view.state.document.to_dict()
    previous = add_rule(view, PronunciationRule('old', 'retained'))
    dialog.on_save(); view.worker.drain()
    saved = dialog.store.path.read_bytes()
    source = tmp_path / ('incoming.' + kind)
    raw = authored(kind)
    source.write_bytes(raw)

    view.picks.append(source)
    dialog.on_import()
    assert dialog.busy and not dialog.import_encoding.IsEnabled()
    view.worker.drain()

    assert len(dialog.rules) == 1 and dialog.preview is None
    assert dialog.import_encoding.IsEnabled()
    assert kind.upper() in dialog.status.GetValue()
    assert dialog.store.path.read_bytes() == saved
    assert dialog.saved_rules == (previous,)
    assert view.state.document.to_dict() == original
    assert source.read_bytes() == raw
    preview(view)
    assert dialog.after.GetValue() == 'Doctor Jones.\r\nCarry 12 kg. Doctor Smith.'
    assert view.state.document.to_dict() == original
    dialog.on_apply()
    assert view.state.document.source_text == original['source_text']
    dialog.on_undo()
    assert view.state.document.text == original['source_text']
    assert dialog.store.path.read_bytes() == saved

    dialog.on_save(); view.worker.drain()
    assert dialog.store.load()[0] == dialog.rules
    assert source.read_bytes() == raw
    assert view.worker.worker_threads and all(
        thread != get_ident() for thread in view.worker.worker_threads)


def test_explicit_encoding_choice_is_captured_before_actual_worker_admission(view, tmp_path):
    dialog = view.dialog
    source = tmp_path / 'ukrainian.dic'
    raw = 'Київ=Кийів\n'.encode('cp1251')
    source.write_bytes(raw)
    view.picks.append(source)
    dialog.on_import(); view.worker.drain()
    assert not dialog.rules and 'encoding' in dialog.status.GetValue()
    assert dialog.store.load() == ((), None)

    dialog.import_encoding.SetSelection(1)
    view.worker.gate.clear()
    view.picks.append(source)
    dialog.on_import()
    assert dialog.busy and not dialog.import_encoding.IsEnabled()
    # A silent programmatic change cannot retarget an already admitted import.
    dialog.import_encoding.SetSelection(2)
    view.worker.gate.set(); view.worker.drain()
    assert len(dialog.rules) == 1 and dialog.rules[0].replacement == 'Кийів'
    assert source.read_bytes() == raw and dialog.store.load() == ((), None)
    assert view.state.document.text == view.state.document.source_text


def test_unknown_bxd_flag_keeps_saved_and_unsaved_rules_without_partial_import(view, tmp_path):
    dialog = view.dialog
    saved_rule = add_rule(view, PronunciationRule('old', 'saved'))
    dialog.on_save(); view.worker.drain()
    saved = dialog.store.path.read_bytes()
    unsaved = add_rule(view, PronunciationRule('review', 'retained'))
    before = view.state.document.to_dict()
    source = tmp_path / 'unknown.bxd'
    raw = authored('bxd').replace(b'\x031\x030\x030\x030\x03Dr.',
                                  b'\x031\x030\x0399\x030\x03Dr.')
    assert raw != authored('bxd')
    source.write_bytes(raw)
    view.picks.append(source)
    dialog.on_import(); view.worker.drain()
    assert dialog.rules == (saved_rule, unsaved)
    assert dialog.saved_rules == (saved_rule,)
    assert 'BXD dictionary line' in dialog.status.GetValue()
    assert dialog.store.path.read_bytes() == saved and source.read_bytes() == raw
    assert view.state.document.to_dict() == before


@pytest.mark.parametrize('when', ['pending', 'completed'])
@pytest.mark.parametrize('how', ['direct', 'parent'])
def test_import_result_cannot_reach_deleted_owner_or_saved_dictionary(view, tmp_path, when, how):
    dialog = view.dialog
    previous = add_rule(view, PronunciationRule('old', 'retained'))
    dialog.on_save(); view.worker.drain()
    saved = dialog.store.path.read_bytes()
    source = tmp_path / 'held.rex'
    raw = authored('rex')
    source.write_bytes(raw)
    before = view.state.document.to_dict()
    if when == 'pending':
        view.worker.gate.clear()
    view.picks.append(source)
    dialog.on_import()
    if when == 'completed':
        view.worker.ready()
        assert len(view.worker.future.result(timeout=5)[0]) == 1
    if how == 'direct':
        dialog.Destroy()
    else:
        # Deliver the actual bound owner-ID destruction path, then make every
        # logical native control refuse access, as the shared lifetime fixture.
        skipped = []
        event = SimpleNamespace(GetId=lambda: dialog._native_id, Skip=lambda: skipped.append(True))
        dialog.on_destroy(event)
        assert skipped == [True]
        _fixture['Widget'].Destroy(dialog)
    assert not dialog.alive and dialog.destroyed and view.cancel.is_set()
    view.worker.gate.set(); view.worker.drain()
    assert dialog.rules == (previous,) and dialog.store.path.read_bytes() == saved
    assert source.read_bytes() == raw and view.state.document.to_dict() == before

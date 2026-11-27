"""Actual Studio edits keep source text and the explicit script boundary."""
from unittest.mock import Mock

import pytest

from carr_library.audio.models import Effect, effect_controls
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
from carr_library.ui import audio_effects


def _dialog(controller):
    dialog = AudioStudioDialog.__new__(AudioStudioDialog)
    dialog.controller = controller
    dialog.dialog = object()
    dialog.script = Mock()
    dialog._capture = Mock()
    dialog._refresh_text = Mock()
    dialog._message = Mock()
    errors = []
    dialog._error = errors.append
    return dialog, errors


@pytest.mark.parametrize('accepted', [False, True], ids=['cancelled', 'confirmed'])
def test_effect_handler_preserves_source_and_saved_project_until_explicit_edit(tmp_path, monkeypatch, accepted):
    original = '[role Alice voice=fixture:voice pan=left]\r\nAlice: Точні слова.\r\n'
    controller = StudioController(private_root=tmp_path / 'library')
    controller.new(original, input_kind='script')
    path = tmp_path / 'audio-project.json'
    controller.save(path)
    before = path.read_bytes()
    controller.result = {'previous': 'accepted recording'}
    dialog, errors = _dialog(controller)

    def choose(parent, roles):
        assert parent is dialog.dialog and 'Alice' in tuple(roles)
        return ('Alice', Effect('pitch', (('semitones', 2),))) if accepted else None

    selector = Mock(side_effect=choose)
    monkeypatch.setattr(audio_effects, 'choose_role_effect', selector)
    dialog.add_effect()
    selector.assert_called_once()
    assert not errors and path.read_bytes() == before
    assert controller.draft.source_text == original
    if not accepted:
        assert controller.script == original and not controller.dirty
        assert controller.result == {'previous': 'accepted recording'}
        assert not controller.draft.can_undo
        dialog._refresh_text.assert_not_called()
        return

    assert controller.dirty and controller.result is None
    assert '[effect Alice pitch semitones=2]\r\n' in controller.script
    assert controller.script.endswith('Alice: Точні слова.\r\n')
    dialog._refresh_text.assert_called_once()
    dialog.script.SetFocus.assert_called_once()
    assert controller.draft.undo() and controller.script == original
    assert controller.draft.redo()
    controller.save()
    reopened = StudioController(private_root=controller.private_root)
    reopened.open(path)
    assert reopened.draft.source_text == original
    alice = next(role for role in reopened.plan().roles if role.name == 'Alice')
    assert alice.pan == -1 and alice.effects == (Effect('pitch', (('semitones', 2.0),)),)


def test_effect_handler_refuses_to_interpret_literal_reading_text(tmp_path, monkeypatch):
    original = '[effect Narrator pitch semitones=12]\nThis is source text to read literally.'
    controller = StudioController(private_root=tmp_path / 'library')
    controller.new(original)
    dialog, errors = _dialog(controller)
    selector = Mock()
    monkeypatch.setattr(audio_effects, 'choose_role_effect', selector)

    dialog.add_effect()

    selector.assert_not_called()
    assert len(errors) == 1 and 'Structured production script' in str(errors[0])
    assert controller.script == original and controller.draft.source_text == original
    assert not controller.draft.can_undo
    dialog._refresh_text.assert_not_called()


def test_native_effect_help_uses_the_same_typed_control_metadata():
    reference = audio_effects.effect_reference()
    controls = effect_controls()
    assert len(controls) == 21
    for kind, fields in controls.items():
        assert kind in reference.splitlines()
        for name, values in fields.items():
            assert name in reference and values['unit'] in reference

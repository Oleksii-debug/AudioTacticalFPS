"""Pure/native-controller boundary; no wx, voice engine or model is needed."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.audio.models import Effect, effect_controls
from carr_library.ui import audio_effects
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController


def fields(kind, **changes):
    values = {name: str(spec['default']) for name, spec in effect_controls()[kind].items()}
    values.update(changes)
    return values


def test_editing_without_changes_keeps_omitted_legacy_controls_and_bypass():
    previous = Effect('compressor', (('ratio', 2.5),), enabled=False)
    result = audio_effects.effect_fields('compressor', fields('compressor', ratio='2.5'), existing=previous)
    assert result == previous
    changed = audio_effects.effect_fields('compressor', fields('compressor', ratio='2.5', mix='.5'),
                                         existing=previous)
    assert changed == Effect('compressor', (('ratio', 2.5), ('mix', .5)), enabled=False)


@pytest.mark.parametrize('changes', [{'ratio': 'nan'}, {'ratio': 'true'}, {'ratio': '1000'},
                                    {'mix': '-.1'}, {'ratio': 2}])
def test_native_fields_reject_untyped_and_out_of_range_parameters(changes):
    with pytest.raises(ValueError):
        audio_effects.effect_fields('compressor', fields('compressor', **changes))


def test_new_effect_fields_keep_displayed_defaults_explicit():
    effect = audio_effects.effect_fields('delay', fields('delay'))
    assert dict(effect.parameters) == {'delay_ms': 250, 'decay': .25, 'mix': .25}


@pytest.fixture
def dialog(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = StudioController(private_root=root)
    controller.new('[effect Narrator gain gain_db=-3]\nExact source.', input_kind='script')
    errors, messages, refreshed = [], [], []
    value = SimpleNamespace(controller=controller, closed=False, dialog=object(),
                            _capture=lambda: None, _error=errors.append,
                            _refresh_text=lambda: refreshed.append(True),
                            _message=lambda message, **kwargs: messages.append(message),
                            script=SimpleNamespace(SetFocus=lambda: None))
    return value, errors, messages, refreshed


def test_native_accept_commits_once_and_cancel_keeps_original_draft(dialog, monkeypatch):
    window, errors, messages, refreshed = dialog
    original = window.controller.script

    def cancel(parent, editor, **kwargs):
        editor.bypass('Narrator', 0)
        return None

    monkeypatch.setattr(audio_effects, 'show_effect_editor', cancel)
    AudioStudioDialog.add_effect(window)
    assert not errors and not refreshed
    assert window.controller.script == original and not window.controller.draft.edits

    def accept(parent, editor, **kwargs):
        editor.bypass('Narrator', 0)
        return editor.script()

    monkeypatch.setattr(audio_effects, 'show_effect_editor', accept)
    AudioStudioDialog.add_effect(window)
    assert not errors and refreshed and messages
    assert window.controller.draft.source_text == original
    assert len(window.controller.draft.edits) == 1
    assert 'enabled=false' in window.controller.script
    assert window.controller.draft.undo() and window.controller.script == original


@pytest.mark.parametrize('change', ['script', 'voice', 'language', 'project', 'draft', 'mode', 'close'])
def test_native_accept_refuses_a_stale_project_or_runtime_selection(dialog, monkeypatch, change):
    window, errors, messages, refreshed = dialog
    controller = window.controller

    def stale(parent, editor, **kwargs):
        editor.bypass('Narrator', 0)
        if change == 'script':
            controller.edit_script(controller.script + '\nNew words.')
        elif change == 'voice':
            controller.configure(voice='another:voice')
        elif change == 'language':
            controller.configure(language='uk')
        elif change == 'project':
            controller.project_id = '1' * 32
        elif change == 'draft':
            from carr_library.reader.narration_text import NarrationDraft
            controller.draft = NarrationDraft(controller.script, source_id='another-source')
        elif change == 'mode':
            controller.input_kind = 'narration'
        else:
            window.closed = True
        return editor.script()

    monkeypatch.setattr(audio_effects, 'show_effect_editor', stale)
    AudioStudioDialog.add_effect(window)
    assert len(errors) == 1 and 'changed' in str(errors[0])
    assert 'enabled=false' not in controller.script
    assert not refreshed and not messages


def test_literal_narration_is_never_interpreted_as_effect_instructions(dialog, monkeypatch):
    window, errors, _, _ = dialog
    window.controller.input_kind = 'narration'
    monkeypatch.setattr(audio_effects, 'show_effect_editor',
                        lambda *args, **kwargs: pytest.fail('Literal text opened the script editor.'))
    AudioStudioDialog.add_effect(window)
    assert len(errors) == 1 and 'Structured' in str(errors[0])
    assert not window.controller.draft.edits


def test_preset_destination_guards_current_project_recovery_and_original(dialog, monkeypatch, tmp_path):
    window, errors, _, _ = dialog
    project = tmp_path / 'project.json'
    source = tmp_path / 'source.txt'
    source.write_bytes(b'Original source')
    window.controller.project_path = project
    window.controller.source_reference = {'kind': 'local-text', 'original_path': str(source)}

    def examine(parent, editor, *, protect_destination, **kwargs):
        for target in (source, project, project.with_name(project.name + '.recovery.json')):
            with pytest.raises(ValueError, match='original source'):
                protect_destination(target)
        protect_destination(tmp_path / 'separate-preset.json')
        return None

    monkeypatch.setattr(audio_effects, 'show_effect_editor', examine)
    AudioStudioDialog.add_effect(window)
    assert not errors and source.read_bytes() == b'Original source'


def test_managed_save_open_and_complete_backup_keep_chain_and_source(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.audio.effect_editing import RoleEffectEditor
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    from carr_library.audio.production import parse_script

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Effects')
    controller = StudioController(private_root=app.root)
    source = '[effect Narrator gain gain_db=-6]\nТочні слова.'
    controller.new(source, input_kind='script')
    editor = RoleEffectEditor(source)
    editor.bypass('Narrator', 0)
    editor.add('Narrator', Effect('limiter', (('ceiling_db', -3),)))
    controller.edit_script(editor.script())
    controller.save_in_library()
    relative = controller.project_path.relative_to(app.root)
    digest = create_backup(app, tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', digest)
    restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    reopened = StudioController(private_root=tmp_path / 'restored').open(tmp_path / 'restored' / relative)
    assert reopened.draft.source_text == source
    assert reopened.script == controller.script
    assert parse_script(reopened.script).roles[0].effects == (
        Effect('gain', (('gain_db', -6.0),), enabled=False),
        Effect('limiter', (('ceiling_db', -3.0),)))

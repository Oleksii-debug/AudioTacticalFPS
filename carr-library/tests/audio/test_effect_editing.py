"""Role edits and preset files exercise actual parser/draft/publication boundaries."""
from dataclasses import replace
import json

import pytest

from carr_library.audio import effect_editing
from carr_library.audio.effect_editing import EffectPreset, RoleEffectEditor, load_preset, save_preset
from carr_library.audio.ffmpeg import effect_filters
from carr_library.audio.models import Effect
from carr_library.audio.production import parse_script
from carr_library.reader.narration_text import NarrationDraft
from carr_library.research.export_file import capture_export_destination


def role(plan, name='Narrator'):
    return next(item for item in plan.roles if item.name == name)


def test_no_change_preserves_every_source_character_and_legacy_parameter_spelling():
    source = '\r\n [effect Narrator highpass frequency=8e1] \r\n# Chapter\r\nNarrator: Ірина — точні слова.\r\n'
    editor = RoleEffectEditor(source)
    assert editor.script() == source
    editor.move('Narrator', 0, 0)
    assert editor.script() == source


def test_reorder_keeps_effect_spelling_and_non_effect_lines_in_original_order():
    source = ('[role Alice pan=left]\r\n'
              ' [effect Alice highpass frequency=8e1] \r\n'
              '[role Alice pan=right]\r\n'
              '[effect Alice gain gain_db=-3]\r\n'
              '[effect Narrator tempo factor=1.1]\r\n'
              '\r\n# Exact chapter\r\nAlice:  Exact words. \r\nNarrator: Unchanged.')
    editor = RoleEffectEditor(source)
    editor.move('Alice', 1, 0)
    changed = editor.script()
    expected = ('[role Alice pan=left]\r\n'
                '[effect Alice gain gain_db=-3]\r\n'
                ' [effect Alice highpass frequency=8e1] \r\n'
                '[role Alice pan=right]\r\n'
                '[effect Narrator tempo factor=1.1]\r\n'
                '\r\n# Exact chapter\r\nAlice:  Exact words. \r\nNarrator: Unchanged.')
    assert changed == expected
    before, after = parse_script(source), parse_script(changed)
    assert before.scenes == after.scenes
    assert role(before, 'Narrator') == role(after, 'Narrator')
    assert role(after, 'Alice').pan == 1
    assert [item.kind for item in role(after, 'Alice').effects] == ['gain', 'highpass']


def test_implicit_unicode_role_is_declared_at_its_first_speech_without_reordering_roles():
    source = "Narrator: Before.\nОлена's voice: Ті самі слова.\nІрина: Після."
    editor = RoleEffectEditor(source, voice='local:test', language='uk')
    editor.add("Олена's voice", Effect('gain', (('gain_db', -4),), enabled=False))
    after = parse_script(editor.script(), voice='local:test', language='uk')
    assert [item.name for item in after.roles] == list(editor.roles)
    assert after.scenes == editor.plan.scenes
    assert role(after, "Олена's voice").voice == 'local:test'
    assert role(after, "Олена's voice").language == 'uk'
    assert editor.script().startswith('Narrator: Before.\n')
    assert editor.script().endswith("Олена's voice: Ті самі слова.\nІрина: Після.")


def test_bypass_edit_remove_and_multi_role_apply_are_one_reversible_draft_change():
    source = ('[role Alice]\n[effect Alice highpass frequency=80]\n'
              '[effect Narrator gain gain_db=-3]\nAlice: Exact source.\nNarrator: After.')
    draft = NarrationDraft(source, source_id='reviewed-source')
    editor = RoleEffectEditor(draft.text)
    editor.bypass('Alice', 0)
    editor.edit('Alice', 0, Effect('highpass', (('frequency', 120),), enabled=False))
    editor.remove('Narrator', 0)
    updated = editor.script()
    assert 'enabled=false' in updated and '[effect Narrator' not in updated
    assert draft.edit_text(updated, reason='Explicit role effect-chain edit')
    assert draft.source_text == source and draft.source_id == 'reviewed-source'
    assert draft.assert_source() is None
    restored = NarrationDraft.from_dict(draft.to_dict())
    assert restored.text == updated and len(restored.edits) == 1
    assert restored.undo() and restored.text == source
    assert restored.redo() and restored.text == updated
    assert role(parse_script(updated), 'Alice').effects == (
        Effect('highpass', (('frequency', 120.0),), enabled=False),)


def test_multiple_role_changes_scan_and_parse_only_once_on_accept(monkeypatch):
    calls = []
    real = effect_editing.parse_script
    monkeypatch.setattr(effect_editing, 'parse_script',
                        lambda *args, **kwargs: (calls.append(1), real(*args, **kwargs))[1])
    source = '\n'.join(f'Role{index}: words' for index in range(63))
    editor = RoleEffectEditor(source)
    assert len(calls) == 1
    assert editor.script() == source and len(calls) == 1
    for name in editor.roles:
        editor.add(name, Effect('gain', enabled=False))
    after = real(editor.script())
    assert len(calls) == 2
    assert len(after.roles) == 64 and all(len(item.effects) == 1 for item in after.roles)
    assert after.scenes == editor.plan.scenes


@pytest.mark.parametrize('operation', ['overflow', 'foreign', 'negative', 'bool', 'bad_move', 'untyped'])
def test_invalid_chain_change_is_atomic(operation):
    source = '[effect Narrator gain]\n' * 16 + 'Narrator: Exact.'
    editor = RoleEffectEditor(source)
    actions = {
        'overflow': lambda: editor.add('Narrator', Effect('gain')),
        'foreign': lambda: editor.add('Other', Effect('gain')),
        'negative': lambda: editor.remove('Narrator', -1),
        'bool': lambda: editor.bypass('Narrator', True),
        'bad_move': lambda: editor.move('Narrator', 0, 16),
        'untyped': lambda: editor.set_chain('Narrator', (object(),)),
    }
    with pytest.raises((TypeError, ValueError)):
        actions[operation]()
    assert editor.script() == source


@pytest.mark.parametrize('option', ['enabled=1', 'enabled=True', 'enabled=', 'enabled=false enabled=true',
                                   'enabled=false frequency=nan', 'enabled=false frequency=-1'])
def test_bypassed_directive_still_requires_exact_boolean_and_valid_parameters(option):
    with pytest.raises(ValueError):
        parse_script(f'[effect Narrator highpass {option}]\nWords.')


def test_bypass_reaches_existing_renderer_without_losing_saved_parameters():
    plan = parse_script('[effect Narrator gain gain_db=-12 enabled=false]\nWords.')
    saved = plan.to_dict()
    assert saved['roles'][0]['effects'][0]['enabled'] is False
    assert effect_filters(plan.roles[0].effects) == []
    editor = RoleEffectEditor(plan.original_script)
    editor.bypass('Narrator', 0)
    enabled = role(parse_script(editor.script())).effects
    assert effect_filters(enabled) == ['volume=-12.00000000dB']


def test_exact_numeric_preset_roundtrip_replaces_only_selected_chain(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    destination = tmp_path / 'Warm voice.json'
    preset = EffectPreset('Теплий голос', (Effect('gain', (('gain_db', -3),)),
                         Effect('compressor', (('ratio', 2.5), ('mix', .4)), enabled=False)))
    approval = capture_export_destination(destination, private_root=private)
    save_preset(destination, preset, private_root=private, approval=approval)
    restored = load_preset(destination)
    assert restored == preset
    editor = RoleEffectEditor('[role Alice]\n[effect Narrator gain]\nAlice: words.')
    before = role(editor.plan)
    editor.apply_preset('Alice', restored)
    assert role(parse_script(editor.script()), 'Alice').effects == preset.effects
    assert role(parse_script(editor.script())) == before
    assert set(json.loads(destination.read_text(encoding='utf-8'))) == {'schema', 'name', 'effects'}


@pytest.mark.parametrize('mutation', ['extra', 'voice', 'enabled', 'duplicate_parameter', 'unknown_effect',
                                     'name', 'too_many', 'not_list', 'nonfinite'])
def test_preset_schema_is_inert_strict_and_bounded(mutation):
    raw = json.loads(EffectPreset('Voice', (Effect('gain'),)).to_text())
    if mutation == 'extra':
        raw['worker'] = 'evil.exe'
    elif mutation == 'voice':
        raw['effects'][0]['voice'] = 'external'
    elif mutation == 'enabled':
        raw['effects'][0]['enabled'] = 'false'
    elif mutation == 'duplicate_parameter':
        raw['effects'][0]['parameters'] = [['gain_db', 1], ['gain_db', 2]]
    elif mutation == 'unknown_effect':
        raw['effects'][0]['kind'] = 'amovie'
    elif mutation == 'name':
        raw['name'] = 'bad\nname'
    elif mutation == 'too_many':
        raw['effects'] *= 17
    elif mutation == 'not_list':
        raw['effects'] = {}
    else:
        raw['effects'][0]['parameters'] = [['gain_db', float('nan')]]
    with pytest.raises((ValueError, TypeError)):
        EffectPreset.from_dict(raw)


@pytest.mark.parametrize('raw', [
    b'{"schema":"carr-role-effect-preset/1","name":"A","name":"B","effects":[]}',
    b'{"schema":"carr-role-effect-preset/1","name":"A","effects":[],"run":true}',
    b' ' * (64 * 1024 + 1),
])
def test_actual_preset_file_reader_rejects_ambiguity_and_oversize(tmp_path, raw):
    path = tmp_path / 'preset.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_preset(path)


def test_preset_publication_keeps_absent_destination_approval(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    path = tmp_path / 'preset.json'
    approval = capture_export_destination(path, private_root=private)
    path.write_bytes(b'another writer')
    with pytest.raises((FileExistsError, ValueError)):
        save_preset(path, EffectPreset('Empty dry chain', ()), private_root=private, approval=approval)
    assert path.read_bytes() == b'another writer'


def test_preset_cannot_be_written_to_private_library_or_without_approval(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    preset = EffectPreset('Dry', ())
    with pytest.raises(ValueError):
        capture_export_destination(private / 'preset.json', private_root=private)
    with pytest.raises(ValueError):
        save_preset(tmp_path / 'preset.json', preset, private_root=private, approval=None)
    assert not list(private.iterdir()) and not (tmp_path / 'preset.json').exists()


def test_empty_preset_is_an_explicit_clear_not_an_automatic_activation():
    source = '[effect Narrator gain gain_db=-3]\nExact.'
    editor = RoleEffectEditor(source)
    preset = EffectPreset('Dry', ())
    assert editor.script() == source
    editor.apply_preset('Narrator', preset)
    assert editor.script() == 'Exact.'

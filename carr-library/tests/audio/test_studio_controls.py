"""Current script/model boundaries for native Studio production controls."""
from dataclasses import replace
import shlex

import pytest

from carr_library.audio.models import Effect, FORMATS, Role
from carr_library.audio.production import parse_script
from carr_library.audio.studio_controls import (
    ScriptProductionEditor, gain_settings, role_fields, role_values,
    saved_voice_options, validate_role_voice, voice_options,
)


def catalogue():
    return voice_options([
        {'id': 'test:voice', 'name': 'Synthetic selected voice', 'languages': ('uk', 'en-US'),
         'controls': ('rate', 'pitch', 'volume'), 'available': True},
        {'id': 'test:limited', 'name': 'Synthetic limited voice', 'languages': ('en-US',),
         'controls': ('volume',), 'available': True},
    ])


@pytest.mark.parametrize('ending', ['\n', '\r\n', '\r'])
def test_no_op_keeps_exact_script_spelling_and_line_endings(ending):
    source = ending.join((
        " [role 'Ірина Павло' voice=test:voice language=uk pan=left] ",
        '[effect Narrator gain enabled=false]',
        '[output format=wav sample_rate=24000 stems=true disk_budget_mb=73]',
        '# Точний розділ', 'Ірина Павло:  Точні слова. ', 'Narrator: Exact words.'))
    editor = ScriptProductionEditor(source)
    for name in editor.roles:
        current = editor.role(name)
        editor.set_role(name, role_fields(current, role_values(current), catalogue()))
    editor.set_output('wav', True)
    assert editor.script() == source
    assert not editor.changed_roles()


def test_role_and_output_edit_preserves_complete_master_timeline_and_original_words():
    source = ("[master mix=.75]\r\n[master-effect gain gain_db=-2]\r\n"
              "[role 'Ірина Павло' voice=test:voice language=uk pan=left]\r\n"
              "[effect 'Ірина Павло' highpass frequency=8e1]\r\n"
              "[output sample_rate=24000 disk_budget_mb=87]\r\n"
              '# Chapter one\r\n[music mood=calm tempo=88]\r\n'
              '[clip asset.wav id=clip-a start=1 track=music]\r\n'
              'Ірина Павло:  Точні слова. \r\n[pause .125]\r\nNarrator: End.')
    editor = ScriptProductionEditor(source)
    before = editor.plan
    original = editor.role('Ірина Павло')
    candidate = replace(original, pan=.25, rate=2, pitch=-1, volume=.6,
                        effects=(*original.effects, Effect('gain', (('gain_db', -6),))))
    editor.set_role(original.name, candidate)
    editor.set_output('m4b', False)
    updated = editor.script()
    after = parse_script(updated)
    expected = replace(before, original_script=updated,
                       roles=tuple(candidate if item.name == candidate.name else item for item in before.roles),
                       output_format='m4b', export_stems=False)
    assert after == expected and after.schema_version == 3
    assert 'Ірина Павло:  Точні слова. \r\n[pause .125]\r\nNarrator: End.' in updated
    assert '[master mix=.75]\r\n[master-effect gain gain_db=-2]\r\n' in updated
    assert '[clip asset.wav id=clip-a start=1 track=music]\r\n' in updated
    assert before.original_script == source and editor.original_script == source


def test_multiple_role_and_output_declarations_keep_prior_order_and_budget():
    source = ('[role Alice voice=test:voice language=uk pan=left]\n'
              '[effect Alice highpass frequency=80]\n'
              '[role Alice rate=1]\n'
              '[output format=mp3 sample_rate=22050 disk_budget_mb=27]\n'
              '[output stems=true]\nAlice: Hello.')
    editor = ScriptProductionEditor(source)
    editor.set_role('Alice', replace(editor.role('Alice'), pan=.5, volume=.33))
    editor.set_output('opus', False)
    updated = editor.script()
    assert updated.startswith('[role Alice voice=test:voice language=uk pan=left]\n'
                              '[effect Alice highpass frequency=80]\n')
    assert '[output format=mp3 sample_rate=22050 disk_budget_mb=27]\n' in updated
    plan = parse_script(updated)
    assert plan.sample_rate == 22050 and plan.disk_budget_bytes == 27 * 1024**2
    assert plan.output_format == 'opus' and plan.export_stems is False
    assert tuple(item.name for item in plan.roles) == ('Narrator', 'Alice')
    assert plan.roles[1].effects == editor.plan.roles[1].effects


@pytest.mark.parametrize('name', ['Narrator', 'Ірина', 'Alex'])
def test_implicit_role_edits_keep_role_order_and_exact_scene_events(name):
    source = '# First\nІрина: Привіт.\n# Second\nAlex: Hello.\nUnprefixed words.'
    editor = ScriptProductionEditor(source, voice='test:voice', language='uk')
    editor.set_role(name, replace(editor.role(name), pan=.5, rate=2))
    updated = editor.script()
    plan = parse_script(updated, voice='test:voice', language='uk')
    assert tuple(role.name for role in plan.roles) == ('Narrator', 'Ірина', 'Alex')
    assert plan.scenes == editor.plan.scenes
    assert plan.roles[editor.roles.index(name)].pan == .5


@pytest.mark.parametrize('tail,enabled', [(False, False), (False, True), (True, False), (True, True)])
def test_final_gain_changes_only_the_owned_tail_and_preserves_earlier_steps(tail, enabled):
    prefix = (Effect('gain', (('gain_db', -9),)), Effect('highpass', (('frequency', 80),)))
    effects = (*prefix, Effect('gain', (), enabled=False)) if tail else prefix
    old = Role('Narrator', voice='test:voice', language='uk', effects=effects)
    values = role_values(old)
    values.update(gain_db='-6', gain_enabled=enabled)
    changed = role_fields(old, values, catalogue())
    assert changed.effects[:-1] == prefix
    assert changed.effects[-1] == Effect('gain', (('gain_db', -6),), enabled=enabled)


def test_unchanged_gain_keeps_omitted_parameters_and_never_inserts_neutral_step():
    for effects in ((), (Effect('gain', (), enabled=False),), (Effect('gain'),)):
        old = Role('Narrator', voice='test:voice', language='uk', effects=effects)
        assert role_fields(old, role_values(old), catalogue()) == old
        assert gain_settings(old)[2] is bool(effects)


@pytest.mark.parametrize('control,value', [('rate', '1'), ('pitch', '1'), ('volume', '90')])
def test_voice_capabilities_refuse_unsupported_nondefault_controls(control, value):
    options = voice_options([{'id': 'test:none', 'name': 'No native controls',
                              'languages': ('uk',), 'controls': (), 'available': True}])
    old = Role('Narrator', voice='test:none', language='uk')
    values = role_values(old)
    values[control] = value
    with pytest.raises(ValueError, match='does not support'):
        role_fields(old, values, options)


@pytest.mark.parametrize('language', ['en', 'en_GB', 'en-GB'])
def test_full_language_code_never_selects_an_unadvertised_regional_route(language):
    old = Role('Narrator', voice='test:limited', language='en-US')
    values = role_values(old)
    values['language'] = language
    with pytest.raises(ValueError, match='exact language'):
        role_fields(old, values, catalogue())


@pytest.mark.parametrize('advertised', [False, True])
def test_unavailable_saved_voice_remains_visible_and_output_only_edit_is_passive(advertised):
    rows = ([{'id': 'missing:exact', 'name': 'Disabled saved voice', 'languages': ('uk',),
              'controls': (), 'available': False, 'reason': 'Disabled explicitly.'}] if advertised else [])
    options = voice_options(rows)
    visible = saved_voice_options(options, 'missing:exact')
    assert len(visible) == 1 and visible[0].id == 'missing:exact' and not visible[0].available
    editor = ScriptProductionEditor('Точні слова.', voice='missing:exact', language='uk')
    original = editor.role('Narrator')
    assert role_fields(original, role_values(original), options) == original
    editor.set_output('flac', False)
    updated = parse_script(editor.script(), voice='missing:exact', language='uk')
    assert updated.roles == editor.plan.roles and not updated.export_stems
    fields = role_values(original)
    fields['pan'] = '.5'
    with pytest.raises(ValueError, match='unavailable'):
        role_fields(original, fields, options)


def test_explicit_voice_change_keeps_nondefault_controls_until_user_resets_them():
    old = Role('Narrator', voice='test:voice', language='uk', rate=3, pitch=2, volume=.5)
    values = role_values(old)
    values.update(voice='test:limited', language='en-US')
    with pytest.raises(ValueError, match='does not support rate'):
        role_fields(old, values, catalogue())
    values['rate'] = '0'
    with pytest.raises(ValueError, match='does not support pitch'):
        role_fields(old, values, catalogue())
    values['pitch'] = '0'
    result = role_fields(old, values, catalogue())
    assert result.voice == 'test:limited' and result.language == 'en-US'
    assert (result.rate, result.pitch, result.volume) == (0, 0, .5)


@pytest.mark.parametrize('field,value', [
    ('pan', 'nan'), ('pan', 'inf'), ('pan', '1.01'), ('pan', True),
    ('rate', '1.5'), ('rate', True), ('rate', '11'), ('pitch', '-11'),
    ('volume', 'NaN'), ('volume', '101'), ('volume', '-1'),
    ('gain_db', 'nan'), ('gain_db', '-121'), ('gain_db', '25'),
    ('gain_enabled', 'false'), ('voice', 'test:\nvoice'), ('language', 'uk\n'),
])
def test_native_numeric_and_identity_fields_are_strict_and_atomic(field, value):
    old = Role('Narrator', voice='test:voice', language='uk')
    values = role_values(old)
    values[field] = value
    with pytest.raises(ValueError):
        role_fields(old, values, catalogue())
    assert old == Role('Narrator', voice='test:voice', language='uk')


@pytest.mark.parametrize('name,role', [('foreign', Role('foreign')), ('Narrator', Role('foreign'))])
def test_role_form_cannot_create_or_rebind_an_existing_role_identity(name, role):
    editor = ScriptProductionEditor('Exact words.')
    with pytest.raises(ValueError):
        editor.set_role(name, role)
    assert editor.script() == 'Exact words.'


@pytest.mark.parametrize('format,stems', [('unknown', True), ('wav', 1), ('wav', 'false')])
def test_invalid_output_selection_preserves_current_plan(format, stems):
    editor = ScriptProductionEditor('Exact words.')
    with pytest.raises(ValueError):
        editor.set_output(format, stems)
    assert editor.script() == 'Exact words.'


@pytest.mark.parametrize('tail', [False, True])
def test_gain_obeys_existing_chain_bound_without_deleting_any_prior_effect(tail):
    effects = (Effect('highpass'),) * 16
    if tail:
        effects = (*effects[:-1], Effect('gain', (('gain_db', -1),)))
    old = Role('Narrator', voice='test:voice', language='uk', effects=effects)
    values = role_values(old)
    values.update(gain_db='-3', gain_enabled=True)
    if tail:
        after = role_fields(old, values, catalogue())
        assert len(after.effects) == 16 and after.effects[:-1] == effects[:-1]
    else:
        with pytest.raises(ValueError, match='effects chain'):
            role_fields(old, values, catalogue())
        assert old.effects == effects


@pytest.mark.parametrize('mutation', ['duplicate', 'availability', 'languages', 'controls', 'identity'])
def test_ambiguous_or_malformed_installed_catalogue_is_refused(mutation):
    row = {'id': 'test:voice', 'name': 'Synthetic voice', 'languages': ('uk',),
           'controls': ('rate',), 'available': True}
    rows = [row]
    if mutation == 'duplicate':
        rows.append(dict(row))
    elif mutation == 'availability':
        row['available'] = 'true'
    elif mutation == 'languages':
        row['languages'] = ('uk', 'uk')
    elif mutation == 'controls':
        row['controls'] = 'rate'
    else:
        row['id'] = 'test:\nvoice'
    with pytest.raises(ValueError):
        voice_options(rows)


@pytest.mark.parametrize('format', FORMATS)
def test_each_existing_format_roundtrips_without_changing_rate_or_source(format):
    source = '[output sample_rate=44100 disk_budget_mb=25]\nТочні слова.'
    editor = ScriptProductionEditor(source)
    editor.set_output(format, False)
    plan = parse_script(editor.script())
    assert (plan.output_format, plan.export_stems, plan.sample_rate,
            plan.disk_budget_bytes) == (format, False, 44100, 25 * 1024**2)
    assert plan.scenes == editor.plan.scenes and plan.roles == editor.plan.roles


def test_quoted_voice_and_role_names_cannot_turn_into_script_directions():
    identifier = "local:Ірина's voice [output format=mp3]"
    source = "[role 'Ірина Павло' voice=test:voice language=uk]\nІрина Павло: Точні слова."
    editor = ScriptProductionEditor(source)
    editor.set_role('Ірина Павло', replace(editor.role('Ірина Павло'), voice=identifier))
    plan = parse_script(editor.script())
    assert plan.roles[1].voice == identifier
    assert plan.output_format == 'wav' and plan.scenes == editor.plan.scenes
    assert shlex.quote(identifier) in editor.script()

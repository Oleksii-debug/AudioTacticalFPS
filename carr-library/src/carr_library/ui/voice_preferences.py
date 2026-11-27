"""Native choices over the reader's existing registry and engine settings.

The owning reader supplies its serial worker, source/library guard and current
controls. This dialog neither discovers providers nor starts audio.
"""
from copy import deepcopy

from ..language_qualification.catalog import canonical_language_tag
from ..runtime_settings import (load_voice_preferences, resolve_voice_preference,
                                save_voice_preferences, validate_voice_preferences)


def show_voice_preferences(parent, *, state_root, voices, preferences, selected,
                           execute, guard, apply_selection):
    import wx

    preferences = validate_voice_preferences(preferences)
    voices = tuple(voices)
    installed = {item.id: item for item in voices}
    dialog = wx.Dialog(parent, title='Local voice preferences', size=(780, 760),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=(
        'Favorites, exact-language defaults and named rate/volume profiles stay in this library. '
        'Unavailable voices are retained without substitution. Use changes only the reader selection; '
        'it does not start audio or change saved projects.')),
        0, wx.EXPAND | wx.ALL, 6)

    def field(label, factory, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
        return control

    language = field('Exact voice language', wx.Choice, choices=[])
    voice = field('Installed and saved voices', wx.Choice, choices=[])
    profile = field('Saved voice profiles', wx.Choice, choices=[])
    profile_name = field('Voice profile name', wx.TextCtrl, value='')
    rate = field('Profile synthesis rate, minus ten to ten', wx.SpinCtrl,
                 min=-10, max=10, initial=selected['rate'])
    volume = field('Profile volume, zero to one hundred', wx.SpinCtrl,
                   min=0, max=100, initial=selected['volume'])
    status = field('Voice preference status', wx.TextCtrl, style=wx.TE_READONLY | wx.TE_MULTILINE)
    actions = wx.GridSizer(cols=3, hgap=4, vgap=4)
    buttons = {}
    for key, label in (
            ('favorite', 'Add or remove favorite'), ('default', 'Save default for language'),
            ('clear_default', 'Clear language default'), ('use_default', 'Use language default'),
            ('save_profile', 'Save voice profile'), ('use_profile', 'Use voice profile'),
            ('delete_profile', 'Delete voice profile'), ('use_voice', 'Use selected voice'),
            ('reload', 'Reload saved preferences')):
        control = wx.Button(dialog, label=label)
        control.SetName(label)
        actions.Add(control, 0, wx.EXPAND)
        buttons[key] = control
    layout.Add(actions, 0, wx.EXPAND | wx.ALL, 6)
    close = wx.Button(dialog, wx.ID_CANCEL, 'Close voice preferences')
    close.SetName('Close voice preferences')
    layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    alive, busy = True, False
    language_tags, voice_ids, profile_names = (), (), ()

    def choice(control, values, explanation):
        index = control.GetSelection()
        if not 0 <= index < len(values):
            raise ValueError(explanation)
        return values[index]

    def visible():
        if not alive:
            return False
        try:
            guard()
        except Exception:
            return False
        return True

    def report(message):
        if visible():
            status.ChangeValue(str(message))

    def refresh():
        nonlocal language_tags, voice_ids, profile_names
        old_language = language_tags[language.GetSelection()] if 0 <= language.GetSelection() < len(language_tags) else selected['language']
        old_voice = voice_ids[voice.GetSelection()] if 0 <= voice.GetSelection() < len(voice_ids) else selected['voice_id']
        old_profile = profile_names[profile.GetSelection()] if 0 <= profile.GetSelection() < len(profile_names) else ''
        tags = set(preferences['defaults']) | {item['language'] for item in preferences['profiles'].values()}
        for item in voices:
            try:
                tags.add(canonical_language_tag(item.language, adapter_underscores=True))
            except ValueError:
                pass  # Availability is not permission to invent a language tag.
        language_tags = tuple(sorted(tags))
        saved = set(preferences['favorites']) | set(preferences['defaults'].values()) | {
            item['voice_id'] for item in preferences['profiles'].values()}
        voice_ids = tuple(installed) + tuple(sorted(saved - installed.keys()))
        profile_names = tuple(preferences['profiles'])
        language.SetItems(list(language_tags))
        voice.SetItems([('Favorite; ' if identity in preferences['favorites'] else '') + (
            f'{installed[identity].name}; {installed[identity].language}; {installed[identity].engine}'
            if identity in installed else 'Unavailable saved voice: ' + identity) for identity in voice_ids])
        profile.SetItems(list(profile_names))
        language.SetSelection(language_tags.index(old_language) if old_language in language_tags else -1)
        voice.SetSelection(voice_ids.index(old_voice) if old_voice in voice_ids else -1)
        profile.SetSelection(profile_names.index(old_profile) if old_profile in profile_names else -1)

    def checked():
        if not alive:
            raise ValueError('The voice preferences window has closed.')
        guard()

    def failed(exc):
        nonlocal busy
        busy = False
        if visible():
            for button in buttons.values():
                button.Enable()
            report(exc)
            status.SetFocus()

    def run(work, done):
        nonlocal busy
        checked()
        if busy:
            raise ValueError('A preferences operation is still running.')
        busy = True
        for button in buttons.values():
            button.Disable()
        def owned_work():
            checked()
            return work()
        def finished(value):
            nonlocal busy
            if not alive:
                return
            try:
                checked()
                busy = False
                for button in buttons.values():
                    button.Enable()
                done(value)
            except Exception as exc:
                failed(exc)
        try:
            if execute(owned_work, finished, on_error=failed) is False:
                failed(RuntimeError('The reader could not start the preferences operation.'))
        except Exception as exc:
            failed(exc)

    def save(candidate):
        expected = deepcopy(preferences)
        candidate = validate_voice_preferences(candidate)
        def saved(value):
            nonlocal preferences
            preferences = value
            refresh()
            report('Voice preferences saved. Existing reader audio and saved projects are unchanged.')
        run(lambda: save_voice_preferences(state_root, candidate, expected=expected, guard=checked), saved)

    def selected_language():
        return choice(language, language_tags, 'Choose an exact declared language.')

    def selected_voice():
        return choice(voice, voice_ids, 'Choose an installed or saved voice.')

    def select(*, default=False, named=False):
        checked()
        selection = resolve_voice_preference(voices, preferences, language=selected_language(),
            voice_id='' if default or named else selected_voice(),
            profile=choice(profile, profile_names, 'Choose a saved voice profile.') if named else '')
        if not named:
            candidate = {'favorites': [], 'defaults': {}, 'profiles': {'Current': {
                **selection, 'rate': rate.GetValue(), 'volume': volume.GetValue()}}}
            selection = resolve_voice_preference(voices, candidate, language=selection['language'], profile='Current')
        apply_selection(selection)
        report('Reader voice selected. No audio has started.')

    def favorite():
        candidate, identity = deepcopy(preferences), selected_voice()
        if identity in candidate['favorites']:
            candidate['favorites'].remove(identity)
        else:
            candidate['favorites'].append(identity)
        save(candidate)

    def default():
        tag, identity = selected_language(), selected_voice()
        resolve_voice_preference(voices, preferences, language=tag, voice_id=identity)
        candidate = deepcopy(preferences)
        candidate['defaults'][tag] = identity
        save(candidate)

    def clear_default():
        candidate = deepcopy(preferences)
        candidate['defaults'].pop(selected_language(), None)
        save(candidate)

    def save_profile():
        tag, identity, name = selected_language(), selected_voice(), profile_name.GetValue()
        candidate = deepcopy(preferences)
        candidate['profiles'][name] = {'voice_id': identity, 'language': tag,
                                      'rate': rate.GetValue(), 'volume': volume.GetValue()}
        candidate = validate_voice_preferences(candidate)
        resolve_voice_preference(voices, candidate, language=tag, profile=name)
        if name in preferences['profiles'] and candidate['profiles'][name] != preferences['profiles'][name]:
            if wx.MessageBox('Replace this saved voice profile with the displayed controls?',
                             'Replace voice profile', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        save(candidate)

    def delete_profile():
        candidate = deepcopy(preferences)
        del candidate['profiles'][choice(profile, profile_names, 'Choose a saved voice profile.')]
        save(candidate)

    def reload():
        def loaded(value):
            nonlocal preferences
            preferences = value
            refresh()
            report('Saved preferences reloaded. No audio has started.')
        run(lambda: load_voice_preferences(state_root), loaded)

    def show_profile(_event=None):
        try:
            checked()
            value = preferences['profiles'][choice(profile, profile_names, 'Choose a saved voice profile.')]
            profile_name.ChangeValue(choice(profile, profile_names, 'Choose a saved voice profile.'))
            language.SetSelection(language_tags.index(value['language']))
            voice.SetSelection(voice_ids.index(value['voice_id']))
            rate.SetValue(value['rate'])
            volume.SetValue(value['volume'])
            report('Saved controls displayed. Use voice profile applies them to this reader without starting audio.')
        except Exception as exc:
            report(exc)

    def invoke(action):
        def handler(_event=None):
            try:
                checked()
                if busy:
                    raise ValueError('A preferences operation is still running.')
                action()
            except Exception as exc:
                report(exc)
                if visible():
                    status.SetFocus()
        return handler

    for key, action in (
            ('favorite', favorite), ('default', default), ('clear_default', clear_default),
            ('use_default', lambda: select(default=True)), ('save_profile', save_profile),
            ('use_profile', lambda: select(named=True)), ('delete_profile', delete_profile),
            ('use_voice', select), ('reload', reload)):
        buttons[key].Bind(wx.EVT_BUTTON, invoke(action))
    profile.Bind(wx.EVT_CHOICE, show_profile)
    close.Bind(wx.EVT_BUTTON, lambda _event: dialog.EndModal(wx.ID_CANCEL))
    try:
        checked()
        refresh()
        report('Choose an exact language and a voice. Favorites and profiles may include unavailable saved voices.')
        voice.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        dialog.Destroy()

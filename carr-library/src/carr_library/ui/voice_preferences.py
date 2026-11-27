"""Native named voice choices; no discovery, synthesis or executable configuration."""
from ..speech.voice_preferences import VoicePreferenceStore, installed_voices, voice_language


def create_voice_preferences(parent, *, library_root, voices, selected_id='', guard=lambda: None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    values = installed_voices(voices)
    store = VoicePreferenceStore(library_root)
    preferences, token = store.load()

    class VoicePreferencesDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Voice preferences by language', size=(800, 620),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.preferences, self.token = preferences, token
            self.selected_id = None
            self.rows = ()
            self.alive = True
            layout = wx.BoxSizer(wx.VERTICAL)
            note = wx.StaticText(self, label='Choose from the voices already loaded in this reader. '
                'Language identifiers come from each installed engine. Saving a default or favorite does not start speech.')
            note.Wrap(740); layout.Add(note, 0, wx.ALL | wx.EXPAND, 6)
            def field(label, factory, **kwargs):
                layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 6)
                control = factory(self, **kwargs); control.SetName(label)
                layout.Add(control, 0, wx.ALL | wx.EXPAND, 6)
                return control
            self.language = field('Declared language or dialect', wx.Choice)
            self.voices = field('Installed voices; favorites are marked', wx.ListBox, size=(-1, 140))
            self.status = field('Voice preference status', wx.TextCtrl, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                size=(-1, 100))
            self.buttons = {}
            for group in (
                (('default', 'Save selected voice as language default', self.on_default),
                 ('clear', 'Remove saved language default', self.on_clear)),
                (('favorite', 'Add or remove selected favorite', self.on_favorite),
                 ('forget', 'Remove unavailable favorites', self.on_forget)),
                (('reload', 'Reload saved preferences', self.on_reload),
                 ('use', 'Use selected voice in reader', self.on_use),
                 ('close', 'Close', self.on_close))):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, handler in group:
                    button = wx.Button(self, label=label); button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, handler); row.Add(button, 0, wx.ALL, 4)
                    self.buttons[key] = button
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout); self.SetEscapeId(self.buttons['close'].GetId())
            self.language.Bind(wx.EVT_CHOICE, self.on_language)
            self.voices.Bind(wx.EVT_LISTBOX, self.on_voice)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.refresh(selected_id)
            self.language.SetFocus()

        def dispose(self):
            self.alive = False
            self.selected_id = None

        def Destroy(self):
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            self.dispose()
            return super().Destroy()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

        def check(self):
            if not self.alive:
                raise ValueError('Voice preferences are closed.')
            guard(); store.guard()
            if not self.alive:
                raise ValueError('Voice preferences are closed.')

        def report(self, message):
            if self.alive:
                self.status.ChangeValue(str(message))

        def selected(self):
            if not self.alive:
                return None
            index = self.voices.GetSelection()
            return self.rows[index] if 0 <= index < len(self.rows) else None

        def key(self):
            if not self.alive:
                return None
            index = self.language.GetSelection()
            return self.languages[index - 1] if 1 <= index <= len(self.languages) else None

        def controls(self):
            if not self.alive:
                return
            selected, key = self.selected(), self.key()
            self.buttons['default'].Enable(selected is not None and key is not None and voice_language(selected) == key)
            self.buttons['clear'].Enable(key in dict(self.preferences.defaults))
            self.buttons['favorite'].Enable(selected is not None)
            self.buttons['use'].Enable(selected is not None)
            self.buttons['forget'].Enable(bool(set(self.preferences.favorites) - {voice.id for voice in values}))

        def refresh(self, preferred_id='', key=None):
            if not self.alive:
                return
            self.languages = tuple(sorted({language for voice in values if (language := voice_language(voice))}
                                         | set(dict(self.preferences.defaults))))
            self.language.Set(['All installed languages', *self.languages])
            self.language.SetSelection(self.languages.index(key) + 1 if key in self.languages else 0)
            self.populate(preferred_id)

        def populate(self, preferred_id=''):
            if not self.alive:
                return
            key = self.key()
            self.rows = tuple(sorted((voice for voice in values if key is None or voice_language(voice) == key),
                key=lambda voice: (voice.id not in self.preferences.favorites, voice.name.casefold(), voice.id)))
            self.voices.Set([('Favorite; ' if voice.id in self.preferences.favorites else '')
                + f'{voice.name}; {voice.language}; {voice.engine}' for voice in self.rows])
            notice = 'Choose an installed voice explicitly. No speech starts in this window.'
            if key is not None and not preferred_id:
                try:
                    preferred = self.preferences.preferred(key, values)
                    if preferred is not None:
                        preferred_id = preferred.id
                        notice = f'Saved default for {key}: {preferred.name}. Use selected voice to apply it to the reader.'
                    else:
                        notice = f'No default is saved for {key}. Select an installed voice.'
                except ValueError as exc:
                    notice = str(exc)
            selected = next((i for i, voice in enumerate(self.rows) if voice.id == preferred_id), -1)
            self.voices.SetSelection(selected)
            missing = len(set(self.preferences.favorites) - {voice.id for voice in values})
            if missing:
                notice += f' {missing} saved favorite(s) are unavailable; they were not substituted.'
            self.report(notice); self.controls()

        def on_language(self, event=None):
            if not self.alive:
                return
            self.populate()

        def on_voice(self, event=None):
            if not self.alive:
                return
            selected = self.selected()
            if selected is not None:
                self.report(f'{selected.name}; {selected.language}; {selected.engine}. '
                    f'Controls: {", ".join(selected.capabilities) or "voice defaults only"}. {selected.timing}')
            self.controls()

        def persist(self, changed, message):
            self.check()
            selected = self.selected(); key = self.key()
            self.preferences, self.token = store.save(changed, expected_token=self.token)
            if not self.alive:
                return
            self.refresh(selected.id if selected is not None else '', key)
            self.report(store.warning or message)

        def on_default(self, event=None):
            if not self.alive:
                return
            try:
                selected = self.selected()
                if selected is None or self.key() is None:
                    raise ValueError('Select one declared language and one installed voice first.')
                self.persist(self.preferences.with_default(self.key(), selected.id, values),
                             f'Saved {selected.name} as the default for {self.key()}. Speech has not started.')
            except Exception as exc: self.report(exc)

        def on_clear(self, event=None):
            if not self.alive:
                return
            try:
                if self.key() is None: raise ValueError('Choose the language default to remove.')
                self.persist(self.preferences.with_default(self.key(), None, values), 'Removed the selected language default.')
            except Exception as exc: self.report(exc)

        def on_favorite(self, event=None):
            if not self.alive:
                return
            try:
                selected = self.selected()
                if selected is None: raise ValueError('Select an installed voice first.')
                enabled = selected.id not in self.preferences.favorites
                self.persist(self.preferences.with_favorite(selected.id, enabled, values),
                             ('Added favorite: ' if enabled else 'Removed favorite: ') + selected.name)
            except Exception as exc: self.report(exc)

        def on_forget(self, event=None):
            if not self.alive:
                return
            try:
                changed = self.preferences
                for identity in set(changed.favorites) - {voice.id for voice in values}:
                    changed = changed.with_favorite(identity, False, values)
                self.persist(changed, 'Removed unavailable favorite identities. Language defaults were preserved.')
            except Exception as exc: self.report(exc)

        def on_reload(self, event=None):
            if not self.alive:
                return
            try:
                self.check()
                self.preferences, self.token = store.load()
                if self.alive:
                    self.refresh()
            except Exception as exc: self.report(exc)

        def on_use(self, event=None):
            if not self.alive:
                return
            try:
                self.check()
                selected = self.selected()
                if selected is None: raise ValueError('Select an available voice before using it.')
                self.selected_id = selected.id
                self.alive = False
                if self:
                    self.EndModal(wx.ID_OK)
            except Exception as exc: self.report(exc)

        def on_close(self, event=None):
            if not self.alive:
                return
            self.alive = False
            if self and self.IsModal(): self.EndModal(wx.ID_CANCEL)

    return VoicePreferencesDialog()


def show_voice_preferences(parent, **kwargs):
    dialog = create_voice_preferences(parent, **kwargs)
    try:
        dialog.ShowModal()
        return dialog.selected_id if dialog else None
    finally:
        dialog.dispose()
        if dialog:
            dialog.Destroy()

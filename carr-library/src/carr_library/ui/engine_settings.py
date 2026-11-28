
"""Native local-engine registration, using the shared supervised UI lifecycle."""
from ..runtime_settings import EngineProfileStore, MUSIC_ENGINES, profile_reference
from .transcription_v43_dialog import TranscriptionSession, _error_text


class EngineSettingsDialog:
    def __init__(self, frame, *, store=None, parent=None):
        import wx
        self.wx, self.frame = wx, frame
        self.store = store or EngineProfileStore(frame.library.root)
        self.reviewed, self.profiles = None, ()
        self.dialog = wx.Dialog(parent or frame, title='Local engine profiles', size=(850, 700),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Local engine registration and current asset verification')
        try:
            self._build()
            self.session = TranscriptionSession(frame, dispatch=wx.CallAfter, changed=self.controls,
                report=self.report, close=self._finish_close)
            self._bind()
            self.refresh()
        except BaseException:
            self.dialog.Destroy()
            raise

    def _build(self):
        wx, window = self.wx, self.dialog
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(window, label=
            'Choose an already installed, reviewed manifest. Registration stays on this computer; '
            'portable projects and backups cannot configure executables.'), 0, wx.EXPAND | wx.ALL, 6)
        self.list = wx.ListBox(window, style=wx.LB_SINGLE, size=(-1, 140))
        self.list.SetName('Saved local engine profiles and enabled state')
        layout.Add(self.list, 0, wx.EXPAND | wx.ALL, 5)
        layout.Add(wx.StaticText(window, label='Local profile &name'), 0, wx.ALL, 4)
        self.name = wx.TextCtrl(window)
        self.name.SetName('Local profile name')
        self.name.SetMaxLength(128)
        layout.Add(self.name, 0, wx.EXPAND | wx.ALL, 4)
        self.details = wx.TextCtrl(window, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.details.SetName('Selected manifest identity, license and registration status')
        layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 5)
        self.status = wx.TextCtrl(window, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 80))
        self.status.SetName('Local engine operation progress and errors')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 5)
        self.buttons = {}
        row = wx.WrapSizer(wx.HORIZONTAL)
        for key, label in (
            ('choose', 'Choose and &review manifest...'), ('register', '&Save reviewed registration'),
            ('review', 'Rechec&k selected profile'), ('toggle', '&Enable or disable selected'),
            ('remove', 'Remove re&gistration'), ('refresh', 'Re&fresh list'), ('close', '&Close')):
            button = wx.Button(window, id=wx.ID_CLOSE if key == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', '').replace('...', ''))
            self.buttons[key] = button
            row.Add(button, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 3)
        window.SetSizer(layout)
        window.SetEscapeId(wx.ID_CLOSE)

    def _bind(self):
        for key, handler in dict(choose=self.choose, register=self.register, review=self.review,
                                 toggle=self.toggle, remove=self.remove, refresh=self.refresh, close=self.close).items():
            self.buttons[key].Bind(self.wx.EVT_BUTTON, handler)
        self.list.Bind(self.wx.EVT_LISTBOX, self.selected)
        self.dialog.Bind(self.wx.EVT_CLOSE, self.close)

    def report(self, value):
        if not self.session.closed:
            self.status.ChangeValue(str(value))
            self.status.SetInsertionPoint(0)

    def controls(self):
        if self.session.closed:
            return
        idle = not self.session.busy and self.session.current()
        selected = 0 <= self.list.GetSelection() < len(self.profiles)
        self.list.Enable(idle)
        self.name.Enable(idle)
        for key in ('choose', 'refresh'):
            self.buttons[key].Enable(idle)
        self.buttons['register'].Enable(idle and self.reviewed is not None)
        for key in ('review', 'toggle', 'remove'):
            self.buttons[key].Enable(idle and selected)

    def refresh(self, event=None):
        if self.session.busy:
            return
        try:
            self.reviewed = None
            snapshot = self.store.snapshot()
            self.profiles = snapshot.profiles
            self.list.SetItems([row.name + ' — ' + row.engine + ' — ' +
                                ('enabled' if row.enabled else 'disabled') for row in self.profiles])
            self.controls()
            self.report('Legacy executable declarations are preserved but inactive. Choose reviewed manifests to register locally.'
                        if snapshot.legacy else
                        str(len(self.profiles)) + ' local registrations. Current files are verified again before use.')
        except Exception as exc:
            self.report(_error_text(exc))
            self.controls()

    def selected(self, event=None):
        self.reviewed = None
        index = self.list.GetSelection()
        if 0 <= index < len(self.profiles):
            profile = self.profiles[index]
            self.name.ChangeValue(profile.name)
            detail = (profile.name + '\nEngine: ' + profile.engine + '\nManifest: ' + profile.manifest_path +
                      '\nSHA-256: ' + profile.manifest_sha256 + '\nState: ' +
                      ('enabled' if profile.enabled else 'disabled'))
            if profile.engine in MUSIC_ENGINES:
                detail += '\nStructured-script reference: model_manifest=' + profile_reference(profile)
            self.details.ChangeValue(detail)
        self.controls()

    def _reviewed(self, reviewed, *, name=None):
        self.reviewed = reviewed
        self.name.ChangeValue((reviewed.manifest.model_id if name is None else name)[:128])
        self.details.ChangeValue(str(reviewed.manifest.path) + '\n' + reviewed.description())
        self.controls()
        self.report('Review the captured identity and license, then Save reviewed registration. No engine has run.')

    def choose(self, event=None):
        if self.session.busy:
            return
        picker = self.wx.FileDialog(self.dialog, 'Choose an installed local-model manifest',
                                    wildcard='JSON manifest (*.json)|*.json',
                                    style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == self.wx.ID_OK:
                path = picker.GetPath()
                self.reviewed = None
                self.session.submit('Verifying installed local engine files',
                    lambda: self.store.inspect(path, cancelled=self.session.stopped), self._reviewed, kind='review')
        except Exception as exc:
            self.report(_error_text(exc))
        finally:
            picker.Destroy()

    def register(self, event=None):
        if self.reviewed is None:
            return
        reviewed, name = self.reviewed, self.name.GetValue()
        def saved(profile):
            self.reviewed = None
            self.refresh()
            self.report('Registered ' + profile.name + '. Reload Studio voices or choose this saved transcription runtime.')
        self.session.submit('Saving reviewed local engine registration',
            lambda: self.store.register(reviewed.manifest.path, name=name,
                expected_sha256=reviewed.manifest.manifest_sha256, cancelled=self.session.stopped),
            saved, kind='save')

    def _selected(self):
        index = self.list.GetSelection()
        if not 0 <= index < len(self.profiles):
            raise ValueError('Choose a saved local engine profile.')
        return self.profiles[index]

    def review(self, event=None):
        try:
            profile = self._selected()
            self.session.submit('Rechecking selected engine assets',
                lambda: self.store.review(profile.id, cancelled=self.session.stopped),
                lambda result: self._reviewed(result, name=profile.name), kind='review')
        except Exception as exc:
            self.report(_error_text(exc))

    def toggle(self, event=None):
        try:
            profile = self._selected()
            self.session.submit('Changing local engine availability',
                lambda: self.store.set_enabled(profile.id, not profile.enabled, cancelled=self.session.stopped),
                lambda _: self.refresh(), kind='configure')
        except Exception as exc:
            self.report(_error_text(exc))

    def remove(self, event=None):
        try:
            profile = self._selected()
            self.session.submit('Removing local engine registration',
                lambda: self.store.remove(profile.id), lambda _: self.refresh(), kind='configure')
        except Exception as exc:
            self.report(_error_text(exc))

    def close(self, event=None):
        finished = self.session.request_close()
        if not finished and event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
            event.Veto()

    def _finish_close(self):
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self):
        try:
            return self.dialog.ShowModal()
        finally:
            self.session.closed = True
            self.session.cancel.set()
            self.dialog.Destroy()


def show_engine_settings(frame, *, parent=None):
    return EngineSettingsDialog(frame, parent=parent).show()


def install_engine_settings_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'engine_settings_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise ValueError('Install the shared Tools menu before local engine settings.')
    action = lambda event=None: show_engine_settings(frame)
    item = menu.Append(wx.ID_ANY, 'Local engine &profiles...')
    frame.Bind(wx.EVT_MENU, action, item)
    register_menu_command(frame, CommandSpec('engines.local', 'Local engine profiles', 'main'), item)
    frame.engine_settings_actions = action
    return action

"""Accessible review of the installed owner's reference-data package API."""
from pathlib import Path
import threading

from ..durable_state import local_path, regular

from ..audio.voice_packages import BACKENDS, VoicePackageManager
from ..audio.voice_lifecycle import VoiceProfileLifecycle, ProtectedVoicePackageReview


class VoicePackageDialog:
    def __init__(self, frame, *, window_parent=None, scope_guard=lambda: None,
                 on_settled=None):
        import wx
        if not callable(scope_guard) or (on_settled is not None and not callable(on_settled)):
            raise TypeError('Use explicit current-library and settlement callbacks.')
        if not callable(getattr(frame, 'run_job', None)):
            raise TypeError('Voice package work requires the existing desktop job owner.')
        self.wx, self.frame, self.library = wx, frame, frame.library
        self.closed = self.destroyed = self.busy = False
        self.review = self.removal_review = self.last_committed_result = None
        self.generation = 0
        self.cancelled = threading.Event()
        self._future = None
        self._scope_guard, self._settlement_observer = scope_guard, on_settled
        self.root = local_path(self.library.root, exists=True)
        info = regular(self.root, directory=True)
        self._root_identity = info.st_dev, info.st_ino
        self._guard()
        self.manager = VoicePackageManager(self.root, scope_guard=self._guard)
        self.lifecycle = VoiceProfileLifecycle(self.root, manager=self.manager,
                                                scope_guard=self._guard)
        self._reference_configuration = None
        self.registered = False
        parent = frame if window_parent is None else window_parent
        self.dialog = wx.Dialog(parent, title='Reference voice packages', size=(920, 720),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._window_id = self.dialog.GetId()
        layout = wx.BoxSizer(wx.VERTICAL)
        self.content = wx.ScrolledWindow(self.dialog, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
        self.content.SetName('Voice package setup, review and permissions')
        self.content.SetMinSize((1, 1))
        self.content.SetScrollRate(16, 16)
        body = wx.BoxSizer(wx.VERTICAL)
        instructions = wx.TextCtrl(self.content, value=(
            'Use the reviewed local owner installation already registered in Local voice engines. '
            'Select its backend and existing relative model/reference folders. Changing these choices clears '
            'previous production-language approvals. Inspect a data-only package or an installed version, '
            'review its source and reference roles, and explicitly confirm your permission to use them. '
            'No model download, reference upload or voice inference occurs here. A language declared by a '
            'package remains a candidate until you explicitly select it for production.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        instructions.SetName('Reference voice setup, rights and language review instructions')
        instructions.SetMinSize((-1, 105))
        body.Add(instructions, 0, wx.EXPAND | wx.ALL, 8)
        form = wx.FlexGridSizer(cols=2, vgap=7, hgap=8)
        form.AddGrowableCol(1)
        self.backend = wx.Choice(self.content, choices=list(BACKENDS))
        self.backend.SetSelection(0)
        self.models = wx.TextCtrl(self.content, value='models')
        self.reference_model = wx.TextCtrl(self.content, value='models/Holos')
        self.references = wx.TextCtrl(self.content, value='models/voices')
        self.profile = wx.TextCtrl(self.content)
        self.version = wx.TextCtrl(self.content)
        self.supported = wx.TextCtrl(self.content)
        self.removal_mode = wx.Choice(self.content, choices=[
            'Deactivate and keep reference files', 'Delete all installed versions and reference files'])
        self.removal_mode.SetSelection(0)
        self.removal_confirmation = wx.TextCtrl(self.content)
        for label, control in (
            ('Installed reference backend', self.backend),
            ('Models folder relative to the reviewed installation', self.models),
            ('Reference model folder relative to the reviewed installation', self.reference_model),
            ('Reference data folder relative to the reviewed installation', self.references),
            ('Installed profile ID for version review', self.profile),
            ('Installed version to review', self.version),
            ('Reviewed production languages, comma separated; blank keeps candidates', self.supported),
            ('Explicit profile removal mode', self.removal_mode),
            ('Exact confirmation from the removal review; initially empty', self.removal_confirmation),
        ):
            control.SetName(label)
            form.Add(wx.StaticText(self.content, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 1, wx.EXPAND)
        body.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        self.rights = wx.CheckBox(self.content,
            label='I have permission to use the exact voice references shown in this review')
        self.rights.SetName(self.rights.GetLabel())
        body.Add(self.rights, 0, wx.EXPAND | wx.ALL, 8)
        self.export_rights = wx.CheckBox(self.content,
            label='I have permission to export the exact reviewed reference files as an unencrypted local package')
        self.export_rights.SetName(self.export_rights.GetLabel())
        body.Add(self.export_rights, 0, wx.EXPAND | wx.ALL, 8)
        self.protected_export_rights = wx.CheckBox(self.content,
            label='I have permission to export the exact reviewed references as a password-protected local package')
        self.protected_export_rights.SetName(self.protected_export_rights.GetLabel())
        body.Add(self.protected_export_rights, 0, wx.EXPAND | wx.ALL, 8)
        commands = wx.WrapSizer(wx.HORIZONTAL)
        self.register_button = wx.Button(self.content, label='Register reviewed local &manifest...')
        self.register_button.SetName('Register a reviewed local voice installation manifest')
        self.reload_button = wx.Button(self.content, label='Reload registered setup')
        self.configure_button = wx.Button(self.content, label='Save &backend and folders')
        self.create_reference_button = wx.Button(self.content, label='Create OmniVoice reference from &recording...')
        self.create_reference_button.SetName('Create a reviewed local reference voice package')
        self.inspect_button = wx.Button(self.content, label='&Inspect data-only package...')
        self.protected_inspect_button = wx.Button(self.content, label='Inspect &protected package...')
        self.protected_inspect_button.SetName('Authenticate and inspect a protected voice package without installing')
        self.versions_button = wx.Button(self.content, label='List installed &versions')
        self.review_button = wx.Button(self.content, label='&Review selected installed version')
        self.apply_button = wx.Button(self.content, label='&Apply reviewed profile')
        self.export_button = wx.Button(self.content, label='&Export reviewed installed version...')
        self.protected_export_button = wx.Button(self.content, label='Export pass&word-protected version...')
        self.protected_export_button.SetName('Export the reviewed installed version as a protected package')
        self.removal_review_button = wx.Button(self.content, label='Review profile &removal')
        self.removal_apply_button = wx.Button(self.content, label='Apply confirmed remo&val')
        self.stop_button = wx.Button(self.dialog, label='&Cancel operation')
        for button, action in (
            (self.register_button, self.register_manifest), (self.reload_button, self.reload),
            (self.configure_button, self.configure), (self.inspect_button, self.inspect_package),
            (self.protected_inspect_button, self.inspect_protected),
            (self.protected_export_button, self.export_protected),
            (self.create_reference_button, self.create_reference),
            (self.versions_button, self.list_versions), (self.review_button, self.review_version),
            (self.apply_button, self.apply), (self.export_button, self.export_version),
            (self.removal_review_button, self.review_removal),
            (self.removal_apply_button, self.remove_profile),
        ):
            button.Bind(wx.EVT_BUTTON, action)
            commands.Add(button, 0, wx.ALL, 4)
        body.Add(commands, 0, wx.EXPAND | wx.ALL, 4)
        self.content.SetSizer(body)
        self.content.FitInside()
        layout.Add(self.content, 1, wx.EXPAND)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY,
                                  size=(-1, 125))
        self.status.SetName('Reference package identity, rights, supported languages and operation status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        self.stop_button.Bind(wx.EVT_BUTTON, self.stop)
        footer.Add(self.stop_button, 0, wx.ALL, 8)
        self.close_button = wx.Button(self.dialog, wx.ID_CLOSE, label='Close')
        self.close_button.Bind(wx.EVT_BUTTON, self.close)
        footer.Add(self.close_button, 0, wx.ALL, 8)
        layout.Add(footer, 0, wx.ALIGN_RIGHT)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        for control in (self.models, self.reference_model, self.references, self.profile, self.version):
            control.Bind(wx.EVT_TEXT, self.invalidate)
        self.backend.Bind(wx.EVT_CHOICE, self.invalidate)
        self.removal_mode.Bind(wx.EVT_CHOICE, self.invalidate)
        self.revision = None
        self.reload()
        self.backend.SetFocus()

    def _owner_guard(self):
        if (getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library):
            raise InterruptedError('Reopen voice packages in the current library.')
        self._scope_guard()
        selected = local_path(self.library.root, exists=True)
        info = regular(selected, directory=True)
        if selected != self.root or (info.st_dev, info.st_ino) != self._root_identity:
            raise InterruptedError('The selected library changed; reopen voice packages.')

    def _guard(self):
        if self.closed or self.destroyed:
            raise InterruptedError('The voice package window is closed.')
        self._owner_guard()

    def _owner_current(self):
        try:
            self._owner_guard()
            return True
        except (OSError, ValueError, RuntimeError, InterruptedError):
            return False

    def _current(self):
        try:
            self._guard()
            return True
        except (OSError, ValueError, RuntimeError, InterruptedError):
            return False

    def _cancelled(self):
        if self.cancelled.is_set() or self.frame.cancel_event.is_set():
            return True
        self._guard()
        return False

    def _load_configuration(self):
        self._guard()
        value = self.manager.store.read()
        if not value['config'].get('dordiai_manifest'):
            return {'revision': value['revision'], 'registered': False}
        return self.manager.configuration(cancelled=self._cancelled)

    def reload(self, event=None):
        return self.start(self._load_configuration, self.loaded)

    def _refresh_parent(self):
        if self._settlement_observer is None or not self._owner_current():
            return
        try:
            self._settlement_observer()
        except Exception as error:
            # Presentation only; this cannot turn a returned commit into failure.
            self.say('The operation settled, but the parent settings view could not refresh: '
                     + str(error), focus=True)

    def register_manifest(self, event=None):
        if not self._current() or self.busy:
            return
        wx = self.wx
        picker = wx.FileDialog(self.dialog, 'Choose the reviewed local voice installation manifest',
            wildcard='JSON (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            self._guard()
            path = Path(picker.GetPath())
            if wx.MessageBox('Register this manifest and its verified local installation for voice package management? '
                'Choose only code and assets you installed and trust. No model is downloaded or synthesized.',
                'Register local voice installation', wx.YES_NO | wx.NO_DEFAULT, self.dialog) != wx.YES:
                return
            self._guard()
        finally:
            picker.Destroy()
        revision = self.revision
        self.invalidate()
        def complete(value):
            self.revision = value['revision']
            self.say('Local manifest registered. Loading its current backend and folders.', focus=True)
            self.wx.CallAfter(lambda: self.reload() if self._current() else None)
        return self.start(lambda: self.manager.register_manifest(path,
            expected_revision=revision, cancelled=self._cancelled), complete, committed=True)

    def say(self, message, *, focus=False):
        if self._current():
            self.status.ChangeValue(message)
            if focus:
                self.status.SetInsertionPoint(0)
                self.status.SetFocus()

    def enable(self):
        if self.closed or self.destroyed:
            return
        available = self._current() and not self.busy
        for control in (self.register_button, self.reload_button):
            control.Enable(available)
        for control in (self.backend, self.models, self.reference_model, self.references,
                        self.profile, self.version, self.supported, self.rights,
                        self.configure_button, self.inspect_button, self.versions_button, self.review_button,
                        self.protected_inspect_button, self.protected_export_rights,
                        self.export_rights, self.removal_mode, self.removal_confirmation, self.removal_review_button):
            control.Enable(available and self.registered)
        values = (self.backend.GetStringSelection(),
                  *(control.GetValue().strip() for control in
                    (self.models, self.reference_model, self.references)))
        reference_ready = (self._reference_configuration == values
                           and values[0] == 'omnivoice')
        self.create_reference_button.Enable(available and self.registered and reference_ready)
        self.apply_button.Enable(available and self.review is not None)
        self.export_button.Enable(available and getattr(self.review, 'operation', None) == 'activate')
        self.protected_export_button.Enable(
            available and getattr(self.review, 'operation', None) == 'activate')
        self.removal_apply_button.Enable(available and self.removal_review is not None)
        self.stop_button.Enable(self.busy)

    def invalidate(self, event=None):
        if self.closed or self.destroyed:
            return
        self.generation += 1
        self.review = self.removal_review = None
        self.rights.SetValue(False)
        self.export_rights.SetValue(False)
        self.protected_export_rights.SetValue(False)
        self.removal_confirmation.ChangeValue('')
        self.enable()
        if event is not None:
            event.Skip()

    def loaded(self, value):
        self.revision = value['revision']
        self.registered = value.get('registered', True)
        self._reference_configuration = None
        if self.registered:
            self.backend.SetStringSelection(value['reference_engine'])
            for name in ('models', 'reference_model', 'references'):
                getattr(self, name).ChangeValue(value[name])
            self._reference_configuration = tuple(value[name] for name in
                ('reference_engine', 'models', 'reference_model', 'references'))
        self.invalidate()
        self.say('Saved setup loaded. Inspect an existing package or installed version before applying it.'
                 if self.registered else
                 'Register an explicitly selected local installation manifest to manage voice packages.',
                 focus=True)

    def start(self, action, complete, *, on_error=None, on_settled=None, committed=False):
        if self.busy or not self._current() or getattr(self.frame, 'working', False):
            if on_settled is not None:
                on_settled()
            return False
        self.busy = True
        self.cancelled = threading.Event()
        generation = self.generation
        self.enable()
        self.say('Verifying the selected local voice setup and reference data.')
        settled = False

        def settle():
            nonlocal settled
            if settled:
                return
            settled = True
            if on_settled is not None:
                on_settled()
            # This can run for a cancelled queued Future after the child closes.
            if self._settlement_observer is not None:
                self.wx.CallAfter(self._refresh_parent)

        def work():
            self._guard()
            if self._cancelled():
                raise InterruptedError('Voice package work was cancelled before admission.')
            return action()

        def failed(error):
            self.busy = False
            from copy import deepcopy
            from ..audio.voice_settings import VoiceManagementCommitted
            receipt = (error.voice_management_receipt
                       if committed and isinstance(error, VoiceManagementCommitted) else None)
            if type(receipt) is dict:
                # The saved state survives child Close and later cleanup failure.
                self.last_committed_result = deepcopy(receipt)
            if not self._current():
                return
            self.review = self.removal_review = None
            self.rights.SetValue(False)
            self.export_rights.SetValue(False)
            self.protected_export_rights.SetValue(False)
            self.removal_confirmation.ChangeValue('')
            self.say(('Voice settings were saved; cleanup needs attention. '
                      if receipt is not None else 'Voice package operation stopped: ') + str(error)
                     + '\nReview and revalidate any completed local change before using it.', focus=True)
            if on_error is not None:
                on_error(error)
            self.enable()

        def finished(value):
            self.busy = False
            if committed:
                self.last_committed_result = value
            if not self._current():
                return
            if not committed and (generation != self.generation or self._cancelled()):
                self.invalidate()
                self.say('The review was cancelled or changed. Prepare a fresh review.', focus=True)
                return
            try:
                complete(value)
            except Exception as error:
                if not committed:
                    raise
                self.say('The operation returned a completed result, but its view could not refresh: '
                         + str(error), focus=True)
            self.enable()

        try:
            future = self.frame.run_job('Managing local voice packages', work, finished, failed)
        except BaseException:
            self.busy = False
            settle()
            if self._current():
                self.enable()
            raise
        if future is None:
            self.busy = False
            settle()
            if self._current():
                self.enable()
            return False
        self._future = future
        def released(completed):
            if self._future is completed:
                self._future = None
            settle()
        future.add_done_callback(released)
        return True

    def create_reference(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        selected = (self.backend.GetStringSelection(),
                    *(control.GetValue().strip() for control in
                      (self.models, self.reference_model, self.references)))
        if selected != self._reference_configuration or selected[0] != 'omnivoice':
            self.say('Save an OmniVoice setup supported by the selected owner installation before creating '
                     'a reference package. The Holos provider does not expose this creation operation.',
                     focus=True)
            return
        from .voice_reference import show_reference_from_packages
        return show_reference_from_packages(self)

    def configure(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        backend = self.backend.GetStringSelection()
        paths = tuple(control.GetValue().strip() for control in
                      (self.models, self.reference_model, self.references))
        revision = self.revision
        self.invalidate()
        def complete(value):
            self.revision = value['revision']
            self._reference_configuration = (backend, *paths)
            self.registered = True
            self.say('Backend and local folders saved. Inspect a package or installed version for a new '
                     'rights and language review.', focus=True)
        self.start(lambda: self.manager.configure(backend, *paths,
            expected_revision=revision, cancelled=self._cancelled), complete, committed=True)

    def inspect_package(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        wx = self.wx
        picker = wx.FileDialog(self.dialog, 'Inspect a local owner voice data package',
            wildcard='Voice data package (*.zip)|*.zip|All files|*.*',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            answer = picker.ShowModal()
            if answer != wx.ID_OK or not self._current():
                return
            self._guard()
            path = Path(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()
        self.invalidate()
        self.start(lambda: self.manager.inspect(path, cancelled=self._cancelled), self.reviewed)

    def list_versions(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        profile = self.profile.GetValue().strip()
        self.invalidate()
        def complete(rows):
            self.say('\n'.join(f"{row['id']}; {row['version']}; {row['name']}; {row['engine']['id']}"
                               for row in rows) or 'No installed versions for that profile.', focus=True)
        self.start(lambda: self.manager.versions(profile, cancelled=self._cancelled), complete)

    def review_version(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        profile, version = self.profile.GetValue().strip(), self.version.GetValue().strip()
        self.invalidate()
        self.start(lambda: self.manager.review_version(profile, version,
            cancelled=self._cancelled), self.reviewed)

    def reviewed(self, review):
        self.removal_review = None
        self.removal_confirmation.ChangeValue('')
        self.export_rights.SetValue(False)
        self.protected_export_rights.SetValue(False)
        self.review = review
        self.revision = review.settings_revision
        self.rights.SetValue(False)
        self.supported.ChangeValue('')
        self.say(review.summary(), focus=True)

    def apply(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        if type(self.review) is ProtectedVoicePackageReview:
            return self.apply_protected(event)
        review = self.review
        approved = self.rights.GetValue()
        languages = tuple(code.strip() for code in self.supported.GetValue().split(',') if code.strip())
        if review is None or approved is not True:
            self.say('Inspect the exact profile and explicitly confirm your permission to use its references.', focus=True)
            return
        self.review = None
        self.rights.SetValue(False)
        def complete(value):
            self.revision = value['revision']
            self.say('Reviewed reference data and language choices are registered. Open a new Audio Studio or '
                     'reader window to select the profile and explicitly generate an audition. '
                     'Model inference, voice similarity and pronunciation have not been qualified by this operation.',
                     focus=True)
        self.start(lambda: self.manager.apply(review, rights_approved=approved,
            supported_languages=languages, cancelled=self._cancelled), complete, committed=True)

    def export_version(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        review, permission = self.review, self.export_rights.GetValue()
        if getattr(review, 'operation', None) != 'activate' or permission is not True:
            self.say('Review the exact installed version and confirm permission to export its reference files.',
                     focus=True)
            return
        wx = self.wx
        picker = wx.FileDialog(self.dialog, 'Export a private unencrypted reference data package',
            wildcard='Owner voice data package (*.dordiai-voice)|*.dordiai-voice|ZIP data package (*.zip)|*.zip',
            style=wx.FD_SAVE)
        try:
            answer = picker.ShowModal()
            if answer != wx.ID_OK or not self._current():
                return
            self._guard()
            destination = Path(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()
        if (not self._current() or self.cancelled.is_set() or self.review is not review
                or self.export_rights.GetValue() is not True):
            self.say('The export review changed while selecting the destination. Review it again.', focus=True)
            return
        self.invalidate()
        def complete(result):
            self.say('Verified reference package saved: ' + result['path']
                     + '\nSHA-256: ' + result['sha256']
                     + '\nThis local data package is not encrypted. Only the reviewed version and its '
                     'reference data are included; engine code and shared models are excluded.'
                     + ('\n' + result['cleanup_warning'] if result.get('cleanup_warning') else ''),
                     focus=True)
        self.start(lambda: self.lifecycle.export(review, destination,
            export_rights_approved=permission, cancelled=self._cancelled), complete, committed=True)


    def _protected_current(self):
        return self._current()

    def _protected_cancelled(self):
        return self._cancelled()

    def _password(self, *, confirm=False):
        """Native operation-only entry; do not touch a destroyed parent or child."""
        if not self._protected_current() or self.busy:
            return None
        wx = self.wx
        window = wx.Dialog(self.dialog, title='Protected voice package password', size=(620, 360))
        identity, live, fields = window.GetId(), [True], []
        def destroyed(event):
            if event.GetId() == identity:
                live[0] = False
            event.Skip()
        window.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            instruction = (
                'Enter at least 12 characters and confirm the exact password. Keep it separately; it cannot be recovered.'
                if confirm else
                'Enter the protected package password for this operation. Inspection does not install a profile; '
                'Apply asks for the password again after a fresh rights review.')
            label = wx.StaticText(window, label=instruction)
            label.Wrap(570)
            layout.Add(label, 0, wx.EXPAND | wx.ALL, 10)
            for caption in (('Password', 'Confirm password') if confirm else ('Password',)):
                layout.Add(wx.StaticText(window, label=caption), 0, wx.LEFT | wx.TOP, 10)
                field = wx.TextCtrl(window, style=wx.TE_PASSWORD)
                field.SetName(caption + ' for this protected voice operation only')
                fields.append(field)
                layout.Add(field, 0, wx.EXPAND | wx.ALL, 10)
            layout.Add(window.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
                       0, wx.ALIGN_RIGHT | wx.ALL, 10)
            window.SetSizer(layout)
            window.SetAffirmativeId(wx.ID_OK)
            window.SetEscapeId(wx.ID_CANCEL)
            fields[0].SetFocus()
            while True:
                answer = window.ShowModal()
                if not live[0] or not self._protected_current() or answer != wx.ID_OK:
                    return None
                password = fields[0].GetValue()
                try:
                    from ..sync.crypto import _password
                    _password(password)
                    if confirm and password != fields[1].GetValue():
                        raise ValueError('The two password entries must match exactly.')
                except ValueError:
                    password = None
                    wx.MessageBox('Use matching entries of at least 12 characters and at most 1024 UTF-8 bytes.',
                                  'Check the password', wx.OK | wx.ICON_INFORMATION, window)
                    if not live[0] or not self._protected_current():
                        return None
                    fields[0].SetFocus()
                    continue
                return password
        finally:
            if live[0]:
                for field in fields:
                    field.ChangeValue('')
                window.Destroy()

    def _start_secret(self, action, complete, password, *, on_error=None, committed=False):
        """Borrow the existing worker; a cancelled queued call also drops its secret."""
        secret = {'value': password}
        def work():
            value = secret.pop('value', None)
            if value is None:
                raise InterruptedError('The protected voice operation was cancelled before starting.')
            try:
                return action(value)
            finally:
                value = None
                secret.clear()
        return self.start(work, complete, on_error=on_error, on_settled=secret.clear,
                          committed=committed)

    def inspect_protected(self, event=None):
        if not self._protected_current() or self.busy:
            return False
        wx = self.wx
        self.cancelled.clear()  # A fresh idle user operation, never an active worker.
        picker = wx.FileDialog(self.dialog, 'Authenticate a protected voice data package',
            wildcard='Protected voice data (*.carr-voice.enc)|*.carr-voice.enc',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            answer = picker.ShowModal()
            if not self._protected_current() or answer != wx.ID_OK:
                return False
            path = Path(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()
        self.invalidate()
        generation = self.generation
        password = self._password()
        if (password is None or not self._protected_current() or self.busy or self.cancelled.is_set()
                or self.generation != generation):
            return False
        def complete(review):
            if self._protected_cancelled() or self.generation != generation:
                self.say('Protected package inspection was cancelled or its input review changed.', focus=True)
                return
            self.reviewed(review)
        return self._start_secret(lambda secret: self.lifecycle.inspect_protected(path,
            passphrase=secret, cancelled=self._protected_cancelled), complete, password)

    def apply_protected(self, event=None):
        if not self._protected_current() or self.busy:
            return False
        review = self.review
        approved = self.rights.GetValue()
        languages = tuple(code.strip() for code in self.supported.GetValue().split(',') if code.strip())
        if type(review) is not ProtectedVoicePackageReview or approved is not True:
            self.say('Authenticate the exact protected package and freshly confirm permission to use its references.',
                     focus=True)
            return False
        generation = self.generation
        password = self._password()
        if (password is None or not self._protected_current() or self.busy or self.cancelled.is_set()
                or self.generation != generation or self.review is not review
                or self.rights.GetValue() is not True
                or languages != tuple(code.strip() for code in self.supported.GetValue().split(',') if code.strip())):
            return False
        self.invalidate()
        def complete(value):
            self.revision = value['revision']
            self.say('The exact protected package was re-authenticated and its reviewed profile applied. '
                'Open a new reader or Studio window for an explicit audition. Inference and voice quality remain unqualified.'
                + ('\n' + value['cleanup_warning'] if value.get('cleanup_warning') else ''), focus=True)
        return self._start_secret(lambda secret: self.lifecycle.apply_protected(review,
            passphrase=secret, rights_approved=approved, supported_languages=languages,
            cancelled=self._protected_cancelled), complete, password, committed=True)

    def export_protected(self, event=None):
        if not self._protected_current() or self.busy:
            return False
        review, permission = self.review, self.protected_export_rights.GetValue()
        if getattr(review, 'operation', None) != 'activate' or permission is not True:
            self.say('Review the installed version and explicitly confirm permission for a protected export.',
                     focus=True)
            return False
        wx = self.wx
        generation = self.generation
        picker = wx.FileDialog(self.dialog, 'Export a password-protected reference data package',
            wildcard='Protected voice data (*.carr-voice.enc)|*.carr-voice.enc', style=wx.FD_SAVE)
        try:
            answer = picker.ShowModal()
            if not self._protected_current() or answer != wx.ID_OK:
                return False
            destination = Path(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()
        if (not self._protected_current() or self.cancelled.is_set() or self.generation != generation
                or self.review is not review or self.protected_export_rights.GetValue() is not True):
            return False
        password = self._password(confirm=True)
        if (password is None or not self._protected_current() or self.busy or self.cancelled.is_set()
                or self.generation != generation or self.review is not review
                or self.protected_export_rights.GetValue() is not True):
            return False
        self.invalidate()
        def complete(result):
            self.say('Authenticated encrypted reference package saved: ' + result['path']
                + '\nCiphertext SHA-256: ' + result['sha256']
                + '\nKeep the password separately. Import authenticates and inspects first; applying a profile '
                'requires a fresh rights and language review. Engine code and shared models are excluded.'
                + ('\n' + result['cleanup_warning'] if result.get('cleanup_warning') else ''), focus=True)
        return self._start_secret(lambda secret: self.lifecycle.export_protected(review, destination,
            passphrase=secret, export_rights_approved=permission,
            cancelled=self._protected_cancelled), complete, password, committed=True)

    def review_removal(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        profile, mode = self.profile.GetValue().strip(), self.removal_mode.GetSelection()
        if mode not in (0, 1):
            self.say('Choose deactivation or deletion explicitly.', focus=True)
            return
        self.invalidate()
        def complete(review):
            self.removal_review = review
            self.revision = review.settings_revision
            self.say(review.summary(), focus=True)
        self.start(lambda: self.lifecycle.review_removal(profile, delete_files=mode == 1,
            cancelled=self._cancelled), complete)

    def remove_profile(self, event=None):
        if not self._current() or self.busy or not self.registered:
            return
        review, confirmation = self.removal_review, self.removal_confirmation.GetValue()
        if review is None or confirmation != review.confirmation:
            self.say('Review the exact profile removal and type its exact displayed confirmation.', focus=True)
            return
        self.invalidate()
        def complete(value):
            self.revision = value['revision']
            self.say('The reviewed profile removal is complete. Production permission is revoked; '
                     'reuse requires a fresh explicit rights and language review. '
                     'Other reviewed profiles and shared models passed the unchanged-file check.',
                     focus=True)
        self.start(lambda: self.lifecycle.remove(review, confirmation=confirmation,
            cancelled=self._cancelled), complete, committed=True)

    def stop(self, event=None):
        self.cancelled.set()
        self.say('Cancellation requested. The bounded local operation will stop at its next checkpoint.')

    def _destroyed(self, event):
        if event.GetId() == self._window_id:
            self.destroyed = True
            self.close(destroying=True)
        event.Skip()

    def close(self, event=None, *, destroying=False):
        if self.closed:
            return
        self.closed = True
        # A queued success may have been adopted before deferred native Destroy.
        # Revoke Python approvals without touching controls in their destructor.
        self.generation += 1
        self.review = self.removal_review = None
        self.cancelled.set()
        if self._future is not None:
            self._future.cancel()
        if destroying:
            return
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)
        else:
            self.dialog.Hide()


def show_voice_packages(frame, *, window_parent=None, scope_guard=lambda: None,
                        on_settled=None):
    controller = VoicePackageDialog(frame, window_parent=window_parent,
        scope_guard=scope_guard, on_settled=on_settled)
    try:
        return controller.dialog.ShowModal()
    finally:
        controller.close()
        if not controller.destroyed:
            controller.dialog.Destroy()

"""Accessible review of the installed owner's reference-data package API."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from ..audio.voice_packages import BACKENDS, VoicePackageManager
from ..audio.voice_lifecycle import VoiceProfileLifecycle


class VoicePackageDialog:
    def __init__(self, parent, root):
        import wx
        self.wx = wx
        self.manager = VoicePackageManager(root)
        self.lifecycle = VoiceProfileLifecycle(root, manager=self.manager)
        self.closed, self.busy, self.review = False, False, None
        self.destroyed = False
        self.removal_review = None
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reference-voice-packages')
        self.dialog = wx.Dialog(parent, title='Reference voice packages', size=(940, 940),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._window_id = self.dialog.GetId()
        layout = wx.BoxSizer(wx.VERTICAL)
        instructions = wx.TextCtrl(self.dialog, value=(
            'Use the reviewed local owner installation already registered in Local voice engines. '
            'Select its backend and existing relative model/reference folders. Changing these choices clears '
            'previous production-language approvals. Inspect a data-only package or an installed version, '
            'review its source and reference roles, and explicitly confirm your permission to use them. '
            'No model download, reference upload or voice inference occurs here. A language declared by a '
            'package remains a candidate until you explicitly select it for production.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY)
        instructions.SetName('Reference voice setup, rights and language review instructions')
        instructions.SetMinSize((-1, 105))
        layout.Add(instructions, 0, wx.EXPAND | wx.ALL, 8)
        form = wx.FlexGridSizer(cols=2, vgap=7, hgap=8)
        form.AddGrowableCol(1)
        self.backend = wx.Choice(self.dialog, choices=list(BACKENDS))
        self.backend.SetSelection(0)
        self.models = wx.TextCtrl(self.dialog, value='models')
        self.reference_model = wx.TextCtrl(self.dialog, value='models/Holos')
        self.references = wx.TextCtrl(self.dialog, value='models/voices')
        self.profile = wx.TextCtrl(self.dialog)
        self.version = wx.TextCtrl(self.dialog)
        self.supported = wx.TextCtrl(self.dialog)
        self.removal_mode = wx.Choice(self.dialog, choices=[
            'Deactivate and keep reference files', 'Delete all installed versions and reference files'])
        self.removal_mode.SetSelection(0)
        self.removal_confirmation = wx.TextCtrl(self.dialog)
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
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        self.rights = wx.CheckBox(self.dialog,
            label='I have permission to use the exact voice references shown in this review')
        self.rights.SetName(self.rights.GetLabel())
        layout.Add(self.rights, 0, wx.EXPAND | wx.ALL, 8)
        self.export_rights = wx.CheckBox(self.dialog,
            label='I have permission to export the exact reviewed reference files as an unencrypted local package')
        self.export_rights.SetName(self.export_rights.GetLabel())
        layout.Add(self.export_rights, 0, wx.EXPAND | wx.ALL, 8)
        commands = wx.WrapSizer(wx.HORIZONTAL)
        self.configure_button = wx.Button(self.dialog, label='Save &backend and folders')
        self.create_reference_button = wx.Button(self.dialog, label='Create from local &recording...')
        self.create_reference_button.SetName('Create a reviewed local reference voice package')
        self.inspect_button = wx.Button(self.dialog, label='&Inspect data-only package...')
        self.versions_button = wx.Button(self.dialog, label='List installed &versions')
        self.review_button = wx.Button(self.dialog, label='&Review selected installed version')
        self.apply_button = wx.Button(self.dialog, label='&Apply reviewed profile')
        self.export_button = wx.Button(self.dialog, label='&Export reviewed installed version...')
        self.removal_review_button = wx.Button(self.dialog, label='Review profile &removal')
        self.removal_apply_button = wx.Button(self.dialog, label='Apply confirmed remo&val')
        self.stop_button = wx.Button(self.dialog, label='&Cancel operation')
        for button, action in (
            (self.configure_button, self.configure), (self.inspect_button, self.inspect_package),
            (self.create_reference_button, self.create_reference),
            (self.versions_button, self.list_versions), (self.review_button, self.review_version),
            (self.apply_button, self.apply), (self.export_button, self.export_version),
            (self.removal_review_button, self.review_removal),
            (self.removal_apply_button, self.remove_profile), (self.stop_button, self.stop),
        ):
            button.Bind(wx.EVT_BUTTON, action)
            commands.Add(button, 0, wx.ALL, 4)
        layout.Add(commands, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Reference package identity, rights, supported languages and operation status')
        layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
        self.close_button = wx.Button(self.dialog, wx.ID_CLOSE, label='Close')
        self.close_button.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.dialog.SetSizer(layout)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        for control in (self.models, self.reference_model, self.references, self.profile, self.version):
            control.Bind(wx.EVT_TEXT, self.invalidate)
        self.backend.Bind(wx.EVT_CHOICE, self.invalidate)
        self.removal_mode.Bind(wx.EVT_CHOICE, self.invalidate)
        self.revision = None
        self.start(lambda: self.manager.configuration(cancelled=self.cancelled.is_set), self.loaded)
        self.backend.SetFocus()

    def say(self, message, *, focus=False):
        if not self.closed:
            self.status.ChangeValue(message)
            if focus:
                self.status.SetInsertionPoint(0)
                self.status.SetFocus()

    def enable(self):
        for control in (self.backend, self.models, self.reference_model, self.references,
                        self.profile, self.version, self.supported, self.rights,
                        self.configure_button, self.inspect_button, self.versions_button, self.review_button,
                        self.create_reference_button,
                        self.export_rights, self.removal_mode, self.removal_confirmation, self.removal_review_button):
            control.Enable(not self.busy)
        self.apply_button.Enable(not self.busy and self.review is not None)
        self.export_button.Enable(not self.busy and getattr(self.review, 'operation', None) == 'activate')
        self.removal_apply_button.Enable(not self.busy and self.removal_review is not None)
        self.stop_button.Enable(self.busy)

    def invalidate(self, event=None):
        self.review = self.removal_review = None
        self.rights.SetValue(False)
        self.export_rights.SetValue(False)
        self.removal_confirmation.ChangeValue('')
        self.enable()
        if event is not None:
            event.Skip()

    def loaded(self, value):
        self.revision = value['revision']
        self.backend.SetStringSelection(value['reference_engine'])
        for name in ('models', 'reference_model', 'references'):
            getattr(self, name).ChangeValue(value[name])
        self.invalidate()
        self.say('Saved runtime loaded. Inspect an existing package or installed version before applying it.', focus=True)

    def start(self, action, complete, *, on_error=None):
        if self.busy or self.closed:
            return
        self.busy = True
        self.cancelled.clear()
        self.enable()
        self.say('Verifying local runtime and reference data. No inference or network request.')
        future = self.executor.submit(action)
        def done(result):
            if self.closed:
                return
            def finish():
                if self.closed:
                    return
                self.busy = False
                try:
                    complete(result.result())
                except Exception as error:
                    self.review = self.removal_review = None
                    self.rights.SetValue(False)
                    self.export_rights.SetValue(False)
                    self.removal_confirmation.ChangeValue('')
                    self.say('Reference voice operation did not complete: ' + str(error)
                             + '\nIf data was installed before interruption, review and revalidate the local registration before use.',
                             focus=True)
                    if on_error is not None:
                        on_error(error)
                self.enable()
            try:
                self.wx.CallAfter(finish)
            except RuntimeError:
                if not self.closed:
                    raise
        future.add_done_callback(done)

    def create_reference(self, event=None):
        if self.closed or self.busy:
            return
        from .voice_reference import show_reference_from_packages
        return show_reference_from_packages(self)

    def configure(self, event=None):
        backend = self.backend.GetStringSelection()
        paths = tuple(control.GetValue().strip() for control in
                      (self.models, self.reference_model, self.references))
        revision = self.revision
        self.invalidate()
        def work():
            self.manager.configure(backend, *paths, expected_revision=revision,
                                   cancelled=self.cancelled.is_set)
            return self.manager.configuration(cancelled=self.cancelled.is_set)
        self.start(work, self.loaded)

    def inspect_package(self, event=None):
        wx = self.wx
        picker = wx.FileDialog(self.dialog, 'Inspect a local owner voice data package',
            wildcard='Voice data package (*.zip)|*.zip|All files|*.*',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            answer = picker.ShowModal()
            if self.closed or answer != wx.ID_OK:
                return
            path = Path(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()
        self.invalidate()
        self.start(lambda: self.manager.inspect(path, cancelled=self.cancelled.is_set), self.reviewed)

    def list_versions(self, event=None):
        profile = self.profile.GetValue().strip()
        self.invalidate()
        def complete(rows):
            self.say('\n'.join(f"{row['id']}; {row['version']}; {row['name']}; {row['engine']['id']}"
                               for row in rows) or 'No installed versions for that profile.', focus=True)
        self.start(lambda: self.manager.versions(profile, cancelled=self.cancelled.is_set), complete)

    def review_version(self, event=None):
        profile, version = self.profile.GetValue().strip(), self.version.GetValue().strip()
        self.invalidate()
        self.start(lambda: self.manager.review_version(profile, version,
            cancelled=self.cancelled.is_set), self.reviewed)

    def reviewed(self, review):
        self.removal_review = None
        self.removal_confirmation.ChangeValue('')
        self.export_rights.SetValue(False)
        self.review = review
        self.revision = review.settings_revision
        self.rights.SetValue(False)
        self.supported.ChangeValue('')
        self.say(review.summary(), focus=True)

    def apply(self, event=None):
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
            supported_languages=languages, cancelled=self.cancelled.is_set), complete)

    def export_version(self, event=None):
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
            if self.closed or answer != wx.ID_OK:
                return
            destination = Path(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()
        if (self.closed or self.cancelled.is_set() or self.review is not review
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
            export_rights_approved=permission, cancelled=self.cancelled.is_set), complete)

    def review_removal(self, event=None):
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
            cancelled=self.cancelled.is_set), complete)

    def remove_profile(self, event=None):
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
            cancelled=self.cancelled.is_set), complete)

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
        self.cancelled.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if destroying:
            return
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CLOSE)
        else:
            self.dialog.Hide()


def show_voice_packages(parent, root):
    controller = VoicePackageDialog(parent, root)
    try:
        return controller.dialog.ShowModal()
    finally:
        controller.close()
        if not controller.destroyed:
            controller.dialog.Destroy()


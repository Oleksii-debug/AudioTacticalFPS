"""Accessible review of the installed owner's reference-data package API."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from ..audio.voice_packages import BACKENDS, VoicePackageManager


class VoicePackageDialog:
    def __init__(self, parent, root):
        import wx
        self.wx = wx
        self.manager = VoicePackageManager(root)
        self.closed, self.busy, self.review = False, False, None
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reference-voice-packages')
        self.dialog = wx.Dialog(parent, title='Reference voice packages', size=(900, 820),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
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
        for label, control in (
            ('Installed reference backend', self.backend),
            ('Models folder relative to the reviewed installation', self.models),
            ('Reference model folder relative to the reviewed installation', self.reference_model),
            ('Reference data folder relative to the reviewed installation', self.references),
            ('Installed profile ID for version review', self.profile),
            ('Installed version to review', self.version),
            ('Reviewed production languages, comma separated; blank keeps candidates', self.supported),
        ):
            control.SetName(label)
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        self.rights = wx.CheckBox(self.dialog,
            label='I have permission to use the exact voice references shown in this review')
        self.rights.SetName(self.rights.GetLabel())
        layout.Add(self.rights, 0, wx.EXPAND | wx.ALL, 8)
        commands = wx.WrapSizer(wx.HORIZONTAL)
        self.configure_button = wx.Button(self.dialog, label='Save &backend and folders')
        self.inspect_button = wx.Button(self.dialog, label='&Inspect data-only package...')
        self.versions_button = wx.Button(self.dialog, label='List installed &versions')
        self.review_button = wx.Button(self.dialog, label='&Review selected installed version')
        self.apply_button = wx.Button(self.dialog, label='&Apply reviewed profile')
        self.stop_button = wx.Button(self.dialog, label='&Cancel operation')
        for button, action in (
            (self.configure_button, self.configure), (self.inspect_button, self.inspect_package),
            (self.versions_button, self.list_versions), (self.review_button, self.review_version),
            (self.apply_button, self.apply), (self.stop_button, self.stop),
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
        for control in (self.models, self.reference_model, self.references, self.profile, self.version):
            control.Bind(wx.EVT_TEXT, self.invalidate)
        self.backend.Bind(wx.EVT_CHOICE, self.invalidate)
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
                        self.configure_button, self.inspect_button, self.versions_button, self.review_button):
            control.Enable(not self.busy)
        self.apply_button.Enable(not self.busy and self.review is not None)
        self.stop_button.Enable(self.busy)

    def invalidate(self, event=None):
        self.review = None
        self.rights.SetValue(False)
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

    def start(self, action, complete):
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
                    self.review = None
                    self.rights.SetValue(False)
                    self.say('Reference voice operation did not complete: ' + str(error)
                             + '\nIf data was installed before interruption, review and revalidate the local registration before use.',
                             focus=True)
                self.enable()
            try:
                self.wx.CallAfter(finish)
            except RuntimeError:
                if not self.closed:
                    raise
        future.add_done_callback(done)

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
            if picker.ShowModal() != wx.ID_OK:
                return
            path = Path(picker.GetPath())
        finally:
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

    def stop(self, event=None):
        self.cancelled.set()
        self.say('Cancellation requested. The bounded local operation will stop at its next checkpoint.')

    def close(self, event=None):
        if self.closed:
            return
        self.closed = True
        self.cancelled.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
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
        controller.dialog.Destroy()


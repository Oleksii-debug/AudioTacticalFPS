"""Explicit remote transcription controls borrowing the existing host worker."""
from concurrent.futures import Future
import json
from pathlib import Path
from threading import Event

from ..assistant.transcription_provider import RETENTION, MODEL
from ..integrations.transcription_remote import approve, RemoteTranscriptionCommitted


class RemoteTranscriptionActions:
    def __init__(self, frame, service, *, is_current=lambda: True, on_job=None):
        self.frame, self.service = frame, service
        self.library, self.root = frame.library, Path(frame.library.root)
        self.workspace = service.workspace
        self.remote = service.remote_transcription()
        self.is_current, self.on_job = is_current, on_job
        self.cancel = Event()
        self.alive, self.busy = True, False
        self.reviewed, self.review_inputs = None, None
        self.last_result, self.last_job = None, None
        self.future = None
        self.profiles = self.remote.profiles()

    def current(self):
        active = getattr(self.frame, 'active', None)
        if (not self.alive or not self.is_current() or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library or Path(self.library.root) != self.root
                or active is None or active[0] != self.workspace):
            return False
        try:
            self.remote.guard()
        except (ValueError, OSError, RuntimeError):
            return False
        return True

    def stopped(self):
        return (self.cancel.is_set() or self.frame.cancel_event.is_set() or not self.current())

    def inputs(self):
        index = self.provider.GetSelection()
        if not 0 <= index < len(self.profiles):
            raise ValueError('Choose an enabled dedicated ElevenLabs transcription profile in AI and API connections.')
        return (self.profiles[index].id, self.file.GetValue(), self.language.GetValue().strip(),
                self.cost.GetValue().strip())

    def report(self, message):
        if self.current():
            self.status.ChangeValue(str(message))
            self.status.SetFocus()

    def controls(self):
        if not self.current():
            return
        for control in self.mutable:
            control.Enable(not self.busy)
        self.review_button.Enable(not self.busy and bool(self.profiles))
        self.send_button.Enable(not self.busy and self.reviewed is not None and self.consent.GetValue())
        self.stop_button.Enable(self.busy)
        self.close_button.Enable(True)

    def invalidate(self, event=None):
        if self.busy:
            return
        self.reviewed, self.review_inputs = None, None
        self.consent.SetValue(False)
        self.preview.ChangeValue('Review one selected recording before any transfer.')
        self.controls()

    def _remember(self, future):
        # No GUI access: the retained outcome survives a closed child window.
        try:
            value = future.result()
            if isinstance(value, dict) and value.get('status') == 'completed':
                self.last_result = value
        except RemoteTranscriptionCommitted as error:
            self.last_result = error.result
        except BaseException as error:
            saved = getattr(error, 'remote_transcription_result', None)
            if isinstance(saved, dict):
                self.last_result = saved

    def begin(self, title, operation, complete):
        if self.busy or not self.current() or getattr(self.frame, 'working', False):
            return False
        self.busy = True
        self.cancel.clear()
        self.controls()

        def work():
            if self.stopped():
                raise InterruptedError('The queued remote transcription view is no longer current.')
            self.remote.guard(self.stopped)
            return operation()

        def done(value):
            self.busy = False
            if isinstance(value, dict) and value.get('status') == 'completed':
                self.last_result = value
            if self.current():
                self.controls()
                try:
                    complete(value)
                except (ValueError, OSError, RuntimeError) as error:
                    self.report(error)

        def failed(error):
            self.busy = False
            if isinstance(error, RemoteTranscriptionCommitted):
                self.last_result = error.result
            if self.current():
                self.controls()
                self.report(error)
                if self.last_result is not None and self.on_job is not None:
                    self.on_job(self.last_result['id'])

        try:
            future = self.frame.run_job(title, work, done=done, failed=failed)
            if future is None or future is False:
                self.busy = False
                self.controls()
                return False
            if not isinstance(future, Future):
                raise TypeError('Remote transcription requires the existing host Future.')
            self.future = future
            future.add_done_callback(self._remember)
            return True
        except BaseException:
            self.busy = False
            self.controls()
            raise

    def review(self, event=None):
        try:
            selected = self.inputs()
            self.invalidate()
            profile, source, language, cost = selected
            def accepted(value):
                if self.inputs() != selected:
                    return
                self.reviewed = json.loads(json.dumps(value, ensure_ascii=False))
                self.review_inputs = selected
                self.preview.ChangeValue(
                    f"Recording: {value['source_path']}\nBytes: {value['audio']['size_bytes']}\n"
                    f"SHA-256: {value['audio']['sha256']}\n"
                    f"Service: {value['profile']['endpoint']}/speech-to-text\n"
                    f"Model: {MODEL}; language: {value['language']}\n"
                    f"One request; local budget reservation: {value['max_cost']} USD.\n"
                    f"This reserves your local budget; it is not a price quote or a provider-enforced billing cap. {RETENTION}\n"
                    "A sent request may be charged even after cancellation. No automatic retry is made.")
                self.controls()
            return self.begin('Review selected remote transcription',
                lambda: self.remote.review(source, profile_id=profile, language=language,
                    max_cost=cost, cancelled=self.stopped), accepted)
        except (ValueError, OSError, RuntimeError) as error:
            self.report(error)
            return False

    def send(self, event=None):
        try:
            if (self.busy or not self.current() or self.reviewed is None
                    or self.review_inputs != self.inputs() or self.consent.GetValue() is not True):
                raise PermissionError('Review this recording and explicitly accept its transfer and cost first.')
            reviewed = json.loads(json.dumps(self.reviewed, ensure_ascii=False))
            selected = self.review_inputs
            if not self.confirm(reviewed):
                return False
            if (not self.current() or self.inputs() != selected or self.review_inputs != selected
                    or self.reviewed != reviewed or self.consent.GetValue() is not True):
                raise PermissionError('The recording, profile or approval changed during confirmation.')
            authorization = approve(reviewed, consent=True)
            self.reviewed, self.review_inputs = None, None
            self.consent.SetValue(False)
            def operation():
                self.last_job = self.remote.submit(authorization, cancelled=self.stopped)
                return self.remote.run(self.last_job, authorization, cancelled=self.stopped)
            def completed(value):
                self.report('Remote transcript saved. Review its text and provider timestamps before use. '
                            'The original recording remains unchanged.')
                if self.on_job is not None:
                    self.on_job(value['id'])
            return self.begin('Transcribe explicitly approved recording', operation, completed)
        except (ValueError, OSError, RuntimeError) as error:
            self.report(error)
            return False

    def close(self, event=None, *, force=False):
        if not self.alive:
            return True
        if self.busy and not force:
            self.cancel.set()
            self.report('Stopping the remote operation. Its worker must finish cleanup before this window closes.')
            if event is not None and getattr(event, 'CanVeto', lambda: False)():
                event.Veto()
            return False
        self.alive = False
        self.cancel.set()
        self.reviewed, self.review_inputs = None, None
        self.finish_close()
        return True


def build_remote_transcription_dialog(frame, service, *, window_parent=None,
                                      is_current=lambda: True, on_job=None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    parent = frame if window_parent is None else window_parent
    actions = RemoteTranscriptionActions(frame, service, is_current=is_current, on_job=on_job)
    if not actions.current() or getattr(frame, 'working', False):
        raise PermissionError('Reopen transcription after the current library operation finishes.')
    dialog = wx.Dialog(parent, title='Remote transcription — explicit selected recording',
                       size=(780, 740), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    panel = wx.ScrolledWindow(dialog, style=wx.VSCROLL)
    panel.SetScrollRate(0, 12)
    body = wx.BoxSizer(wx.VERTICAL)
    panel.SetSizer(body)
    layout.Add(panel, 1, wx.EXPAND)
    def field(label, factory=wx.TextCtrl, **options):
        body.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
        control = factory(panel, **options)
        control.SetName(label.replace('&', ''))
        body.Add(control, 0, wx.EXPAND | wx.ALL, 4)
        return control
    actions.provider = field('&Provider account', wx.Choice,
        choices=[profile.name for profile in actions.profiles])
    if actions.profiles:
        actions.provider.SetSelection(0)
    actions.file = field('Selected &recording', style=wx.TE_READONLY)
    choose = wx.Button(panel, label='Choose recording...')
    choose.SetName('Choose recording')
    body.Add(choose, 0, wx.ALL, 4)
    actions.language = field('&Language code or auto', value='auto')
    actions.cost = field('Local budget reservation in &USD', value='')
    actions.preview = field('Exact recording and transfer &review',
        style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 210),
        value='No recording is sent by opening this window. LOCAL ONLY remains the default elsewhere. '
              'For this optional action, configure one enabled ElevenLabs profile with model scribe_v2 '
              'and capability speech.transcribe in AI and API connections.')
    actions.consent = wx.CheckBox(panel, label=
        'I approve this selected recording, spending allowance and normal provider retention.')
    actions.consent.SetName('Explicit selected recording transfer consent')
    actions.consent.SetValue(False)
    body.Add(actions.consent, 0, wx.ALL, 6)
    actions.status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 75),
        value='Remote recognition is optional. Local Whisper settings and jobs are unchanged.')
    actions.status.SetName('Remote transcription status')
    layout.Add(actions.status, 0, wx.EXPAND | wx.ALL, 4)
    row = wx.BoxSizer(wx.HORIZONTAL)
    for key, label in (('review_button', '&Review'), ('send_button', '&Send approved recording'),
                       ('stop_button', 'S&top'), ('close_button', '&Close')):
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        setattr(actions, key, button)
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.ALIGN_RIGHT)
    dialog.SetSizer(layout)
    actions.mutable = [actions.provider, actions.file, choose, actions.language, actions.cost, actions.consent]
    def confirm(value):
        return wx.MessageBox(
            f"Send this selected recording to ElevenLabs now?\n{value['source_path']}\n"
            f"Local budget reservation: {value['max_cost']} USD; actual provider billing is not capped by this setting.\n{RETENTION}",
            'Send selected recording', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, dialog) == wx.YES
    actions.confirm = confirm
    def finish_close():
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)
        else:
            dialog.Hide()
    actions.finish_close = finish_close
    def pick(event=None):
        try:
            if not actions.current() or actions.busy:
                return
            with wx.FileDialog(dialog, 'Choose one recording for explicit remote transcription',
                wildcard='Audio and video|*.wav;*.flac;*.mp3;*.m4a;*.ogg;*.opus;*.aac;*.wma;*.webm;*.mp4;*.mkv',
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                selected = chooser.GetPath()
            if not actions.current():
                return
            actions.file.ChangeValue(selected)
            actions.invalidate()
        except (ValueError, OSError, RuntimeError) as error:
            actions.report(error)
    choose.Bind(wx.EVT_BUTTON, pick)
    actions.review_button.Bind(wx.EVT_BUTTON, actions.review)
    actions.send_button.Bind(wx.EVT_BUTTON, actions.send)
    actions.stop_button.Bind(wx.EVT_BUTTON, lambda event: actions.cancel.set())
    actions.close_button.Bind(wx.EVT_BUTTON, actions.close)
    actions.provider.Bind(wx.EVT_CHOICE, actions.invalidate)
    actions.language.Bind(wx.EVT_TEXT, actions.invalidate)
    actions.cost.Bind(wx.EVT_TEXT, actions.invalidate)
    actions.consent.Bind(wx.EVT_CHECKBOX, lambda event: actions.controls())
    dialog.Bind(wx.EVT_CLOSE, actions.close)
    actions.controls()
    dialog.remote_transcription_actions = actions
    return dialog


def show_remote_transcription(frame, service, **options):
    dialog = build_remote_transcription_dialog(frame, service, **options)
    try:
        dialog.ShowModal()
    finally:
        dialog.remote_transcription_actions.close(force=True)
        dialog.Destroy()
    return dialog.remote_transcription_actions.last_result

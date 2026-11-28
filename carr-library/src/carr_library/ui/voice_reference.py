"""Explicit local reference review borrowing the existing voice-dialog worker."""
from pathlib import Path
import threading

from ..audio.voice_reference import (ReferenceDescription, ReferencePackageResult,
                                     ReferenceReview, VoiceReferenceService)


class VoiceReferenceDialog:
    def __init__(self, parent, *, service, submit, alive, busy, cancelled, guard,
                 saved=None, record=None):
        import wx
        if not isinstance(service, VoiceReferenceService) or any(not callable(value)
                for value in (submit, alive, busy, cancelled, guard)):
            raise TypeError('Reference review requires the current service and borrowed dialog lifecycle.')
        if any(value is not None and not callable(value) for value in (saved, record)):
            raise TypeError('Reference handoff callbacks must be explicit callables.')
        self.wx, self.service, self.submit = wx, service, submit
        self.parent_alive, self.parent_busy, self.parent_cancelled, self.guard = alive, busy, cancelled, guard
        self.saved, self.record = saved, record
        self.closed = self.destroyed = self.busy = False
        self.generation, self.review, self.result = 0, None, None
        self.cancelled = threading.Event()
        self.dialog = wx.Dialog(parent, title='Review local voice reference', size=(940, 880),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.voice_reference = self
        self._window_id = self.dialog.GetId()
        layout = wx.BoxSizer(wx.VERTICAL)
        notice = wx.TextCtrl(self.dialog, value=(
            'Select a local standard 16-bit mono/stereo PCM WAV and its exact transcript. '
            'This route uses only the reviewed configured OmniVoice audio/text reference adapter. '
            'Review measurements and listen to the recording in the existing audio tools. '
            'No automatic speaker, consent or transcript validation, training or synthesis occurs. '
            'Create writes a LOCAL UNENCRYPTED data-only package; it does not install or approve a voice.'),
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 105))
        notice.SetName('Local reference scope, permission, privacy and quality limits')
        layout.Add(notice, 0, wx.EXPAND | wx.ALL, 6)
        form = wx.FlexGridSizer(cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1)
        self.fields = {}
        for name, label, initial in (
                ('source', 'Selected local PCM reference recording', ''),
                ('profile_id', 'New reference profile ID', ''),
                ('version', 'New reference profile version', '1.0.0'),
                ('name', 'Reference profile display name', ''),
                ('language', 'Explicit reference language code', ''),
                ('provenance_source', 'Reference provenance and origin', ''),
                ('rights_notice', 'Reference permission and license notice', '')):
            field = wx.TextCtrl(self.dialog, value=initial)
            field.SetName(label)
            field.Bind(wx.EVT_TEXT, self.changed)
            self.fields[name] = field
            form.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(field, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 6)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.choose_button = self._button(row, 'Choose PCM reference...', self.choose_source)
        self.record_button = None
        if record is not None:
            self.record_button = self._button(row, 'Open existing recorder...', self.open_recorder)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 3)
        layout.Add(wx.StaticText(self.dialog, label='Exact text spoken in this recording'), 0, wx.LEFT | wx.TOP, 6)
        self.transcript = wx.TextCtrl(self.dialog, style=wx.TE_MULTILINE, size=(-1, 100))
        self.transcript.SetName('Exact reference transcript')
        self.transcript.Bind(wx.EVT_TEXT, self.changed)
        layout.Add(self.transcript, 0, wx.EXPAND | wx.ALL, 6)
        self.consent = wx.CheckBox(self.dialog,
            label='I have permission to use and locally export this exact recording and transcript as a voice reference')
        self.consent.SetName(self.consent.GetLabel())
        self.consent.SetValue(False)
        self.consent.Bind(wx.EVT_CHECKBOX, self.consent_changed)
        layout.Add(self.consent, 0, wx.EXPAND | wx.ALL, 6)
        self.quality = wx.CheckBox(self.dialog,
            label='I reviewed this recording, its measurements and the matching transcript')
        self.quality.SetName(self.quality.GetLabel())
        self.quality.SetValue(False)
        self.quality.Bind(wx.EVT_CHECKBOX, lambda event: self.sync())
        layout.Add(self.quality, 0, wx.EXPAND | wx.ALL, 6)
        self.status = wx.TextCtrl(self.dialog, value='No reference has been reviewed.',
            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 230))
        self.status.SetName('Reference measurements, exact identity and local package status')
        layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 6)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.review_button = self._button(row, 'Review reference input', self.review_input)
        self.create_button = self._button(row, 'Create local reference package...', self.create_package)
        self.cancel_button = self._button(row, 'Cancel reference work', self.cancel)
        self.close_button = self._button(row, 'Close reference review', self.close, identifier=wx.ID_CANCEL)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.sync()

    def _button(self, row, label, handler, *, identifier=None):
        button = self.wx.Button(self.dialog, self.wx.ID_ANY if identifier is None else identifier, label=label)
        button.SetName(label)
        button.Bind(self.wx.EVT_BUTTON, handler)
        row.Add(button, 0, self.wx.ALL, 4)
        return button

    def _alive(self):
        return not self.closed and not self.destroyed and self.parent_alive()

    def say(self, message, *, focus=False):
        if self._alive():
            self.status.ChangeValue(str(message))
            if focus:
                self.status.SetInsertionPoint(0)
                self.status.SetFocus()

    def sync(self):
        if not self._alive() or not hasattr(self, 'review_button'):
            return
        idle = not self.busy and not self.parent_busy()
        for control in (*self.fields.values(), self.transcript, self.choose_button,
                        self.consent, self.review_button):
            control.Enable(idle)
        if self.record_button is not None:
            self.record_button.Enable(idle)
        self.quality.Enable(idle and self.review is not None)
        self.create_button.Enable(idle and self.review is not None
                                  and self.consent.GetValue() and self.quality.GetValue())
        self.cancel_button.Enable(self.busy)

    def _values(self):
        values = {name: field.GetValue() for name, field in self.fields.items()}
        return (values.pop('source'), ReferenceDescription(
            **values, reference_text=self.transcript.GetValue()))

    def changed(self, event=None):
        if self._alive():
            self.generation += 1
            self.review = None
            self.consent.SetValue(False)
            self.quality.SetValue(False)
            self.sync()
        if event is not None:
            event.Skip()

    def consent_changed(self, event=None):
        if self._alive():
            if not self.consent.GetValue():
                self.generation += 1
                self.review = None
                self.quality.SetValue(False)
            self.sync()

    def _start(self, work, complete, *, committed=False):
        if not self._alive() or self.busy or self.parent_busy():
            return False
        try:
            self.guard()
        except Exception as exc:
            self.say(str(exc), focus=True)
            return False
        self.generation += 1
        generation = self.generation
        self.cancelled = threading.Event()
        stop = self.cancelled
        self.busy = True
        self.sync()

        def cancelled():
            return stop.is_set() or self.parent_cancelled() or self.closed or not self.parent_alive()

        def failed(error):
            self.busy = False
            if not self._alive():
                return
            self.review = None
            self.quality.SetValue(False)
            self.say('Reference work did not complete. ' + str(error), focus=True)
            self.sync()

        def finished(value):
            self.busy = False
            if not self._alive():
                return
            # A successful writer receipt remains a completed output even if
            # cancellation arrives before the GUI handles this completion.
            if committed and type(value) is ReferencePackageResult:
                complete(value)
                self.sync()
                return
            if cancelled() or generation != self.generation:
                self.review = None
                self.quality.SetValue(False)
                self.say('Reference review cancelled or changed; prepare a fresh review.')
                self.sync()
                return
            try:
                self.guard()
                complete(value)
            except Exception as exc:
                failed(exc)
            self.sync()

        try:
            submitted = self.submit(lambda: work(cancelled), finished, on_error=failed)
        except Exception as exc:
            failed(exc)
            return False
        if submitted is False:
            self.busy = False
            self.sync()
            return False
        return True

    def review_input(self, event=None):
        if not self._alive() or self.busy or self.parent_busy():
            return False
        try:
            selected = self._values()
            consent = self.consent.GetValue()
            if consent is not True:
                raise ValueError('Confirm permission for this selected recording and transcript first.')
        except Exception as exc:
            self.say(str(exc), focus=True)
            return False
        self.review = None
        self.quality.SetValue(False)
        self.say('Reviewing captured local PCM and the configured owner adapter. No inference.')
        def reviewed(value):
            if type(value) is not ReferenceReview or self._values() != selected:
                raise ValueError('Reference fields changed; prepare a new review.')
            self.fields['source'].ChangeValue(value.source)
            self.review = value
            self.say(value.summary(), focus=True)
        return self._start(lambda stopped: self.service.review(
            *selected, consent=consent, cancelled=stopped), reviewed)

    def choose_source(self, event=None):
        if not self._alive() or self.busy or self.parent_busy():
            return
        try:
            self.guard()
            with self.wx.FileDialog(self.dialog, 'Select local PCM reference',
                    wildcard='PCM WAV (*.wav)|*.wav',
                    style=self.wx.FD_OPEN | self.wx.FD_FILE_MUST_EXIST) as picker:
                answer = picker.ShowModal()
                if not self._alive():
                    return
                self.guard()
                if answer != self.wx.ID_OK:
                    return
                selected = picker.GetPath()
            self.fields['source'].ChangeValue(selected)
            self.changed()
        except Exception as exc:
            self.say(str(exc), focus=True)

    def open_recorder(self, event=None):
        if not self._alive() or self.busy or self.parent_busy() or self.record is None:
            return
        generation = self.generation
        def selected(path):
            if not self._alive() or self.busy or generation != self.generation:
                return
            try:
                self.guard()
                if not isinstance(path, (str, Path)):
                    raise ValueError('Select a verified saved recording from the existing recorder.')
                self.fields['source'].ChangeValue(str(path))
                self.changed()
                self.say('The saved recording is selected. Enter its exact transcript and review permission.')
            except Exception as exc:
                self.say(str(exc), focus=True)
        try:
            self.guard()
            # The adapter opens the existing recorder with this native parent;
            # only its verified explicit selection may call back with a path.
            self.record(self.dialog, selected)
        except Exception as exc:
            self.say(str(exc), focus=True)

    def create_package(self, event=None):
        if not self._alive() or self.busy or self.parent_busy():
            return False
        try:
            self.guard()
            review = self.review
            if (type(review) is not ReferenceReview or not self.consent.GetValue()
                    or not self.quality.GetValue()
                    or self._values() != (review.source, review.description)):
                raise ValueError('Review these exact inputs and confirm permission and the recording/text check.')
            if self.cancelled.is_set() or self.parent_cancelled():
                raise InterruptedError('The current reference review was cancelled; review it again.')
            picker_generation = self.generation
            with self.wx.FileDialog(self.dialog, 'Save local unencrypted reference package',
                    defaultFile=review.description.profile_id + '-' + review.description.version + '.dordiai-voice',
                    wildcard='Local voice data (*.dordiai-voice)|*.dordiai-voice|ZIP (*.zip)|*.zip',
                    style=self.wx.FD_SAVE) as picker:
                answer = picker.ShowModal()
                if not self._alive():
                    return False
                self.guard()
                if answer != self.wx.ID_OK:
                    return False
                destination = picker.GetPath()
            if self.cancelled.is_set() or self.parent_cancelled():
                raise InterruptedError('Reference package creation was cancelled while choosing its destination.')
            if (picker_generation != self.generation or self.review is not review or not self.consent.GetValue()
                    or not self.quality.GetValue()
                    or self._values() != (review.source, review.description)):
                raise ValueError('The reference review changed while choosing its destination.')
        except Exception as exc:
            self.say(str(exc), focus=True)
            return False

        def saved(value):
            self.result = value
            self.review = None
            self.quality.SetValue(False)
            message = ('Saved local UNENCRYPTED reference package: ' + value.path
                + '\nOriginal recording and transcript were not modified. No training or inference ran. '
                + 'Use Inspect data-only package for the separate installation, rights and language review.')
            if value.cleanup_warning:
                message += '\n' + value.cleanup_warning
            self.say(message, focus=True)
            try:
                self.guard()
                if self.saved is not None:
                    self.saved(Path(value.path))
            except Exception:
                # The output is already committed. A stale parent scope or
                # failed handoff cannot relabel the saved package as a failure.
                self.say(message + '\nThe parent handoff is unavailable; select this saved file explicitly.', focus=True)
        self.say('Creating only the reviewed local data package. Existing destinations will not be overwritten.')
        return self._start(lambda stopped: self.service.create(review, destination,
            consent=True, quality_reviewed=True, cancelled=stopped), saved, committed=True)

    def cancel(self, event=None):
        if self._alive():
            self.cancelled.set()
            self.generation += 1
            self.review = None
            self.quality.SetValue(False)
            self.say('Cancelling reference work. A package already published remains a completed local file.')
            self.sync()

    def _destroyed(self, event):
        if event.GetId() == self._window_id:
            self.destroyed = self.closed = True
            self.cancelled.set()
            self.generation += 1
            self.review = None
        event.Skip()

    def close(self, event=None):
        if self.destroyed:
            return
        self.closed = True
        self.cancelled.set()
        self.generation += 1
        self.review = None
        self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self.fields['source'].SetFocus()
            self.dialog.ShowModal()
            return self.result
        finally:
            self.closed = True
            self.cancelled.set()
            if not self.destroyed:
                self.dialog.Destroy()


def show_voice_reference(parent, *, service, submit, alive, busy, cancelled, guard,
                         saved=None, record=None):
    return VoiceReferenceDialog(parent, service=service, submit=submit, alive=alive,
        busy=busy, cancelled=cancelled, guard=guard, saved=saved, record=record).show()


def show_reference_from_packages(controller):
    """One button adapter borrowing the current package dialog and optional recorder."""
    if controller.closed or controller.busy:
        return None
    root = controller.manager.store.projects.root
    window = controller.dialog.GetParent()
    frame = library = None
    seen = set()
    for _ in range(32):
        if window is None or id(window) in seen:
            break
        seen.add(id(window))
        candidate = getattr(window, 'library', None)
        if (candidate is not None and getattr(candidate, 'root', None) == root
                and callable(getattr(window, 'run_job', None))):
            frame, library = window, candidate
            break
        parent = getattr(window, 'GetParent', None)
        window = parent() if callable(parent) else None

    def alive():
        return not controller.closed and not (frame is not None and getattr(frame, '_closing', False))

    def guard():
        # Called from the borrowed worker too: captured Python authority only,
        # with no native window calls or filesystem reads here.
        if not alive() or controller.manager.store.projects.root != root:
            raise InterruptedError('The reference package dialog is no longer current.')
        if frame is not None and (frame.library is not library or library.root != root):
            raise ValueError('The current library changed; reopen reference review.')

    def submit(work, complete, *, on_error):
        guard()
        if controller.busy:
            return False
        controller.start(work, complete, on_error=on_error)
        return True

    record = None
    if frame is not None:
        def record(reference_dialog, selected):
            guard()
            from .recording_dialog import show_recording
            def verified(value):
                guard()
                if type(value) is not dict or not isinstance(value.get('path'), (str, Path)):
                    raise ValueError('The existing recorder did not return a verified saved recording.')
                selected(value['path'])
            return show_recording(frame, on_selected=verified, window_parent=reference_dialog,
                selection_label='Use verified recording as reference')

    service = VoiceReferenceService(root, scope_guard=guard)
    result = show_voice_reference(controller.dialog, service=service, submit=submit,
        alive=alive, busy=lambda: controller.busy, cancelled=controller.cancelled.is_set,
        guard=guard, record=record)
    if type(result) is ReferencePackageResult and alive() and not controller.busy:
        try:
            guard()
            controller.invalidate()
            # Inspection is read-only. The parent's normal review still clears
            # rights and language approvals; no package is installed here.
            controller.start(lambda: controller.manager.inspect(
                result.path, cancelled=controller.cancelled.is_set), controller.reviewed)
        except Exception:
            # The saved package remains completed even when its parent closes
            # or changes before this optional explicit review handoff.
            return result
    return result

"""Explicit preview/apply/undo using the current reading controller and executor."""
from dataclasses import dataclass, field
from pathlib import Path

from ..reader.narration import NarrationProject
from ..reader.speech_normalization import (
    SpeechOptions, SpeechPreview, prepare_speech, validate_preview,
)


@dataclass(frozen=True)
class SpeechSelection:
    identity: int
    revision: int
    text: str
    selection: tuple[int, int]
    owner: object = field(compare=False, repr=False)
    project: NarrationProject = field(compare=False, repr=False)

    def __post_init__(self):
        if (type(self.identity) is not int or self.identity != id(self.owner)
                or not isinstance(self.project, NarrationProject)
                or self.revision != id(self.project) or type(self.text) is not str
                or type(self.selection) is not tuple or len(self.selection) != 2
                or any(type(value) is not int for value in self.selection)
                or not 0 <= self.selection[0] <= self.selection[1] <= len(self.text)):
            raise ValueError('Capture the exact current reading copy and selection.')


@dataclass(frozen=True)
class SpeechProposal:
    snapshot: SpeechSelection
    bounds: tuple[int, int]
    result: SpeechPreview


def capture_speech(controller, selection):
    from .narration_editor import NarrationController
    if not isinstance(controller, NarrationController):
        raise TypeError('Use the existing reading-copy controller.')
    controller.guard()
    return SpeechSelection(id(controller), id(controller.project), controller.pending_text,
                           selection, controller, controller.project)


def _bounds(snapshot, bounds):
    if (not isinstance(snapshot, SpeechSelection) or type(bounds) is not tuple or len(bounds) != 2
            or any(type(value) is not int for value in bounds)
            or not 0 <= bounds[0] < bounds[1] <= len(snapshot.text)):
        raise ValueError('Choose a nonempty current reading-copy passage.')


def prepare_selection(snapshot, bounds, options, *, cancelled=lambda: False, timeout=2.0):
    _bounds(snapshot, bounds)
    return SpeechProposal(snapshot, bounds, prepare_speech(
        snapshot.text[bounds[0]:bounds[1]], options, cancelled=cancelled, timeout=timeout))


def _current(controller, snapshot, current):
    from .narration_editor import NarrationController
    if (not isinstance(controller, NarrationController) or not isinstance(snapshot, SpeechSelection)
            or current != snapshot or controller is not snapshot.owner
            or controller.project is not snapshot.project or controller.pending_text != snapshot.text):
        raise ValueError('The reading copy, revision or selection changed. Preview speech preparation again.')
    controller.guard()


def apply_speech(controller, proposal, current):
    if not isinstance(proposal, SpeechProposal):
        raise ValueError('Preview the speech preparation before applying it.')
    _current(controller, proposal.snapshot, current)
    _bounds(current, proposal.bounds)
    preview = validate_preview(proposal.result)
    start, end = proposal.bounds
    if current.text[start:end] != preview.source:
        raise ValueError('The selected passage no longer matches this speech preview.')
    if not preview.changed:
        return False
    controller.apply(current.text[:start] + preview.text + current.text[end:],
                     label='Reviewed speech preparation — ' + preview.options.locale)
    return True


def undo_speech(controller, applied, current):
    _current(controller, applied, current)
    controller.undo()


def reading_frame(parent, root):
    # Reuse the existing bounded parent/executor lookup, without importing any
    # optional dictionary backend or creating another worker/session framework.
    from .spellcheck_dialog import spelling_frame
    try:
        return spelling_frame(parent, root)
    except ValueError as exc:
        raise ValueError('Open speech preparation from the current library reading window.') from exc


def create_speech_preparation(parent, *, capture, apply, undo, guard, run, cancel,
                              stopped=lambda: False, request_close=None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class SpeechPreparationDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Prepare reading copy for speech', size=(980, 850),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id, self._destroy_requested = self.GetId(), False
            self.alive, self.busy, self.close_requested = True, False, False
            self.proposal = self.applied = self.reviewed_form = None
            try:
                self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
                guard()
                initial = capture()
                panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
                panel.SetScrollRate(0, 12)
                box = wx.BoxSizer(wx.VERTICAL)
                notice = wx.StaticText(panel, label=
                    'Prepare only this separate reading copy. Verbatim preserves exact text. '
                    'Choose Ukrainian or English explicitly for finite speech rules; unknown or ambiguous forms '
                    'stay literal. Read the proposed text before Apply. Foreign names use the existing '
                    'reviewed pronunciation dictionary. No voice or recording is started.')
                notice.Wrap(900)
                box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)

                def field(label, factory=wx.Choice, **kwargs):
                    box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                    control = factory(panel, **kwargs)
                    control.SetName(label.replace('&', ''))
                    box.Add(control, 0, wx.ALL | wx.EXPAND, 3)
                    return control

                self.mode = field('Preparation &mode', choices=['Verbatim — exact source', 'Explicit speech rules'])
                self.mode.SetSelection(0)
                self.locale = field('Speech &language', choices=['Choose a locale', 'Ukrainian — uk-UA', 'English — en-US'])
                self.locale.SetSelection(0)
                self.scope = field('Text &scope', choices=['Whole reading copy', 'Current selected passage'])
                self.scope.SetSelection(int(initial.selection[0] != initial.selection[1]))
                self.numbers = field('Read numbers as', choices=['Cardinal words', 'Individual digits'])
                self.numbers.SetSelection(0)
                self.decimal = field('Decimal marker', choices=['Selected locale', 'Explicit dot', 'Explicit comma'])
                self.decimal.SetSelection(0)
                self.date_format = field('Exact date format', choices=['YYYY-MM-DD', 'Locale: DD.MM.YYYY / MM/DD/YYYY'])
                self.date_format.SetSelection(0)
                self.expansions = {}
                for key, label in (('numbers', 'Expand numbers'), ('dates', 'Expand dates'),
                        ('units', 'Expand listed units'), ('abbreviations', 'Expand listed abbreviations'),
                        ('math', 'Read numeric arithmetic operators'), ('symbols', 'Read listed symbols')):
                    control = wx.CheckBox(panel, label=label)
                    control.SetName(label)
                    self.expansions[key] = control
                    box.Add(control, 0, wx.ALL, 3)
                self.structures = {}
                for key, label in (('headings', 'Markdown headings'), ('tables', 'Simple pipe tables'),
                                   ('footnotes', 'Markdown footnotes'), ('code', 'Fenced code blocks')):
                    control = field(label, choices=['Keep exact source', 'Read with explicit labels', 'Skip recognized structure'])
                    control.SetSelection(0)
                    self.structures[key] = control
                self.before = field('Exact selected source', wx.TextCtrl,
                                    style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 115))
                self.after = field('Proposed reading text', wx.TextCtrl,
                                   style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 145))
                self.status = field('Speech preparation status', wx.TextCtrl,
                                    style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 95))
                panel.SetSizer(box)
                layout = wx.BoxSizer(wx.VERTICAL)
                layout.Add(panel, 1, wx.EXPAND)
                row = wx.BoxSizer(wx.HORIZONTAL)
                self.buttons = {}
                for key, label, callback in (('preview', '&Preview', self.on_preview),
                        ('apply', '&Apply reviewed text', self.on_apply), ('undo', '&Undo last preparation', self.on_undo),
                        ('cancel', 'Cancel preview', self.on_cancel), ('close', 'Close', self.on_close)):
                    control = wx.Button(self, label=label)
                    control.SetName(label.replace('&', ''))
                    control.Bind(wx.EVT_BUTTON, callback)
                    self.buttons[key] = control
                    row.Add(control, 0, wx.ALL, 4)
                layout.Add(row, 0, wx.EXPAND)
                self.SetSizer(layout)
                panel.FitInside()
                for control in (self.mode, self.locale, self.scope, self.numbers, self.decimal,
                                self.date_format, *self.structures.values()):
                    control.Bind(wx.EVT_CHOICE, self.on_change)
                for control in self.expansions.values():
                    control.Bind(wx.EVT_CHECKBOX, self.on_change)
                self.Bind(wx.EVT_CLOSE, self.on_close)
                self.report('Preview is limited to 200,000 characters and 5,000 edits. '
                            'Choose a smaller selected passage for a large copy. Every Apply is undoable.')
                self.controls()
                self.mode.SetFocus()
            except BaseException:
                try:
                    self.Destroy()
                except Exception:
                    pass
                raise

        def Destroy(self):
            if self._destroy_requested:
                return False
            self._destroy_requested = True
            self.alive = False
            cancel.set()
            return super().Destroy()

        def report(self, value):
            if self.alive:
                self.status.ChangeValue(str(value))

        def controls(self):
            if not self.alive:
                return
            speech = self.mode.GetSelection() == 1
            self.mode.Enable(not self.busy)
            self.scope.Enable(not self.busy)
            for control in (self.locale, self.numbers, self.decimal, self.date_format,
                            *self.expansions.values(), *self.structures.values()):
                control.Enable(not self.busy and speech)
            for key, button in self.buttons.items():
                button.Enable(not self.busy)
            self.buttons['close'].Enable(True)
            self.buttons['cancel'].Enable(self.busy)
            self.buttons['apply'].Enable(not self.busy and self.proposal is not None and self.proposal.result.changed)
            self.buttons['undo'].Enable(not self.busy and self.applied is not None)

        def form(self):
            return (self.mode.GetSelection(), self.locale.GetSelection(), self.scope.GetSelection(),
                    self.numbers.GetSelection(), self.decimal.GetSelection(), self.date_format.GetSelection(),
                    tuple((key, control.GetValue()) for key, control in self.expansions.items()),
                    tuple((key, control.GetSelection()) for key, control in self.structures.items()))

        def options(self, form):
            mode, locale, scope, numbers, decimal, dates, expansions, structures = form
            for value, count in ((mode, 2), (locale, 3), (scope, 2), (numbers, 2), (decimal, 3), (dates, 2)):
                if type(value) is not int or not 0 <= value < count:
                    raise ValueError('Choose the explicit preparation mode, language, scope and formats.')
            if mode == 0:
                return SpeechOptions()
            if locale == 0:
                raise ValueError('Choose Ukrainian or English explicitly; no fallback language is selected.')
            selected_structures = {}
            for key, index in structures:
                if type(index) is not int or not 0 <= index < 3:
                    raise ValueError('Choose keep, read or skip for each supported structure.')
                selected_structures[key] = ('keep', 'read', 'skip')[index]
            return SpeechOptions(mode='speech', locale=('', 'uk-UA', 'en-US')[locale],
                number_style=('cardinal', 'digits')[numbers], decimal_mark=('locale', '.', ',')[decimal],
                date_format=('iso', 'locale')[dates], **dict(expansions), **selected_structures)

        def stopping(self):
            return not self.alive or cancel.is_set() or stopped()

        def invalidate(self):
            self.proposal = self.reviewed_form = None
            if self.alive:
                self.before.ChangeValue('')
                self.after.ChangeValue('')

        def on_change(self, event=None):
            if self.alive and not self.busy:
                self.invalidate()
                self.controls()

        def begin(self, work, done):
            if self.busy or not self.alive:
                return
            cancel.clear()
            self.busy = True
            self.controls()
            def completed(value):
                self.busy = False
                if not self.alive:
                    return
                try:
                    if self.stopping():
                        raise InterruptedError('Speech preview cancelled. The reading copy is unchanged.')
                    guard()
                    done(value)
                except Exception as exc:
                    self.report(exc)
                self.controls()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.report(error)
                    self.controls()
            try:
                guard()
                if run(work, completed, on_failed=failed) is False:
                    failed('Finish or cancel the current reader operation first.')
            except Exception as exc:
                failed(exc)

        def on_preview(self, event=None):
            if self.busy or not self.alive:
                return
            try:
                guard()
                snapshot, form = capture(), self.form()
                options = self.options(form)
                bounds = (0, len(snapshot.text)) if form[2] == 0 else snapshot.selection
                _bounds(snapshot, bounds)
                self.invalidate()
                self.before.ChangeValue(snapshot.text[bounds[0]:bounds[1]])
                self.report('Preparing an explicit text proposal. Nothing is applied yet.')
                def accepted(proposal):
                    if capture() != snapshot or self.form() != form:
                        raise ValueError('The reading copy, selection or choices changed. Preview again.')
                    self.proposal, self.reviewed_form = proposal, form
                    self.after.ChangeValue(proposal.result.text)
                    self.report((str(len(proposal.result.edits)) + ' source-bound changes. '
                        'Apply changes only the reading copy; the original remains exact. '
                        if proposal.result.changed else 'The proposed passage is unchanged. ') +
                        '\n'.join(proposal.result.notices))
                self.begin(lambda: prepare_selection(snapshot, bounds, options, cancelled=self.stopping), accepted)
            except Exception as exc:
                self.report(exc)
                self.controls()

        def on_apply(self, event=None):
            if self.busy or not self.alive or self.proposal is None:
                return
            try:
                guard()
                if self.form() != self.reviewed_form or capture() != self.proposal.snapshot:
                    raise ValueError('The reading copy, selection or choices changed. Preview again.')
                proposal = self.proposal
                if not proposal.result.changed:
                    raise ValueError('The proposed passage is unchanged.')
                self.applied, warning = apply(proposal)
                self.invalidate()
                self.report('Reviewed speech text applied to this reading copy. Undo remains available. ' + warning)
            except Exception as exc:
                self.report(exc)
            self.controls()

        def on_undo(self, event=None):
            if self.busy or not self.alive or self.applied is None:
                return
            try:
                guard()
                warning = undo(self.applied)
                self.applied = None
                self.invalidate()
                self.report('Earlier reading text restored. ' + warning)
            except Exception as exc:
                self.report(exc)
            self.controls()

        def on_cancel(self, event=None):
            if self.alive and self.busy:
                cancel.set()
                self.report('Cancelling preview; waiting for the existing worker to settle.')

        def finish_close(self):
            if self.alive:
                self.alive = False
                cancel.set()
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)

        def on_close(self, event=None):
            if not self.alive:
                return
            self.close_requested = True
            cancel.set()
            allowed = request_close() if request_close is not None else not self.busy
            if not allowed:
                self.report('Waiting for the current text preview to finish cancellation.')
                if event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
                    event.Veto()
                return
            self.finish_close()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.alive = False
                cancel.set()
            event.Skip()

    return SpeechPreparationDialog()


def show_speech_preparation(parent, *, frame, library_root, capture, apply, undo, guard,
                            wx_module=None, on_ready=None):
    """Reuse the current frame's actual operation settlement and close guard."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    from .transcription_dialog import TranscriptionSession
    import threading
    root, holder = Path(library_root).absolute(), {}
    if Path(frame.library.root).absolute() != root:
        raise ValueError('The reading copy belongs to a different selected library.')

    def check():
        guard()
        session = holder.get('session')
        if session is not None:
            session.guard()
            if session.root != root:
                raise InterruptedError('The selected library changed during speech preparation.')

    def report(value):
        if 'view' in holder:
            holder['view'].report(value)

    def run(work, done, *, on_failed):
        def perform():
            try:
                return True, work()
            except Exception as exc:
                return False, exc
        def completed(result):
            success, value = result
            done(value) if success else on_failed(value)
        return holder['session'].submit('Prepare reading speech', perform, completed, kind='speech-preparation')

    def stopping():
        return 'session' in holder and holder['session'].stopped()

    def request_close():
        return 'session' not in holder or holder['session'].request_close()

    cancellation = threading.Event()
    view = create_speech_preparation(parent, capture=capture, apply=apply, undo=undo,
        guard=check, run=run, cancel=cancellation, stopped=stopping,
        request_close=request_close, wx_module=wx)
    holder['view'] = view
    try:
        session = TranscriptionSession(frame, dispatch=wx.CallAfter, changed=view.controls,
            report=report, close=view.finish_close, request_stop=cancellation.set)
        session.cancel = cancellation
        holder['session'] = session
        check()
        if on_ready is not None:
            wx.CallAfter(on_ready, view, session)
        view.ShowModal()
    finally:
        cancellation.set()
        if 'session' in holder:
            holder['session'].closed = True
        view.Destroy()

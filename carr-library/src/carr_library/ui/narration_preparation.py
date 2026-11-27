"""Native selected-range preparation with explicit review and owned cancellation."""
from pathlib import Path

from ..reader.preparation import (
    LANGUAGES, PreparationContext, PreparationPreview, SpellReport,
    accepted_word_rule, prepare_selection, spellcheck_selection, spelling_proposal,
)

UNIT_CHOICES = (
    ('Mass: kilogram', 'mass-kilogram', 'kg'),
    ('Length: metre', 'length-meter', 'm'),
    ('Length: kilometre', 'length-kilometer', 'km'),
    ('Duration: second', 'duration-second', 's'),
    ('Duration: minute', 'duration-minute', 'min'),
    ('Duration: hour', 'duration-hour', 'h'),
    ('Volume: litre', 'volume-liter', 'L'),
    ('Temperature: Celsius', 'temperature-celsius', '\u00b0C'),
)


class PreparationDialog:
    """The enclosing editor stays unchanged until this modal returns an acceptance."""
    def __init__(self, parent, project, start, end, *, guard=lambda: None):
        import wx
        from .narration_editor import PronunciationPreviewTask
        self.wx, self.project, self.start, self.end, self.guard = wx, project, start, end, guard
        self.result = self.rule = self.preview = self.report = None
        self.closed = self.close_pending = False
        self.dialog = wx.Dialog(parent, title='Prepare selected reading text', size=(900, 860),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.fields = []

        def text(label, value='', *, readonly=False, multiline=False):
            layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALL, 5)
            style = (wx.TE_READONLY if readonly else 0) | (wx.TE_MULTILINE if multiline else 0)
            control = wx.TextCtrl(self.dialog, value=value, style=style)
            control.SetName(label.replace('&', ''))
            layout.Add(control, 1 if multiline else 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
            if not readonly:
                self.fields.append(control)
                control.Bind(wx.EVT_TEXT, self.changed)
            return control

        def choice(label, options, selected=0, *, context=True):
            layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALL, 5)
            control = wx.Choice(self.dialog, choices=list(options))
            control.SetName(label.replace('&', ''))
            control.SetSelection(selected)
            layout.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
            self.fields.append(control)
            control.Bind(wx.EVT_CHOICE, self.changed if context else self.suggestion_changed)
            return control

        self.selection = text('Selected reading text (read only)', project.current_text[start:end],
                              readonly=True, multiline=True)
        self.language = choice('Exact preparation language', ('English [en]', 'Ukrainian [uk]', 'Slovak [sk]'))
        self.voice = text('Pronunciation voice scope (optional exact voice ID)')
        self.kind = choice('Explicit preparation operation', ('Number', 'Date', 'Unit', 'Symbol'))
        self.number = choice('Number-reading mode', ('Cardinal', 'Ordinal', 'Individual digits', 'Year'))
        self.date_order = choice('Written date order', ('Year / month / day', 'Day / month / year', 'Month / day / year'))
        self.unit = choice('Explicit unit meaning', [row[0] for row in UNIT_CHOICES])
        self.unit_symbol = text('Exact written unit symbol', 'kg')
        self.gender = choice('Ukrainian cardinal gender', ('Masculine', 'Feminine'))
        self.case = choice('Ukrainian cardinal grammatical case',
                           ('Nominative', 'Genitive', 'Dative', 'Accusative', 'Instrumental', 'Locative'))
        self.normal_manifest = text('Reviewed optional number/date/unit package manifest')
        self.spell_manifest = text('Reviewed Hunspell runtime and dictionary manifest')
        self.issues = wx.ListBox(self.dialog)
        self.issues.SetName('Spelling occurrences with exact reading offsets')
        self.issues.Bind(wx.EVT_LISTBOX, self.selected_issue)
        layout.Add(self.issues, 1, wx.EXPAND | wx.ALL, 6)
        self.suggestions = choice('Hunspell suggestions for selected occurrence', (), context=False)
        self.details = text('Preparation preview, provenance and operation status',
            'Select reviewed local tool manifests. Preview first; Apply changes only this reading copy. '
            'Date/number grammar and spelling suggestions require human review. No language-quality claim.',
            readonly=True, multiline=True)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 5)
        self.buttons = {}
        actions = (
            ('normal_browse', 'Select number tool...', lambda event: self.pick(self.normal_manifest)),
            ('spell_browse', 'Select Hunspell dictionary...', lambda event: self.pick(self.spell_manifest)),
            ('normalize', '&Preview number/date/unit/symbol', self.normalize),
            ('spell', '&Check selected spelling', self.spell),
            ('spelling_preview', 'Preview selected spelling &suggestion', self.spelling_preview),
            ('accept_word', 'Accept this word for project/&language/voice', self.accept_word),
            ('apply', '&Apply reviewed change', self.apply),
            ('cancel', 'Cancel &work', self.cancel),
            ('close', 'Close &without applying', self.close),
        )
        for key, label, handler in actions:
            button = wx.Button(self.dialog, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, handler)
            self.buttons[key] = button
            buttons.Add(button, 0, wx.ALL, 4)
        self.task = PronunciationPreviewTask(dispatch=wx.CallAfter, completed=self.completed)
        self.dialog.SetSizer(layout)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.SetEscapeId(self.buttons['close'].GetId())
        self.dialog.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord('P'), self.buttons['normalize'].GetId()),
            (wx.ACCEL_ALT, ord('C'), self.buttons['spell'].GetId()),
        ]))
        for key in ('normalize', 'spell'):
            self.dialog.Bind(wx.EVT_MENU, self.normalize if key == 'normalize' else self.spell,
                             id=self.buttons[key].GetId())
        self.refresh()

    def say(self, message):
        if not self.closed:
            self.details.ChangeValue(str(message))
            self.details.SetInsertionPoint(0)

    def pick(self, control):
        if self.task.busy:
            return
        wx = self.wx
        picker = wx.FileDialog(self.dialog, 'Choose explicitly reviewed local tool manifest',
                               wildcard='Local tool manifests (*.json)|*.json',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                control.ChangeValue(str(Path(picker.GetPath()).absolute()))
                self.changed()
        finally:
            picker.Destroy()

    def changed(self, event=None):
        if not hasattr(self, 'task') or self.closed:
            return
        if self.task.busy:
            return
        self.preview = None
        self.report = None
        self.issues.Set([])
        self.suggestions.Set([])
        if event is not None and event.GetEventObject() is self.unit:
            index = self.unit.GetSelection()
            if 0 <= index < len(UNIT_CHOICES):
                self.unit_symbol.ChangeValue(UNIT_CHOICES[index][2])
        self.refresh()

    def context(self):
        language = self.language.GetSelection()
        kind = self.kind.GetSelection()
        number = self.number.GetSelection()
        date_order = self.date_order.GetSelection()
        if not 0 <= language < 3 or not 0 <= kind < 4 or not 0 <= number < 4 or not 0 <= date_order < 3:
            raise ValueError('Choose an explicit language and preparation context.')
        unit_id = unit_symbol = ''
        if kind == 2:
            unit_index = self.unit.GetSelection()
            if not 0 <= unit_index < len(UNIT_CHOICES):
                raise ValueError('Choose an explicit unit meaning.')
            unit_id, unit_symbol = UNIT_CHOICES[unit_index][1], self.unit_symbol.GetValue()
        gender = 'feminine' if self.gender.GetSelection() == 1 else 'masculine'
        case_index = self.case.GetSelection()
        cases = ('nominative', 'genitive', 'dative', 'accusative', 'instrumental', 'locative')
        if not 0 <= case_index < len(cases):
            raise ValueError('Choose a grammatical case.')
        uk_cardinal = language == 1 and (kind != 0 or number == 0)
        return PreparationContext(LANGUAGES[language], ('number', 'date', 'unit', 'symbol')[kind],
            ('cardinal', 'ordinal', 'digits', 'year')[number] if kind == 0 else 'cardinal',
            ('ymd', 'dmy', 'mdy')[date_order], unit_id, unit_symbol,
            gender if uk_cardinal else 'masculine', cases[case_index] if uk_cardinal else 'nominative',
            self.voice.GetValue())

    def refresh(self):
        if self.closed:
            return
        idle = not self.task.busy and not self.close_pending
        for control in self.fields:
            control.Enable(idle)
        kind, language = self.kind.GetSelection(), self.language.GetSelection()
        self.number.Enable(idle and kind == 0)
        self.date_order.Enable(idle and kind == 1)
        self.unit.Enable(idle and kind == 2)
        self.unit_symbol.Enable(idle and kind == 2)
        uk_cardinal = language == 1 and (kind != 0 or self.number.GetSelection() == 0)
        self.gender.Enable(idle and uk_cardinal)
        self.case.Enable(idle and uk_cardinal)
        for key in ('normal_browse', 'spell_browse'):
            self.buttons[key].Enable(idle)
        supported = kind != 3 or language == 0
        self.buttons['normalize'].Enable(idle and supported and bool(self.normal_manifest.GetValue().strip()))
        self.buttons['spell'].Enable(idle and bool(self.spell_manifest.GetValue().strip()))
        self.buttons['cancel'].Enable(self.task.busy and not self.task.cancel.is_set())
        self.buttons['close'].Enable(True)
        self.buttons['apply'].Enable(idle and self.preview is not None)
        issue = self.issues.GetSelection()
        has_issue = self.report is not None and 0 <= issue < len(self.report.issues)
        self.buttons['accept_word'].Enable(idle and has_issue)
        self.buttons['spelling_preview'].Enable(idle and has_issue and self.suggestions.GetSelection() >= 0)
        self.issues.Enable(idle)
        self.suggestions.Enable(idle and has_issue)
        if idle and not supported:
            self.say('Unicode symbol names are English only. Use the existing scoped pronunciation dictionary '
                     'for Ukrainian or Slovak; no untranslated symbol names will be applied.')

    def _start(self, operation):
        if self.task.busy or self.closed or self.close_pending:
            return
        self.guard()
        self.preview = self.report = None
        self.issues.Set([])
        self.suggestions.Set([])
        self.task.start_operation(operation)
        self.refresh()
        self.say('Preparing the explicit selection. Cancel work or Close settles the owned worker; text is unchanged.')

    def normalize(self, event=None):
        try:
            context = self.context()
            manifest = self.normal_manifest.GetValue()
            self._start(lambda cancelled: prepare_selection(self.project, self.start, self.end, context,
                                                            manifest, cancelled=cancelled))
        except Exception as exc:
            self.say(str(exc))

    def spell(self, event=None):
        try:
            index = self.language.GetSelection()
            if not 0 <= index < len(LANGUAGES):
                raise ValueError('Choose an exact spelling dictionary language.')
            language, voice = LANGUAGES[index], self.voice.GetValue()
            manifest = self.spell_manifest.GetValue()
            self._start(lambda cancelled: spellcheck_selection(self.project, self.start, self.end, manifest,
                                      language=language, voice_id=voice, cancelled=cancelled))
        except Exception as exc:
            self.say(str(exc))

    def completed(self, result, error):
        if self.closed:
            return
        if self.close_pending:
            self.result = self.rule = None
            self.closed = True
            self.dialog.EndModal(self.wx.ID_CANCEL)
            return
        try:
            self.guard()
            if error is not None:
                self.say(str(error))
            elif type(result) is PreparationPreview:
                self.preview = result
                self.say(result.description())
            elif type(result) is SpellReport:
                self.report = result
                self.issues.Set([f'[{row.start}, {row.end}) {row.word}; {len(row.suggestions)} suggestions'
                                 for row in result.issues])
                if result.issues:
                    self.issues.SetSelection(0)
                    self.selected_issue()
                self.say(f'Hunspell checked {result.checked_words} words. '
                         f'{len(result.issues)} reported occurrences; {result.protected_words} words have '
                         f'explicit pronunciation overrides; {result.skipped_tokens} numeric/code/long tokens skipped. '
                         'Choose one occurrence and suggestion, then preview. No correction is automatic.')
            else:
                raise ValueError('The preparation operation returned no validated result.')
        except Exception as exc:
            self.preview = self.report = None
            self.say(str(exc))
        finally:
            self.refresh()

    def selected_issue(self, event=None):
        if self.task.busy or self.report is None:
            return
        index = self.issues.GetSelection()
        self.preview = None
        self.suggestions.Set([])
        if 0 <= index < len(self.report.issues):
            self.suggestions.Set(list(self.report.issues[index].suggestions))
            if self.report.issues[index].suggestions:
                self.suggestions.SetSelection(0)
        self.refresh()

    def suggestion_changed(self, event=None):
        if hasattr(self, 'task') and not self.task.busy:
            self.preview = None
            self.refresh()

    def spelling_preview(self, event=None):
        if self.task.busy or self.report is None:
            return
        try:
            self.guard()
            index, choice = self.issues.GetSelection(), self.suggestions.GetSelection()
            if not 0 <= index < len(self.report.issues) or not 0 <= choice < len(self.report.issues[index].suggestions):
                raise ValueError('Choose a spelling occurrence and suggestion.')
            self.preview = spelling_proposal(self.project, self.report, index,
                                            self.report.issues[index].suggestions[choice])
            self.say(self.preview.description())
            self.refresh()
        except Exception as exc:
            self.say(str(exc))

    def accept_word(self, event=None):
        if self.task.busy or self.report is None:
            return
        try:
            self.guard()
            self.rule = accepted_word_rule(self.report, self.issues.GetSelection())
            self.result = None
            self.closed = True
            self.dialog.EndModal(self.wx.ID_OK)
        except Exception as exc:
            self.say(str(exc))

    def apply(self, event=None):
        if self.task.busy or self.preview is None or self.closed:
            return
        try:
            self.guard()
            self.preview.record.validate_revision(self.project.original_text, self.project.current_text,
                                                  self.preview.text)
            self.result, self.rule = self.preview, None
            self.closed = True
            self.dialog.EndModal(self.wx.ID_OK)
        except Exception as exc:
            self.say(str(exc))

    def cancel(self, event=None):
        if self.task.busy:
            self.task.stop()
            self.refresh()
            self.say('Cancelling the owned preparation worker; waiting for cleanup. No partial change is applied.')

    def close(self, event=None):
        if self.closed:
            return
        if self.task.busy:
            self.close_pending = True
            self.cancel()
            if event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
                event.Veto()
            return
        self.result = self.rule = None
        self.closed = True
        self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self, *, on_ready=None):
        try:
            self.guard()
            self.selection.SetFocus()
            if on_ready is not None:
                self.wx.CallAfter(on_ready, self)
            self.dialog.ShowModal()
            return self.result, self.rule
        finally:
            self.closed = True
            self.task.dispose()
            self.dialog.Destroy()


def show_preparation(parent, project, start, end, *, guard=lambda: None, on_ready=None):
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(project.current_text):
        raise ValueError('Select the exact reading text to prepare first.')
    return PreparationDialog(parent, project, start, end, guard=guard).show(on_ready=on_ready)

"""Native installed OCR model selection; no model downloads or source changes."""
from dataclasses import replace

from ..extract.ocr_languages import (MAX_SELECTED_MODELS, OcrLanguageError,
    discover_languages, load_presets, model_label, store_preset, delete_preset,
    split_languages)
from ..extract.page_ocr import Config
from ..extract.pinned_tessdata import POLICY


class OcrOptionsActions:
    """Model selection handlers separately tested without a native UI verdict."""
    def report(self, text, *, focus=True):
        self.status.SetValue(text)
        if focus:
            self.status.SetFocus()

    def model_profile(self):
        control = getattr(self, 'model_source', None)
        if control is None:
            return self.initial.model_profile
        index = control.GetSelection()
        if index not in (0, 1, 2):
            raise OcrLanguageError('Choose the Installed, Fast or Best OCR model set.')
        return ('installed', 'fast', 'best')[index]

    def change_model_profile(self, event=None):
        if self.busy:
            return
        if self.model_profile() == 'best':
            self.profile.SetSelection(0)
        self.catalog = None
        self.rescan()

    def manage_models(self, event=None):
        if self.busy:
            self.report('Wait for the current model check before managing OCR models.')
            return
        from .ocr_model_manager import show_ocr_model_manager
        show_ocr_model_manager(self)
        self.rescan()

    def filter_models(self, event=None):
        self.visible_codes = self.catalog.matching(self.filter.GetValue()) if self.catalog else ()
        self.available.SetItems([model_label(code) for code in self.visible_codes])
        self.available.SetSelection(0 if self.visible_codes else -1)
        if event is not None:
            self.report(f'{len(self.visible_codes)} installed recognition models match the filter.', focus=False)

    def _show_selected(self, selection=None):
        if selection is None:
            selection = self.chosen.GetSelection()
        codes = self.catalog.selectable if self.catalog else ()
        self.chosen.SetItems([f'{i}. {model_label(code)}' +
                             ('; not currently installed' if self.catalog and code not in codes else '')
                             for i, code in enumerate(self.selected_codes, 1)])
        self.chosen.SetSelection(min(max(selection, 0), len(self.selected_codes) - 1))

    def add_model(self, event=None):
        index = self.available.GetSelection()
        try:
            if self.busy or not 0 <= index < len(self.visible_codes):
                raise OcrLanguageError('Select an installed recognition model first.')
            code = self.visible_codes[index]
            if code in self.selected_codes:
                raise OcrLanguageError('That model is already selected.')
            if len(self.selected_codes) >= MAX_SELECTED_MODELS:
                raise OcrLanguageError(f'Use at most {MAX_SELECTED_MODELS} models in one OCR pass. Remove one before adding another.')
            self.selected_codes.append(code)
            self._show_selected(len(self.selected_codes) - 1)
            self.report(model_label(code) + ' added. Order is the recognition priority.', focus=False)
        except ValueError as exc:
            self.report(str(exc))

    def remove_model(self, event=None):
        index = self.chosen.GetSelection()
        if self.busy or not 0 <= index < len(self.selected_codes):
            self.report('Select a model in the recognition order first.')
            return
        removed = self.selected_codes.pop(index)
        self._show_selected(index)
        self.chosen.SetFocus()
        self.report(model_label(removed) + ' removed from this choice.', focus=False)

    def move_model(self, direction):
        index = self.chosen.GetSelection()
        if (self.busy or direction not in (-1, 1) or not 0 <= index < len(self.selected_codes)
                or not 0 <= index + direction < len(self.selected_codes)):
            self.report('That model cannot move farther in this direction.')
            return
        other = index + direction
        self.selected_codes[index], self.selected_codes[other] = self.selected_codes[other], self.selected_codes[index]
        self._show_selected(other)
        self.chosen.SetFocus()
        self.report('Recognition priority updated.', focus=False)

    def current_config(self):
        if self.busy or self.catalog is None:
            raise OcrLanguageError('Wait for the installed OCR models to be checked.')
        if not self.selected_codes:
            raise OcrLanguageError('Choose at least one text recognition language or script.')
        index = self.profile.GetSelection()
        if index not in (0, 1):
            raise OcrLanguageError('Choose a model profile.')
        config = replace(self.initial, languages='+'.join(self.selected_codes),
                         orientation=self.orientation.GetValue(),
                         model_policy=('default', POLICY)[index],
                         model_profile=self.model_profile())
        if config.model_profile != self.catalog.model_profile:
            raise OcrLanguageError('The model set changed. Rescan before using these settings.')
        if self.catalog.integrity != 'unavailable':
            self.catalog.validate(config.languages, orientation=config.orientation,
                                  model_policy=config.model_policy)
        return config

    def accept(self, event=None):
        try:
            config = self.current_config()
        except ValueError as exc:
            self.report(str(exc))
            return
        import wx
        self.result = config
        self.EndModal(wx.ID_OK)

    def _presets(self):
        items = []
        problem = None
        if self.catalog is not None:
            for name, codes in [('English', 'eng'), ('Ukrainian', 'ukr'), ('Slovak', 'slk'),
                    ('English, Ukrainian and Slovak', 'eng+ukr+slk')]:
                if set(codes.split('+')) <= set(self.catalog.selectable):
                    items.append((name, dict(languages=codes, model_policy='default', orientation=False,
                                             model_profile=self.catalog.model_profile), False))
        if self.preset_root is not None:
            try:
                for row in load_presets(self.preset_root):
                    items.append(('Saved: ' + row['name'], {k: v for k, v in row.items() if k != 'name'}, row['name']))
            except (ValueError, OSError) as exc:
                problem = str(exc)
        self.preset_items = tuple(items)
        self.presets.SetItems([label for label, _, _ in items])
        self.presets.SetSelection(0 if items else -1)
        return problem

    def apply_preset(self, event=None):
        try:
            index = self.presets.GetSelection()
            if self.busy or self.catalog is None or not 0 <= index < len(self.preset_items):
                raise OcrLanguageError('Select a preset first.')
            label, settings, _ = self.preset_items[index]
            expected_profile = settings.get('model_profile', 'installed')
            if expected_profile != self.catalog.model_profile:
                raise OcrLanguageError('This preset uses ' + expected_profile.title() +
                    ' models. Choose that model set, wait for its inventory, then apply the preset.')
            self.catalog.validate(settings['languages'], orientation=settings['orientation'],
                                  model_policy=settings['model_policy'])
            self.selected_codes = list(split_languages(settings['languages']))
            self.profile.SetSelection(0 if settings['model_policy'] == 'default' else 1)
            self.orientation.SetValue(settings['orientation'])
            self._show_selected(0)
            self.chosen.SetFocus()
            self.report('Applied ' + label + '. Review the order, then use these settings.', focus=False)
        except ValueError as exc:
            self.report(str(exc))

    def save_preset(self, event=None):
        import wx
        try:
            if self.preset_root is None:
                raise OcrLanguageError('Open a library before saving a recognition preset.')
            config = self.current_config()
            if self.catalog.integrity == 'unavailable':
                raise OcrLanguageError('Install and check the OCR models before saving a preset.')
            dialog = wx.TextEntryDialog(self, 'Name this ordered language selection.', 'Save OCR preset')
            try:
                name = dialog.GetValue().strip() if dialog.ShowModal() == wx.ID_OK else None
            finally:
                dialog.Destroy()
            if name is None:
                return
            try:
                store_preset(self.preset_root, name, config)
            except FileExistsError:
                if wx.MessageBox('Replace the saved preset with this name?', 'Replace OCR preset',
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                store_preset(self.preset_root, name, config, replace=True)
            self._presets()
            self.report('OCR preset saved. It can be reused in this library after restarting.')
        except Exception as exc:
            self.report(str(exc))

    def remove_preset(self, event=None):
        import wx
        try:
            index = self.presets.GetSelection()
            if self.busy or not 0 <= index < len(self.preset_items) or not self.preset_items[index][2]:
                raise OcrLanguageError('Choose a saved custom preset to delete. Built-in presets are retained.')
            name = self.preset_items[index][2]
            if wx.MessageBox('Delete this saved OCR preset? Existing import plans keep their settings.',
                             'Delete OCR preset', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                return
            delete_preset(self.preset_root, name)
            self._presets()
            self.report('Saved preset deleted. Current choices and saved import plans are unchanged.')
        except Exception as exc:
            self.report(str(exc))

    def rescan(self, event=None):
        if self.busy or self.closed:
            return
        import wx
        self.busy = True
        self.cancel_event.clear()
        self.ok.Disable()
        self.rescan_button.Disable()
        for control in (getattr(self, 'model_source', None), getattr(self, 'manage_button', None)):
            if control is not None:
                control.Disable()
        profile = self.model_profile()
        self.report('Checking the selected local OCR engine and its installed models…', focus=False)

        def finish(future):
            def apply():
                if self.closed:
                    return
                self.busy = False
                self.rescan_button.Enable()
                for control in (getattr(self, 'model_source', None), getattr(self, 'manage_button', None)):
                    if control is not None:
                        control.Enable()
                try:
                    self.catalog = future.result()
                    self.filter_models()
                    self._show_selected()
                    preset_problem = self._presets()
                    if self.catalog.integrity == 'unavailable':
                        self.ok.SetLabel('Continue with available text')
                        message = ('No local OCR engine was found. Native text can still be imported; '
                                   'scanned pages will remain for review. No model was downloaded.')
                    elif self.catalog.integrity == 'managed_inventory':
                        self.ok.SetLabel('Use settings')
                        message = (f'{self.catalog.engine_version}: {len(self.catalog.selectable)} recognition models. '
                                   'Selected model bytes are verified before OCR. No recognition-quality test '
                                   'is implied by this inventory. Use Manage offline models to install missing models.')
                    else:
                        self.ok.SetLabel('Use settings')
                        integrity = ('Portable model inventory and SHA256 checks passed.'
                                     if self.catalog.integrity == 'portable_sha256' else
                                     'System-installed models; publisher authenticity is not verified here.')
                        message = (f'{self.catalog.engine_version}: {len(self.catalog.selectable)} recognition models. '
                                   + integrity + ' Add one or more models in the required priority order. '
                                   'OSD checks orientation; it does not select a language.')
                    if preset_problem:
                        message += (' Saved presets could not be read and were retained unchanged: '
                                    + preset_problem + ' Manual model selection is available.')
                    self.ok.Enable()
                    self.report(message, focus=False)
                except Exception as exc:
                    self.catalog = None
                    self.visible_codes = ()
                    self.available.SetItems([])
                    self.ok.Disable()
                    self.report(f'Model check did not complete: {exc} Use Rescan after correcting the installation.')
            if not self.closed:
                wx.CallAfter(apply)

        self.executor.submit(discover_languages, cancelled=self.cancel_event.is_set,
                             profile=profile).add_done_callback(finish)


def create_ocr_dialog(parent, initial=None, *, preset_root=None):
    """Create native controls; caller owns ShowModal/Destroy and result handling."""
    import wx
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    class OcrOptionsDialog(wx.Dialog, OcrOptionsActions):
        def __init__(self):
            super().__init__(parent, title='Text recognition languages', size=(800, 730),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.initial = initial or Config()
            self.result = None
            self.preset_root = preset_root
            self.selected_codes = list(split_languages(self.initial.languages))
            self.catalog = None
            self.visible_codes, self.preset_items = (), ()
            self.busy = self.closed = False
            self.cancel_event = Event()
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ocr-models')
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(self, label='Choose installed languages or script models for scanned pages. '
                'Filter, add models, then order them. Automatic language detection is not enabled. '
                'Originals stay unchanged and recognized text still requires review.')
            introduction.Wrap(730)
            layout.Add(introduction, 0, wx.EXPAND | wx.ALL, 8)
            source_row = wx.BoxSizer(wx.HORIZONTAL)
            source_row.Add(wx.StaticText(self, label='OCR model set'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
            self.model_source = wx.Choice(self, choices=['Installed with the current OCR engine',
                'Managed Fast models', 'Managed Best models'])
            self.model_source.SetName('OCR model set: Installed, Fast or Best')
            self.model_source.SetSelection(('installed', 'fast', 'best').index(self.initial.model_profile))
            self.model_source.Bind(wx.EVT_CHOICE, self.change_model_profile)
            source_row.Add(self.model_source, 1, wx.EXPAND | wx.RIGHT, 8)
            self.manage_button = wx.Button(self, label='Manage offline &models...')
            self.manage_button.SetName('Manage offline OCR models')
            self.manage_button.Bind(wx.EVT_BUTTON, self.manage_models)
            source_row.Add(self.manage_button, 0)
            layout.Add(source_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            layout.Add(wx.StaticText(self, label='&Filter installed languages and scripts'), 0, wx.LEFT | wx.RIGHT, 8)
            self.filter = wx.TextCtrl(self)
            self.filter.SetName('Filter installed languages and scripts')
            self.filter.Bind(wx.EVT_TEXT, self.filter_models)
            layout.Add(self.filter, 0, wx.EXPAND | wx.ALL, 8)
            lists = wx.BoxSizer(wx.HORIZONTAL)
            for label, attribute in [('Installed recognition models', 'available'),
                                     ('Selected models in recognition order', 'chosen')]:
                column = wx.BoxSizer(wx.VERTICAL)
                column.Add(wx.StaticText(self, label=label), 0, wx.BOTTOM, 4)
                control = wx.ListBox(self, size=(-1, 155))
                control.SetName(label)
                setattr(self, attribute, control)
                column.Add(control, 1, wx.EXPAND)
                lists.Add(column, 1, wx.EXPAND | wx.RIGHT, 8)
            self.available.Bind(wx.EVT_LISTBOX_DCLICK, self.add_model)
            layout.Add(lists, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            actions = wx.BoxSizer(wx.HORIZONTAL)
            for label, handler in [('&Add selected', self.add_model), ('&Remove selected', self.remove_model),
                    ('Move &up', lambda event: self.move_model(-1)),
                    ('Move &down', lambda event: self.move_model(1))]:
                button = wx.Button(self, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                actions.Add(button, 0, wx.RIGHT, 6)
            layout.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            profiles = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            profiles.AddGrowableCol(1)
            self.profile = wx.Choice(self, choices=['Installed models (identity recorded during import)',
                'Pinned tessdata_fast: English, Ukrainian and Slovak'])
            self.profile.SetSelection(0 if self.initial.model_policy == 'default' else 1)
            self.presets = wx.Choice(self)
            for label, control in [('Model profile', self.profile), ('Recognition presets', self.presets)]:
                control.SetName(label)
                profiles.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                profiles.Add(control, 1, wx.EXPAND)
            layout.Add(profiles, 0, wx.EXPAND | wx.ALL, 8)
            presets = wx.BoxSizer(wx.HORIZONTAL)
            for label, handler in [('Use &preset', self.apply_preset), ('&Save preset...', self.save_preset),
                                   ('D&elete saved preset...', self.remove_preset)]:
                button = wx.Button(self, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                if preset_root is None and handler != self.apply_preset:
                    button.Disable()
                presets.Add(button, 0, wx.RIGHT, 6)
            layout.Add(presets, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.orientation = wx.CheckBox(self, label='Check page &orientation with the installed OSD model')
            self.orientation.SetName('Check page orientation with the installed OSD model')
            self.orientation.SetValue(self.initial.orientation)
            layout.Add(self.orientation, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            self.status.SetName('OCR model status and validation')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.rescan_button = wx.Button(self, label='Re&scan installed models')
            self.rescan_button.Bind(wx.EVT_BUTTON, self.rescan)
            buttons.Add(self.rescan_button, 0, wx.RIGHT, 8)
            buttons.AddStretchSpacer(1)
            self.ok = wx.Button(self, wx.ID_OK, 'Use settings')
            self.ok.Bind(wx.EVT_BUTTON, self.accept)
            self.ok.Disable()
            self.ok.SetDefault()
            buttons.Add(self.ok, 0, wx.RIGHT, 8)
            buttons.Add(wx.Button(self, wx.ID_CANCEL, 'Cancel'))
            layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetMinSize((720, 650))
            self._show_selected(0)
            self.filter.SetFocus()
            wx.CallAfter(self.rescan)

        def Destroy(self):
            self.closed = True
            self.cancel_event.set()
            self.executor.shutdown(wait=False, cancel_futures=True)
            return super().Destroy()

    return OcrOptionsDialog()

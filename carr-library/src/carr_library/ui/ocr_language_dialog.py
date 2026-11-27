"""Native installed OCR model selection; no model downloads or source changes."""
from dataclasses import replace
from pathlib import Path

from ..extract.ocr_languages import (MAX_SELECTED_MODELS, OcrLanguageError,
    discover_languages, load_presets, model_label, store_preset, delete_preset,
    split_languages)
from ..extract.page_ocr import Config
from ..extract.pinned_tessdata import POLICY

MODEL_PROFILES = ('installed', 'fast', 'best')


class OcrOptionsActions:
    """Model selection handlers separately tested without a native UI verdict."""
    def report(self, text, *, focus=True):
        self.status.SetValue(text)
        if focus:
            self.status.SetFocus()

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

    def _model_profile(self):
        return getattr(self, 'active_model_profile', self.initial.model_profile)

    def _set_model_profile(self, value):
        self.active_model_profile = value
        if hasattr(self, 'model_source'):
            self.model_source.SetSelection(MODEL_PROFILES.index(value))

    def change_model_profile(self, event=None):
        if self.busy or self.closed:
            self._set_model_profile(self._model_profile())
            return
        index = self.model_source.GetSelection()
        if index not in range(len(MODEL_PROFILES)):
            self.report('Choose an installed, managed Fast, or managed Best profile.')
            return
        self._set_model_profile(MODEL_PROFILES[index])
        self.active_pack = None
        self.catalog = None
        self.rescan()

    def manage_models(self, event=None):
        if self.busy or self.closed:
            return
        from .ocr_model_manager import show_ocr_model_manager
        show_ocr_model_manager(self)
        self.rescan()

    def current_config(self):
        if self.busy or self.catalog is None:
            raise OcrLanguageError('Wait for the installed OCR models to be checked.')
        index = self.profile.GetSelection()
        if index not in (0, 1):
            raise OcrLanguageError('Choose a model profile.')
        model_profile = self._model_profile()
        if self.catalog.model_profile != model_profile:
            raise OcrLanguageError('Check the selected model profile before using these settings.')
        pack = getattr(self, 'active_pack', None)
        config = replace(self.initial, languages='+'.join(self.selected_codes),
                         orientation=self.orientation.GetValue(),
                         model_policy=('default', POLICY)[index],
                         model_profile=model_profile,
                         model_pack_path=pack[0] if pack else None,
                         model_pack_sha256=pack[1] if pack else None)
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
                    settings = dict(languages=codes, model_policy='default', orientation=False)
                    pack = getattr(self, 'active_pack', None)
                    if pack:
                        settings.update(model_pack_path=pack[0], model_pack_sha256=pack[1])
                    if self._model_profile() != 'installed':
                        settings['model_profile'] = self._model_profile()
                    items.append((name, settings, False))
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
            binding = ((settings['model_pack_path'], settings['model_pack_sha256'])
                       if settings.get('model_pack_path') else None)
            model_profile = settings.get('model_profile', 'installed')

            def apply(catalog):
                if catalog.model_profile != model_profile:
                    raise OcrLanguageError('The returned OCR inventory belongs to a different profile.')
                catalog.validate(settings['languages'], orientation=settings['orientation'],
                                 model_policy=settings['model_policy'])
                self._set_model_profile(model_profile)
                self.active_pack, self.catalog = binding, catalog
                self.selected_codes = list(split_languages(settings['languages']))
                self.profile.SetSelection(0 if settings['model_policy'] == 'default' else 1)
                self.orientation.SetValue(settings['orientation'])
                self.filter_models()
                self._show_selected(0)
                self.chosen.SetFocus()
                self.report('Applied ' + label + '. Review the order, then use these settings.', focus=False)

            if binding != getattr(self, 'active_pack', None) or model_profile != self._model_profile():
                options = dict(model_pack_path=binding[0], model_pack_sha256=binding[1]) if binding else {}
                if model_profile != 'installed':
                    options['model_profile'] = model_profile
                self._pack_job('Checking the saved OCR model binding…',
                    lambda: discover_languages(cancelled=self.cancel_event.is_set, **options), apply,
                    activation=True)
            else:
                apply(self.catalog)
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
        model_profile = self._model_profile()
        self.busy = True
        self.cancel_event.clear()
        self.ok.Disable()
        self.rescan_button.Disable()
        self._enable_pack_controls(False)
        self.report('Checking the selected local OCR engine and its installed models…', focus=False)

        def finish(future):
            def apply():
                if self.closed:
                    return
                self.busy = False
                self.rescan_button.Enable()
                self._enable_pack_controls(True)
                try:
                    catalog = future.result()
                    if catalog.model_profile != model_profile:
                        raise OcrLanguageError('The returned OCR inventory belongs to a different profile.')
                    self.catalog = catalog
                    self.filter_models()
                    self._show_selected()
                    preset_problem = self._presets()
                    if self.catalog.integrity == 'unavailable':
                        self.ok.SetLabel('Continue with available text')
                        message = ('No local OCR engine was found. Native text can still be imported; '
                                   'scanned pages will remain for review. No model was downloaded.')
                    else:
                        self.ok.SetLabel('Use settings')
                        integrity = ('No models are installed in this managed profile. Use the shared model manager.'
                                     if self.catalog.integrity == 'verified_managed_profile' and not self.catalog.codes else
                                     'Selected managed model profile and license checks passed.'
                                     if self.catalog.integrity == 'verified_managed_profile' else
                                     'Selected offline pack model and license SHA256 checks passed.'
                                     if self.catalog.integrity == 'verified_model_pack' else
                                     'Portable model inventory and SHA256 checks passed.'
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

        pack = getattr(self, 'active_pack', None)
        options = dict(model_pack_path=pack[0], model_pack_sha256=pack[1]) if pack else {}
        if model_profile != 'installed':
            options['model_profile'] = model_profile
        self.executor.submit(discover_languages, cancelled=self.cancel_event.is_set, **options).add_done_callback(finish)

    def _enable_pack_controls(self, enabled):
        self.profile.Enable() if enabled else self.profile.Disable()
        if hasattr(self, 'model_source'):
            self.model_source.Enable() if enabled else self.model_source.Disable()
        for control in getattr(self, 'pack_controls', ()):
            control.Enable() if enabled else control.Disable()
        if hasattr(self, 'pack_stop'):
            self.pack_stop.Disable() if enabled else self.pack_stop.Enable()

    def _pack_job(self, message, job, success, *, activation=False):
        if self.busy or self.closed:
            return
        import wx
        self.busy = True
        self.cancel_event.clear()
        self.ok.Disable()
        self.rescan_button.Disable()
        self._enable_pack_controls(False)
        self.report(message, focus=False)

        def finished(future):
            def apply():
                if self.closed:
                    return
                self.busy = False
                self.rescan_button.Enable()
                self._enable_pack_controls(True)
                if self.catalog is not None:
                    self.ok.Enable()
                try:
                    result = future.result()
                    if activation and self.cancel_event.is_set():
                        self.report('Pack verification was cancelled. The current OCR selection is unchanged.')
                        return
                    success(result)
                    if self.catalog is not None:
                        self.ok.Enable()
                        if self.catalog.integrity != 'unavailable':
                            self.ok.SetLabel('Use settings')
                except Exception as exc:
                    self.report('OCR pack operation did not complete: ' + str(exc))
            if not self.closed:
                wx.CallAfter(apply)

        try:
            self.executor.submit(job).add_done_callback(finished)
        except Exception as exc:
            self.busy = False
            self.rescan_button.Enable()
            self._enable_pack_controls(True)
            if self.catalog is not None:
                self.ok.Enable()
            self.report('OCR pack operation could not start: ' + str(exc))

    def catalogue_filter(self, event=None):
        from ..extract.ocr_model_catalog import list_models
        try:
            profile = self._selected_pack_profile()
            self.catalogue_models = list_models(profile, query=self.pack_filter.GetValue())
            self.pack_models.SetItems([f'{model.name} [{model.code}]; {model.kind}; {model.size:,} bytes'
                                      for model in self.catalogue_models])
            self.pack_models.SetSelection(0 if self.catalogue_models else -1)
        except (OSError, ValueError, IndexError) as exc:
            self.report(str(exc))

    def catalogue_add(self, event=None):
        index = self.pack_models.GetSelection()
        if self.busy or not 0 <= index < len(self.catalogue_models):
            return
        codes = self.pack_codes.GetValue().split()
        code = self.catalogue_models[index].code
        if code not in codes:
            codes.append(code)
        self.pack_codes.SetValue(' '.join(codes))
        self.pack_codes.SetFocus()

    def _pack_budget(self):
        value = self.pack_budget.GetValue().strip()
        if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 4096:
            raise OcrLanguageError('Choose a disk budget from 1 to 4096 MiB.')
        return int(value) * 1024**2

    def _selected_pack_profile(self):
        index = self.pack_profile.GetSelection()
        if index not in (0, 1):
            raise OcrLanguageError('Choose the fast or best official model profile.')
        return ('fast', 'best')[index]

    def prepare_pack(self, event=None):
        from ..extract.ocr_model_catalog import prepare_offline_pack
        try:
            source, destination = self.pack_source.GetValue().strip(), self.pack_prepared.GetValue().strip()
            codes = self.pack_codes.GetValue().split()
            profile = self._selected_pack_profile()
            budget = self._pack_budget()
            if not source or not destination or not codes:
                raise OcrLanguageError('Choose the local model source, explicit model codes, and a new prepared-pack folder.')
        except (ValueError, IndexError) as exc:
            self.report(str(exc))
            return

        def ready(result):
            self.pack_install_source.SetValue(result.path)
            self.pack_digest.SetValue(result.manifest_sha256)
            self.report(f'Prepared {len(result.models)} verified {result.profile} models in {result.path}. '
                f'Keep this manifest SHA256 separately: {result.manifest_sha256}. '
                'The complete upstream license is included. Review the checksum, choose a new installation folder, then Install reviewed pack.')
        self._pack_job('Verifying the selected local official models and preparing the pack…',
            lambda: prepare_offline_pack(source, destination, codes, profile=profile, max_bytes=budget,
                                         cancelled=self.cancel_event.is_set), ready)

    def install_pack(self, event=None):
        from ..extract.language_manager import install_offline_pack
        try:
            source = self.pack_install_source.GetValue().strip()
            destination, digest = self.pack_installed.GetValue().strip(), self.pack_digest.GetValue().strip()
            budget = self._pack_budget()
            if not source or not destination:
                raise OcrLanguageError('Choose a prepared pack and a new separate installation folder.')
        except ValueError as exc:
            self.report(str(exc))
            return

        def ready(result):
            self.pack_installed.SetValue(result['path'])
            self.pack_digest.SetValue(result['manifest_sha256'])
            self.report(f'Installed {result["models"]} verified models in {result["path"]}. '
                'Choose Use verified pack to bind this directory and checksum to the OCR settings. '
                'The portable installation was not modified.')
        self._pack_job('Installing the reviewed pack into the new directory…',
            lambda: install_offline_pack(source, destination, expected_manifest_sha256=digest,
                                         max_bytes=budget, cancelled=self.cancel_event.is_set), ready)

    def use_pack(self, event=None):
        from ..extract.language_manager import validate_pack_selection
        path, digest = self.pack_installed.GetValue().strip(), self.pack_digest.GetValue().strip()
        try:
            validate_pack_selection(path, digest)
        except ValueError as exc:
            self.report(str(exc))
            return

        def ready(catalog):
            if catalog.model_profile != 'installed':
                raise OcrLanguageError('The returned inventory is not the selected reviewed pack.')
            self._set_model_profile('installed')
            self.active_pack, self.catalog = (path, digest), catalog
            self.profile.SetSelection(0)
            self.filter_models()
            self._show_selected()
            self._presets()
            if hasattr(self, 'pages'):
                self.pages.SetSelection(0)
            self.filter.SetFocus()
            self.report('The verified offline pack is selected. Choose the recognition languages and order, '
                        'then Use settings. Every import rechecks the exact pack and engine.', focus=False)
        self._pack_job('Verifying the installed pack and checking its actual OCR model inventory…',
            lambda: discover_languages(model_pack_path=path, model_pack_sha256=digest,
                                       cancelled=self.cancel_event.is_set), ready, activation=True)

    def use_default_models(self, event=None):
        if self.busy:
            return
        self._set_model_profile('installed')
        self.active_pack = None
        self.profile.SetSelection(0)
        self.rescan()

    def stop_pack(self, event=None):
        self.cancel_event.set()
        self.report('Stopping the current OCR model operation…', focus=False)

    def choose_pack_directory(self, attribute, *, new=False):
        import wx
        if self.busy:
            return
        picker = wx.DirDialog(self, 'Choose the parent folder' if new else 'Choose a local model folder',
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            parent = picker.GetPath() if picker.ShowModal() == wx.ID_OK else None
        finally:
            picker.Destroy()
        if parent is None:
            return
        path = Path(parent)
        if new:
            prompt = wx.TextEntryDialog(self, 'Name the new folder; existing folders are never overwritten.',
                                        'New OCR pack folder')
            try:
                name = prompt.GetValue().strip() if prompt.ShowModal() == wx.ID_OK else None
            finally:
                prompt.Destroy()
            if name is None:
                return
            if not name or name in ('.', '..') or any(char in name for char in '\\/:') or Path(name).name != name:
                self.report('Enter a single new folder name.')
                return
            path /= name
        getattr(self, attribute).SetValue(str(path))
        getattr(self, attribute).SetFocus()

    def create_pack_page(self, notebook):
        """One canonical Fast/Best manager plus activation of existing reviewed packs."""
        import wx
        page = wx.ScrolledWindow(notebook)
        page.SetScrollRate(8, 8)
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(page, label=(
            'Manage official Fast and Best models in the shared model manager. '
            'Installation uses explicit local files and verifies their pinned upstream identities. '
            'Then choose Managed Fast or Managed Best on the Recognition languages page. '
            'Existing reviewed packs can still be selected below with their original manifest checksum.'))
        explanation.Wrap(690)
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        self.pack_controls = []
        manager = wx.Button(page, label='Manage official Fast and Best models...')
        manager.SetName('Manage official Fast and Best models')
        manager.Bind(wx.EVT_BUTTON, self.manage_models)
        self.pack_controls.append(manager)
        layout.Add(manager, 0, wx.ALL, 8)
        for label, attribute, value in (
                ('Existing reviewed pack folder', 'pack_installed', self.initial.model_pack_path or ''),
                ('Independently recorded manifest SHA256', 'pack_digest', self.initial.model_pack_sha256 or '')):
            layout.Add(wx.StaticText(page, label=label), 0, wx.LEFT | wx.RIGHT, 8)
            control = wx.TextCtrl(page, value=value)
            control.SetName(label)
            setattr(self, attribute, control)
            self.pack_controls.append(control)
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 8)
        for label, handler in (
                ('Choose existing reviewed pack...', lambda event: self.choose_pack_directory('pack_installed')),
                ('Use verified pack', self.use_pack),
                ('Use bundled or system models', self.use_default_models)):
            button = wx.Button(page, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            self.pack_controls.append(button)
            layout.Add(button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.pack_stop = wx.Button(page, label='Stop model verification')
        self.pack_stop.Bind(wx.EVT_BUTTON, self.stop_pack)
        self.pack_stop.Disable()
        layout.Add(self.pack_stop, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        page.SetSizer(layout)
        page.FitInside()
        return page


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
            self.active_model_profile = self.initial.model_profile
            self.active_pack = ((self.initial.model_pack_path, self.initial.model_pack_sha256)
                                if self.initial.model_pack_path else None)
            self.result = None
            self.preset_root = preset_root
            self.selected_codes = list(split_languages(self.initial.languages))
            self.catalog = None
            self.visible_codes, self.preset_items = (), ()
            self.busy = self.closed = False
            self.cancel_event = Event()
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ocr-models')
            outer = wx.BoxSizer(wx.VERTICAL)
            self.pages = wx.Notebook(self)
            self.pages.SetName('OCR languages and offline model packs')
            selection = wx.Panel(self.pages)
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(selection, label='Choose installed languages or script models for scanned pages. '
                'Filter, add models, then order them. Automatic language detection is not enabled. '
                'Originals stay unchanged and recognized text still requires review.')
            introduction.Wrap(730)
            layout.Add(introduction, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(selection, label='&Filter installed languages and scripts'), 0, wx.LEFT | wx.RIGHT, 8)
            self.filter = wx.TextCtrl(selection)
            self.filter.SetName('Filter installed languages and scripts')
            self.filter.Bind(wx.EVT_TEXT, self.filter_models)
            layout.Add(self.filter, 0, wx.EXPAND | wx.ALL, 8)
            lists = wx.BoxSizer(wx.HORIZONTAL)
            for label, attribute in [('Installed recognition models', 'available'),
                                     ('Selected models in recognition order', 'chosen')]:
                column = wx.BoxSizer(wx.VERTICAL)
                column.Add(wx.StaticText(selection, label=label), 0, wx.BOTTOM, 4)
                control = wx.ListBox(selection, size=(-1, 155))
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
                button = wx.Button(selection, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                actions.Add(button, 0, wx.RIGHT, 6)
            layout.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            profiles = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            profiles.AddGrowableCol(1)
            self.model_source = wx.Choice(selection, choices=['Bundled or system models',
                'Managed Fast models', 'Managed Best models'])
            self.model_source.SetSelection(MODEL_PROFILES.index(self.active_model_profile))
            self.model_source.Bind(wx.EVT_CHOICE, self.change_model_profile)
            self.profile = wx.Choice(selection, choices=['Selected models (identity recorded during import)',
                'Pinned tessdata_fast: English, Ukrainian and Slovak'])
            self.profile.SetSelection(0 if self.initial.model_policy == 'default' else 1)
            self.presets = wx.Choice(selection)
            for label, control in [('Model source', self.model_source),
                                   ('Verification policy', self.profile), ('Recognition presets', self.presets)]:
                control.SetName(label)
                profiles.Add(wx.StaticText(selection, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                profiles.Add(control, 1, wx.EXPAND)
            layout.Add(profiles, 0, wx.EXPAND | wx.ALL, 8)
            presets = wx.BoxSizer(wx.HORIZONTAL)
            for label, handler in [('Use &preset', self.apply_preset), ('&Save preset...', self.save_preset),
                                   ('D&elete saved preset...', self.remove_preset)]:
                button = wx.Button(selection, label=label)
                button.Bind(wx.EVT_BUTTON, handler)
                if preset_root is None and handler != self.apply_preset:
                    button.Disable()
                presets.Add(button, 0, wx.RIGHT, 6)
            layout.Add(presets, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.orientation = wx.CheckBox(selection, label='Check page &orientation with the installed OSD model')
            self.orientation.SetName('Check page orientation with the installed OSD model')
            self.orientation.SetValue(self.initial.orientation)
            layout.Add(self.orientation, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            selection.SetSizer(layout)
            self.pages.AddPage(selection, 'Recognition languages')
            self.pages.AddPage(self.create_pack_page(self.pages), 'Offline packs')
            outer.Add(self.pages, 1, wx.EXPAND | wx.ALL, 8)
            layout = outer
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

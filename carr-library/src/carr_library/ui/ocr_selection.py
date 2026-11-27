"""Explicit native selection of installed OCR languages and managed profiles.

Inventory is local. It is not language identification and does not execute OCR.
The trusted runtime's model listing runs off the UI thread with bounded output,
cancellation and a deadline. wx is imported only when showing a dialog.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryFile
from threading import Event
import time

from ..extract.ocr_languages import (model_ids, load_presets, store_preset, delete_preset)
from ..extract.ocr_process import prepare_ocr_query, normalize_ocr_query_output
from ..extract.page_ocr import Config
from ..extract.pinned_tessdata import MODELS, POLICY


PROFILES = (('installed', 'Models installed with the current OCR runtime'),
            ('fast', 'Managed Fast models'), ('best', 'Managed Best models'))
AUXILIARY = frozenset(('osd', 'equ'))
MAX_INVENTORY_BYTES = 128 * 1024


class OcrEngineMissingError(ValueError):
    """No selected/installed engine exists; distinct from a broken runtime."""


def parse_installed_models(data, *, expected_directory=None):
    """Parse a complete Tesseract listing without treating its text as paths."""
    if type(data) is not bytes or not 0 < len(data) <= MAX_INVENTORY_BYTES:
        raise ValueError('The installed OCR model listing exceeds its limit or is empty.')
    try:
        lines = data.decode('utf-8-sig').splitlines()
    except UnicodeError:
        raise ValueError('The installed OCR model listing is not valid UTF-8.') from None
    if not lines:
        raise ValueError('The installed OCR model listing is empty.')
    header = re.fullmatch(r'List of available languages in "([^"\r\n]+)" \(([0-9]+)\):', lines[0])
    if header is None:
        raise ValueError('The OCR runtime returned an unsupported model listing.')
    if expected_directory is not None and (
            os.path.normcase(os.path.abspath(header[1])) !=
            os.path.normcase(os.path.abspath(expected_directory))):
        raise ValueError('The OCR runtime reported a different model directory.')
    count = int(header[2])
    identifiers = tuple(line for line in lines[1:] if line)
    if (count != len(identifiers) or count > 1024 or len(set(identifiers)) != count
            or any(len(identifier) > 128 for identifier in identifiers)):
        raise ValueError('The OCR runtime returned an invalid model inventory.')
    for identifier in identifiers:
        if model_ids(identifier) != (identifier,):
            raise ValueError('The OCR runtime returned an invalid model identifier.')
    return identifiers


def discover_installed_models(*, cancelled=lambda: False, timeout=10.0):
    """Inspect the existing trusted engine, never probe CWD or change its data."""
    from ..extract.ocr_runtime import discover_ocr_runtime
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError('Invalid OCR inventory timeout.')
    deadline = time.monotonic() + timeout

    def remaining():
        if cancelled():
            raise InterruptedError('OCR model discovery cancelled.')
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('OCR model discovery timed out. Check the local OCR runtime and retry.')
        return seconds

    remaining()
    # The existing portable-engine contract includes English. System PATH
    # discovery does not require English, so other installed languages work.
    runtime = discover_ocr_runtime('eng', remaining=remaining)
    if runtime is None:
        raise OcrEngineMissingError('No local OCR runtime was found. Install or select the OCR runtime first.')
    with TemporaryFile() as output, TemporaryFile() as errors:
        remaining()
        command, cwd, report_directory = prepare_ocr_query(runtime, [runtime.executable, '--list-langs'])
        process = subprocess.Popen(command,
            stdin=subprocess.DEVNULL, stdout=output, stderr=errors, cwd=cwd,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                remaining()
                if (os.fstat(output.fileno()).st_size > MAX_INVENTORY_BYTES
                        or os.fstat(errors.fileno()).st_size > MAX_INVENTORY_BYTES):
                    raise ValueError('The OCR runtime exceeded its model-listing output limit.')
                try:
                    process.wait(timeout=min(0.05, remaining()))
                except subprocess.TimeoutExpired:
                    pass
            remaining()
            if (os.fstat(output.fileno()).st_size > MAX_INVENTORY_BYTES
                    or os.fstat(errors.fileno()).st_size > MAX_INVENTORY_BYTES):
                raise ValueError('The OCR runtime exceeded its model-listing output limit.')
            if process.returncode:
                raise ValueError('The OCR runtime could not list its installed models.')
            output.seek(0)
            data = normalize_ocr_query_output(output.read(MAX_INVENTORY_BYTES + 1), report_directory)
            identifiers = parse_installed_models(data, expected_directory=runtime.tessdata)
            runtime.assert_unchanged(remaining)
            remaining()
            return identifiers
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def selectable_models(profile, identifiers, *, initial=None):
    """Join trusted catalogue labels to exact installed IDs, excluding OSD/equ."""
    from ..extract.model_catalog import models
    if profile not in dict(PROFILES):
        raise ValueError('Choose an OCR model profile.')
    if isinstance(identifiers, (str, bytes)):
        raise ValueError('Invalid installed model inventory.')
    identifiers = tuple(identifiers)
    if (len(set(identifiers)) != len(identifiers) or len(identifiers) > 1024
            or any(model_ids(identifier) != (identifier,) for identifier in identifiers)):
        raise ValueError('Invalid installed model inventory.')
    catalogue = {row.identifier: row for kind in ('fast', 'best') for row in models(kind)}
    chosen_profile = {row.identifier: row for row in models(profile)} if profile != 'installed' else catalogue
    if profile != 'installed' and not set(identifiers) <= set(chosen_profile):
        raise ValueError('Installed models do not match the chosen profile catalogue.')
    rows = []
    for identifier in identifiers:
        model = chosen_profile.get(identifier)
        if identifier.casefold() in AUXILIARY or model is not None and model.kind == 'auxiliary':
            continue
        kind = model.kind if model is not None else 'script' if identifier.startswith('script/') else 'language'
        label = model.label if model is not None else identifier
        rows.append((identifier, f'{label} — {identifier} — {kind}'))
    rows.sort(key=lambda row: (row[1].casefold(), row[0]))
    # Preserve an existing language-priority order when reopening a selection.
    preferred = model_ids((initial or Config()).languages)
    rank = {identifier: index for index, identifier in enumerate(preferred)}
    rows.sort(key=lambda row: rank.get(row[0], len(rank)))
    return tuple(rows)


def selected_config(profile, identifiers, available, *, orientation=False, initial=None):
    """Validate final native choices and preserve existing resource budgets."""
    if profile not in dict(PROFILES):
        raise ValueError('Choose an OCR model profile.')
    if isinstance(identifiers, (str, bytes)) or isinstance(available, (str, bytes)):
        raise ValueError('Select one or more installed text languages or scripts.')
    chosen, available = tuple(identifiers), set(available)
    if (not chosen or len(set(chosen)) != len(chosen) or not set(chosen) <= available
            or any(model_ids(identifier) != (identifier,) for identifier in chosen)
            or any(identifier.casefold() in AUXILIARY for identifier in chosen)):
        raise ValueError('Select one or more installed text languages or scripts.')
    if type(orientation) is not bool or orientation and 'osd' not in available:
        raise ValueError('Install the osd model in this profile to enable orientation detection.')
    previous = initial or Config()
    policy = previous.model_policy
    if policy == POLICY and (profile == 'best' or not set(chosen) <= set(MODELS)):
        policy = 'default'
    return replace(previous, languages='+'.join(chosen), model_profile=profile,
                   orientation=orientation, model_policy=policy)


def _same_preset(root, expected):
    if type(expected) is not dict or not isinstance(expected.get('name'), str):
        raise ValueError('Choose a saved OCR preset.')
    row = next((row for row in load_presets(root)
                if row['name'].casefold() == expected['name'].casefold()), None)
    if row != expected:
        raise ValueError('The saved OCR preset changed or was deleted. Refresh the list and select it again.')
    return row


def verify_saved_preset(root, expected, *, initial=None, cancelled=lambda: False, timeout=30):
    """Fresh local checks before applying intent; no installation or download.

The shared store owns schema validation. Version 1 retains installed-model
meaning, exact order and policy. Model availability is checked again here,
including managed selected bytes and OSD, not inferred from a cached list.
"""
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise ValueError('Invalid saved OCR preset verification timeout.')
    deadline = time.monotonic() + timeout

    def remaining():
        if cancelled():
            raise InterruptedError('Saved OCR preset verification cancelled.')
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('Saved OCR preset verification timed out; current choices are unchanged.')
        return seconds

    remaining()
    row = _same_preset(root, expected)
    candidate = replace(initial or Config(), languages=row['languages'], orientation=row['orientation'],
                        model_policy=row['model_policy'], model_profile=row.get('model_profile', 'installed'))
    if candidate.model_profile == 'installed':
        available = discover_installed_models(cancelled=cancelled, timeout=min(10, remaining()))
    else:
        from ..extract.ocr_model_store import OcrModelStore
        store = OcrModelStore()
        available = store.available(candidate.model_profile)
        remaining()
        store.select(candidate.model_profile, candidate.languages, orientation=candidate.orientation,
                     remaining=remaining)
    result = selected_config(candidate.model_profile, model_ids(candidate.languages), available,
                             orientation=candidate.orientation, initial=candidate)
    selectable_models(candidate.model_profile, available, initial=result)
    remaining()
    _same_preset(root, row)
    remaining()
    return result, tuple(available)


def _preset_scope(parent, root=None):
    """Bind this dialog's preset writes to its original data folder and host."""
    owners = []
    for owner in (parent, getattr(parent, 'frame', None)):
        library = getattr(owner, 'library', None)
        if library is not None:
            owners.append((owner, library, str(Path(library.root).absolute())))
    if root is None and owners:
        root = owners[0][2]
    if root is None:
        return None, lambda: True
    root = Path(root).absolute()
    try:
        before = root.stat()
    except OSError:
        return root, lambda: False
    identity = before.st_dev, before.st_ino

    def current():
        try:
            now = root.stat()
            return ((now.st_dev, now.st_ino) == identity
                and all(getattr(owner, 'library', None) is library
                        and str(Path(library.root).absolute()) == path
                        and not getattr(owner, '_closing', False)
                        and not getattr(owner, 'closed', False)
                        for owner, library, path in owners))
        except (OSError, ValueError, AttributeError):
            return False
    return root, current


def _discover_with_dialog(parent, *, operation=None, status=None):
    import wx

    class InventoryDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Checking installed OCR models', size=(560, 190))
            self.cancelled = Event()
            self.closed = False
            self.alive = True
            self.result = self.error = None
            layout = wx.BoxSizer(wx.VERTICAL)
            self.status = wx.TextCtrl(self, value=status or 'Verifying the local OCR runtime and reading its model list…',
                                      style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('OCR model discovery status')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            self.cancel = wx.Button(self, wx.ID_CANCEL, '&Cancel')
            self.cancel.Bind(wx.EVT_BUTTON, self.request_cancel)
            layout.Add(self.cancel, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='carr-ocr-inventory')
            try:
                future = self.worker.submit(operation or discover_installed_models, cancelled=self.cancelled.is_set)
            except Exception:
                self.closed = True
                self.worker.shutdown(wait=False, cancel_futures=True)
                self.Destroy()
                raise
            future.add_done_callback(lambda done: None if self.closed else wx.CallAfter(self.finish, done))
            self.status.SetFocus()

        def finish(self, future):
            if self.closed:
                return
            try:
                result = future.result()
                if not self.cancelled.is_set():
                    self.result = result
            except InterruptedError:
                pass
            except Exception as exc:
                if not self.cancelled.is_set():
                    self.error = exc
            self.closed = True
            self.EndModal(wx.ID_OK if self.result is not None else wx.ID_CANCEL)

        def request_cancel(self, event=None):
            if self.closed or not self.alive:
                return
            self.cancelled.set()
            self.cancel.Enable(False)
            self.status.SetValue('Cancellation requested. Waiting for the local check to stop.')

        def on_close(self, event):
            self.request_cancel()
            if event.CanVeto():
                event.Veto()
            else:
                self.closed = True
                event.Skip()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
                self.closed = True
                self.cancelled.set()
                if hasattr(self, 'worker'):
                    self.worker.shutdown(wait=False, cancel_futures=True)
            event.Skip()

    dialog = InventoryDialog()
    try:
        dialog.ShowModal()
        if dialog.error is not None:
            raise dialog.error
        return dialog.result
    finally:
        dialog.closed = True
        dialog.cancelled.set()
        dialog.worker.shutdown(wait=False, cancel_futures=True)
        if dialog.alive:
            dialog.Destroy()


def _choose_languages(parent, profile, available, initial, *, preset_root=None):
    import wx
    rows = selectable_models(profile, available, initial=initial)
    if not rows:
        raise ValueError('No text-language or script models are available in this profile. '
                         'Use the offline OCR model manager to install models first.')
    preset_root, scope_current = _preset_scope(parent, preset_root)

    class SelectionDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='OCR languages for this import', size=(720, 650),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.result = None
            self.alive = True
            self.rows, self.available, self.profile = rows, tuple(available), profile
            self.config_base = initial
            self.preset_rows = ()
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            layout = wx.BoxSizer(wx.VERTICAL)
            hint = wx.StaticText(self, label='Check only the languages or scripts used in these scanned pages. '
                                 'Space selects or clears a model. Multiple selections enable mixed-language OCR; '
                                 'they do not automatically identify the document language.')
            hint.Wrap(660)
            layout.Add(hint, 0, wx.EXPAND | wx.ALL, 8)
            self.entries = wx.CheckListBox(self, choices=[row[1] for row in rows])
            self.entries.SetName('Installed OCR languages and scripts; Space selects or clears a model')
            layout.Add(self.entries, 1, wx.EXPAND | wx.ALL, 8)
            preferred = set(model_ids(initial.languages))
            checked = [index for index, row in enumerate(rows) if row[0] in preferred]
            if not checked:
                checked = [next((index for index, row in enumerate(rows) if row[0] == 'eng'), 0)]
            for index in checked:
                self.entries.Check(index)
            self.entries.SetSelection(checked[0])
            self.orientation = wx.CheckBox(self, label='Check page &orientation using the installed osd model')
            self.orientation.SetName('Page orientation detection; requires the osd model in this profile')
            self.orientation.SetValue(initial.orientation and 'osd' in available)
            self.orientation.Enable('osd' in available)
            layout.Add(self.orientation, 0, wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='Saved OCR presets in this library data folder'),
                       0, wx.LEFT | wx.RIGHT, 8)
            self.presets = wx.Choice(self)
            self.presets.SetName('Saved OCR presets with profile, language order and orientation')
            layout.Add(self.presets, 0, wx.EXPAND | wx.ALL, 8)
            preset_buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.preset_buttons = []
            for label, handler in (('&Save preset...', self.save_preset),
                                   ('&Load preset', self.load_preset), ('&Delete preset', self.remove_preset)):
                button = wx.Button(self, label=label)
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, handler)
                button.Enable(preset_root is not None)
                self.preset_buttons.append(button)
                preset_buttons.Add(button, 0, wx.ALL, 5)
            layout.Add(preset_buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 3)
            self.status = wx.TextCtrl(self, value=self.profile_status(), style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('OCR language selection status and errors')
            self.status.SetMinSize((-1, 56))
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            use = wx.Button(self, wx.ID_OK, '&Use selected models')
            use.Bind(wx.EVT_BUTTON, self.accept)
            use.SetDefault()
            buttons.Add(use, 0, wx.ALL, 8)
            buttons.Add(wx.Button(self, wx.ID_CANCEL, '&Cancel'), 0, wx.ALL, 8)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((580, 520))
            self.SetEscapeId(wx.ID_CANCEL)
            self.refresh_presets()
            self.entries.SetFocus()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
            event.Skip()

        def profile_status(self):
            return (f'Profile: {self.profile}. Orientation detection does not identify languages.'
                    if 'osd' in self.available else
                    f'Profile: {self.profile}. Orientation detection is unavailable because osd is not installed.')

        def report(self, message, *, focus=True):
            if self.alive:
                self.status.SetValue(message)
                if focus:
                    self.status.SetFocus()

        def require_scope(self):
            if not self.alive or not scope_current():
                raise ValueError('The library data folder changed. Reopen OCR model selection.')

        def current_config(self):
            self.require_scope()
            checked = tuple(row[0] for index, row in enumerate(self.rows) if self.entries.IsChecked(index))
            return selected_config(self.profile, checked, self.available,
                orientation=self.orientation.GetValue(), initial=self.config_base)

        def refresh_presets(self, name=None):
            if not self.alive:
                return
            try:
                self.require_scope()
                self.preset_rows = load_presets(preset_root) if preset_root is not None else ()
                self.presets.SetItems([f"{row['name']} — {row.get('model_profile', 'installed')} — "
                    f"{row['languages']}; orientation {'on' if row['orientation'] else 'off'}"
                    for row in self.preset_rows])
                chosen = next((index for index, row in enumerate(self.preset_rows) if row['name'] == name),
                              0 if self.preset_rows else -1)
                self.presets.SetSelection(chosen)
                if preset_root is None:
                    self.report(self.profile_status() + ' Open a library data folder to save presets.', focus=False)
            except (ValueError, OSError) as exc:
                self.preset_rows = ()
                self.presets.SetItems([])
                self.report(self.profile_status() + ' Saved presets are unavailable and were retained unchanged: '
                            + str(exc) + ' Manual model selection remains available.', focus=False)

        def selected_preset(self):
            self.require_scope()
            if preset_root is None:
                raise ValueError('Open a library data folder before using saved OCR presets.')
            index = self.presets.GetSelection()
            if not 0 <= index < len(self.preset_rows):
                raise ValueError('Choose a saved OCR preset first.')
            return _same_preset(preset_root, self.preset_rows[index])

        def confirm(self, title, message):
            question = wx.MessageDialog(self, message, title,
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
            try:
                accepted = question.ShowModal() == wx.ID_YES
                self.require_scope()
                return accepted
            finally:
                if self.alive:
                    question.Destroy()

        def save_preset(self, event=None):
            try:
                if preset_root is None:
                    raise ValueError('Open a library data folder before saving OCR presets.')
                config = self.current_config()
                prompt = wx.TextEntryDialog(self, 'Name this profile, ordered model selection and orientation setting.',
                                            'Save OCR preset')
                try:
                    answer = prompt.ShowModal()
                    name = prompt.GetValue().strip() if self.alive and answer == wx.ID_OK else None
                finally:
                    if self.alive:
                        prompt.Destroy()
                if name is None:
                    return
                self.require_scope()
                try:
                    store_preset(preset_root, name, config)
                except FileExistsError:
                    if not self.confirm('Replace OCR preset', 'Replace the saved OCR preset named ' + name + '?'):
                        return
                    store_preset(preset_root, name, config, replace=True)
                self.refresh_presets(name)
                self.report('Saved OCR preset ' + name + '. ' + self.profile_status())
            except (ValueError, OSError) as exc:
                self.report(str(exc))

        def load_preset(self, event=None):
            try:
                row = self.selected_preset()
                selected = dict(row)
                base = self.config_base
                result = _discover_with_dialog(self,
                    operation=lambda *, cancelled: verify_saved_preset(preset_root, selected, initial=base,
                        cancelled=lambda: cancelled() or not self.alive or not scope_current()),
                    status='Checking the saved preset and its actual local model files. No models will be downloaded.')
                if result is None:
                    return
                self.require_scope()
                _same_preset(preset_root, selected)
                config, found = result
                prepared_rows = selectable_models(config.model_profile, found, initial=config)
                # Only now replace the authoritative controls. Failed, stale or
                # cancelled checks leave every current choice untouched.
                self.rows, self.available, self.profile = prepared_rows, tuple(found), config.model_profile
                self.config_base = config
                self.entries.SetItems([item[1] for item in self.rows])
                chosen = set(model_ids(config.languages))
                for index, item in enumerate(self.rows):
                    self.entries.Check(index, item[0] in chosen)
                self.entries.SetSelection(0)
                self.orientation.Enable('osd' in found)
                self.orientation.SetValue(config.orientation)
                self.report('Loaded ' + selected['name'] + '. ' + self.profile_status()
                            + ' Review the model order, then use the selected models.', focus=False)
                self.entries.SetFocus()
            except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.report(str(exc))

        def remove_preset(self, event=None):
            try:
                row = self.selected_preset()
                if not self.confirm('Delete OCR preset', 'Delete the saved OCR preset named ' + row['name']
                                    + '? Current model choices and installed files will be retained.'):
                    return
                _same_preset(preset_root, row)
                delete_preset(preset_root, row['name'])
                self.refresh_presets()
                self.report('Deleted the saved preset. Current model choices and installed files are unchanged.')
            except (ValueError, OSError) as exc:
                self.report(str(exc))

        def accept(self, event=None):
            try:
                self.result = self.current_config()
                self.EndModal(wx.ID_OK)
            except ValueError as exc:
                self.report(str(exc))

    dialog = SelectionDialog()
    try:
        return dialog.result if dialog.ShowModal() == wx.ID_OK else None
    finally:
        if dialog.alive:
            dialog.Destroy()


def choose_model_config(parent, *, initial=None, preset_root=None):
    """Choose a local profile and explicit languages; return None on cancellation."""
    import wx
    initial = initial or Config()
    chooser = wx.SingleChoiceDialog(parent, 'Choose the OCR model set for this import.',
                                    'OCR model profile', [label for _, label in PROFILES])
    try:
        chooser.SetSelection(next(i for i, row in enumerate(PROFILES) if row[0] == initial.model_profile))
        if chooser.ShowModal() != wx.ID_OK:
            return None
        index = chooser.GetSelection()
        if not 0 <= index < len(PROFILES):
            raise ValueError('Choose a valid OCR model profile.')
        profile = PROFILES[index][0]
    except ValueError as exc:
        wx.MessageBox(str(exc), 'OCR model selection', wx.OK | wx.ICON_ERROR, parent)
        return None
    finally:
        chooser.Destroy()
    try:
        if profile == 'installed':
            available = _discover_with_dialog(parent)
            if available is None:
                return None
        else:
            from ..extract.ocr_model_store import OcrModelStore
            available = OcrModelStore().available(profile)
        return _choose_languages(parent, profile, available, initial, preset_root=preset_root)
    except OcrEngineMissingError as exc:
        if profile != 'installed':
            wx.MessageBox(str(exc), 'OCR model selection', wx.OK | wx.ICON_ERROR, parent)
            return None
        question = wx.MessageDialog(parent,
            'No local OCR engine is available. You can continue extracting existing PDF text. '
            'Pages that need OCR cannot be recognized and remain unverified; the import may pause for review. '
            'Continuing does not approve extracted quotations.',
            'Continue without OCR?', wx.OK | wx.CANCEL | wx.ICON_INFORMATION)
        try:
            question.SetOKCancelLabels('Continue with text extraction', 'Cancel')
            return replace(initial, model_profile='installed') if question.ShowModal() == wx.ID_OK else None
        finally:
            question.Destroy()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        wx.MessageBox(str(exc), 'OCR model selection', wx.OK | wx.ICON_ERROR, parent)
        return None

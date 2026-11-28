"""Explicit native selection of installed OCR languages and managed profiles.

Inventory is local. It is not language identification and does not execute OCR.
The trusted runtime's model listing runs off the UI thread with bounded output,
cancellation and a deadline. wx is imported only when showing a dialog.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryFile
from threading import Event
import time

from ..extract.ocr_languages import model_ids, AUXILIARY_MODELS
from ..extract.ocr_process import prepare_ocr_query, normalize_ocr_query_output
from ..extract.page_ocr import Config, ExtendedConfig
from ..extract.pinned_tessdata import MODELS, POLICY


PROFILES = (('installed', 'Models installed with the current OCR runtime'),
            ('fast', 'Managed Fast models'), ('best', 'Managed Best models'))
AUXILIARY = AUXILIARY_MODELS
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


def selected_config(profile, identifiers, available, *, orientation=False, initial=None,
                    automatic_languages=None):
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
    automatic = previous.automatic_languages if automatic_languages is None else automatic_languages
    if type(automatic) is not bool:
        raise ValueError('Automatic language selection must be on or off.')
    policy = previous.model_policy
    if policy == POLICY and (profile == 'best' or not set(chosen) <= set(MODELS)):
        policy = 'default'
    values = asdict(previous)
    values.update(languages='+'.join(chosen), model_profile=profile,
                  orientation=orientation, model_policy=policy)
    if automatic or isinstance(previous, ExtendedConfig):
        return ExtendedConfig(**{**values, 'automatic_languages': automatic})
    return Config(**values)


def _discover_with_dialog(parent):
    import wx

    class InventoryDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Checking installed OCR models', size=(560, 190))
            self.cancelled = Event()
            self.closed = False
            self.result = self.error = None
            layout = wx.BoxSizer(wx.VERTICAL)
            self.status = wx.TextCtrl(self, value='Verifying the local OCR runtime and reading its model list…',
                                      style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('OCR model discovery status')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            self.cancel = wx.Button(self, wx.ID_CANCEL, '&Cancel')
            self.cancel.Bind(wx.EVT_BUTTON, self.request_cancel)
            layout.Add(self.cancel, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='carr-ocr-inventory')
            try:
                future = self.worker.submit(discover_installed_models, cancelled=self.cancelled.is_set)
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
        dialog.Destroy()


def _choose_languages(parent, profile, available, initial):
    import wx
    rows = selectable_models(profile, available, initial=initial)
    if not rows:
        raise ValueError('No text-language or script models are available in this profile. '
                         'Use the offline OCR model manager to install models first.')

    class SelectionDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='OCR languages for this import', size=(720, 560),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.result = None
            layout = wx.BoxSizer(wx.VERTICAL)
            hint = wx.StaticText(self, label='Check the local languages or scripts allowed for this import. '
                                 'Space selects or clears a model. Manual mode uses this exact selection. '
                                 'Automatic mode uses at most eight selected bootstrap models, then a pinned offline '
                                 'text detector chooses language-specific paragraph alternatives from that same pool. '
                                 'Uncertain or unsupported language results keep the first-pass candidate for review.')
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
            self.automatic = wx.CheckBox(self, label='Automatic &language detection and paragraph review (offline)')
            self.automatic.SetName('Automatic OCR language detection; bounded selected model pool')
            self.automatic.SetValue(initial.automatic_languages)
            layout.Add(self.automatic, 0, wx.ALL, 8)
            self.orientation = wx.CheckBox(self, label='Check page &orientation using the installed osd model')
            self.orientation.SetName('Page orientation detection; requires the osd model in this profile')
            self.orientation.SetValue(initial.orientation and 'osd' in available)
            self.orientation.Enable('osd' in available)
            layout.Add(self.orientation, 0, wx.ALL, 8)
            self.status = wx.TextCtrl(self, value=(
                f'Profile: {profile}. Orientation detection does not identify languages.' if 'osd' in available else
                f'Profile: {profile}. Orientation detection is unavailable because osd is not installed.'),
                style=wx.TE_READONLY | wx.TE_MULTILINE)
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
            self.SetMinSize((580, 420))
            self.SetEscapeId(wx.ID_CANCEL)
            self.entries.SetFocus()

        def accept(self, event=None):
            try:
                checked = tuple(row[0] for index, row in enumerate(rows) if self.entries.IsChecked(index))
                self.result = selected_config(profile, checked, available,
                    orientation=self.orientation.GetValue(), initial=initial,
                    automatic_languages=self.automatic.GetValue())
                self.EndModal(wx.ID_OK)
            except ValueError as exc:
                self.status.SetValue(str(exc))
                self.status.SetFocus()

    dialog = SelectionDialog()
    try:
        return dialog.result if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def choose_model_config(parent, *, initial=None):
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
        return _choose_languages(parent, profile, available, initial)
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

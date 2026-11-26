"""Check the installed OCR catalogue, CLI and actual native model-manager dialog.

Run with the installed interpreter: python -I scripts/windows_ocr_models_smoke.py.
Use --package-only for wheel/CLI checks without a display. --allow-other-platform
allows an explicitly labelled non-Windows native wx run; it proves no Windows
behavior. No model is installed or downloaded. Native event dispatch and focus
checks are not manual keyboard use, NVDA speech or OCR accuracy acceptance.
"""
import sys

sys.dont_write_bytecode = True

import argparse
import base64
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Timer
from time import monotonic, sleep
import traceback


PACKAGE_FILES = (
    'carr_library/extract/model_catalog.py',
    'carr_library/extract/ocr_model_store.py',
    'carr_library/extract/managed_ocr.py',
    'carr_library/extract/ocr_model_catalog.py',
    'carr_library/extract/ocr-model-catalog.json',
    'carr_library/extract/TESSDATA_FAST_LICENSE',
    'carr_library/extract/TESSDATA_BEST_LICENSE',
    'carr_library/ui/ocr_model_manager.py',
    'carr_library/__main__.py',
)


def _require(condition, name, checks):
    if not condition:
        raise AssertionError(name)
    checks.append(name)
    print('PASS:', name, flush=True)


def _network_guard():
    """Fail Python socket attempts in this read-only acceptance process."""
    attempts = []

    def deny(event, arguments):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'):
            attempts.append(event)
            raise RuntimeError('Network access is forbidden in the OCR model-manager smoke.')

    sys.addaudithook(deny)
    return attempts


def package_checks(root, checks):
    """Verify shipped bytes before importing their APIs; then run the actual CLI."""
    distribution = importlib.metadata.distribution('universal-research-library')
    files = {str(item).replace('\\', '/'): item for item in distribution.files or ()}
    digests, paths = {}, {}
    for name in PACKAGE_FILES:
        item = files.get(name)
        _require(item is not None and item.hash is not None and item.hash.mode == 'sha256',
                 'installed wheel records ' + name, checks)
        path = Path(distribution.locate_file(item)).resolve(strict=True)
        _require(any(part.casefold() in ('site-packages', 'dist-packages') for part in path.parts),
                 'installed file is outside the source checkout: ' + name, checks)
        raw = path.read_bytes()
        actual = base64.urlsafe_b64encode(sha256(raw).digest()).rstrip(b'=').decode('ascii')
        _require(actual == item.hash.value and len(raw) == item.size,
                 'installed wheel checksum and size match: ' + name, checks)
        digests[name] = sha256(raw).hexdigest()
        paths[name] = path

    from carr_library.extract import model_catalog
    from carr_library.ui import ocr_model_manager
    _require(Path(model_catalog.__file__).resolve() == paths['carr_library/extract/model_catalog.py']
             and Path(ocr_model_manager.__file__).resolve() == paths['carr_library/ui/ocr_model_manager.py'],
             'catalogue and UI imports resolve to the verified installed wheel', checks)
    _require(model_catalog.catalogue_sha256() == digests['carr_library/extract/ocr-model-catalog.json'],
             'catalogue API fingerprints the installed JSON bytes', checks)
    for profile in ('fast', 'best'):
        rows = model_catalog.models(profile)
        _require(len(rows) > 100 and {'ukr', 'script/Latin'} <= {row.identifier for row in rows},
                 profile + ' catalogue contains language and script models', checks)
        license_data = model_catalog.license_bytes(profile)
        _require(sha256(license_data).hexdigest()
                 == digests['carr_library/extract/TESSDATA_' + profile.upper() + '_LICENSE'],
                 profile + ' license matches both its reviewed Git blob and wheel record', checks)

    # Invoke the installed entrypoint under isolated Python, with no source path
    # insertion. The child guard also catches a future accidental network call.
    bootstrap = (
        "import runpy, sys\n"
        "attempts=[]\n"
        "def deny(event,args):\n"
        " if event in ('socket.connect','socket.getaddrinfo','socket.gethostbyname'):\n"
        "  attempts.append(event)\n"
        "  raise RuntimeError('Network is forbidden in the OCR CLI smoke')\n"
        "sys.addaudithook(deny)\n"
        "try:\n"
        " runpy.run_module('carr_library',run_name='__main__',alter_sys=True)\n"
        "finally:\n"
        " if attempts: raise RuntimeError('OCR CLI attempted network access')\n"
    )
    for profile, query, expected in (('fast', 'Ukrainian', 'ukr'), ('best', 'script/Latin', 'script/Latin')):
        result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap,
            '--list-ocr-models', '--ocr-profile', profile, '--ocr-model-search', query,
            '--ocr-model-store', str(root / 'cli model store')],
            cwd=root, capture_output=True, timeout=20)
        _require(result.returncode == 0,
                 profile + ' installed CLI exits successfully without downloads', checks)
        output = json.loads(result.stdout.decode('utf-8'))
        identifiers = {row['identifier'] for row in output['models']}
        _require(output['network_access'] is False and expected in identifiers
                 and output['revision'] == model_catalog.REVISIONS[profile]
                 and all(row['installed'] is False for row in output['models']),
                 profile + ' installed CLI reads the exact catalogue and empty private store', checks)
    _require(not tuple(root.iterdir()), 'catalogue and CLI inspection create no model or application files', checks)
    return digests


def native_checks(root, checks, *, allow_other_platform=False):
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.extract.model_catalog import REVISIONS, models
    from carr_library.extract.ocr_model_store import OcrModelStore
    from carr_library.ui.ocr_model_manager import create_ocr_model_manager

    _require(allow_other_platform or wx.Platform == '__WXMSW__',
             'native wx platform matches the requested acceptance platform', checks)
    failures = []

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    frame = dialog = None

    def pump_until(predicate, explanation, timeout=5):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError(explanation)
            gui.Yield()
            sleep(0.01)

    def focus_is(control):
        current = wx.Window.FindFocus()
        while current is not None:
            if current is control:
                return True
            current = current.GetParent()
        return False

    def event(control, kind, *, integer=None):
        action = wx.CommandEvent(kind.typeId, control.GetId())
        action.SetEventObject(control)
        if integer is not None:
            action.SetInt(integer)
        _require(control.GetEventHandler().ProcessEvent(action),
                 'native event reaches ' + control.GetName(), checks)
        gui.Yield()

    try:
        frame = wx.Frame(None, title='OCR smoke parent', size=(700, 350))
        parent_note = wx.TextCtrl(frame, style=wx.TE_MULTILINE)
        parent_note.SetName('Parent unsaved research note')
        original_note = 'Unsaved parent note — Українська нотатка; keep every character.\nSecond line.'
        parent_note.SetValue(original_note)
        gui.SetTopWindow(frame)
        frame.Show()
        frame.Raise()
        parent_note.SetFocus()
        pump_until(lambda: focus_is(parent_note), 'Parent note did not receive native focus.')
        dialog = create_ocr_model_manager(frame, OcrModelStore(root / 'native model store'))
        errors = []

        def scenarios():
            try:
                names = {
                    'profile': 'OCR model profile',
                    'search': 'Find OCR models by language, script or identifier',
                    'entries': 'OCR models; Space selects or clears a model',
                    'summary': 'Selected models and disk-size estimate',
                    'details': 'Selected model details and verified catalogue identity',
                    'source': 'Offline model source folder',
                    'status': 'OCR model installation status and errors',
                }
                _require(all(getattr(dialog, key).GetName() == value for key, value in names.items()),
                         'native model-manager controls expose readable accessibility names', checks)
                _require(isinstance(dialog.profile, wx.Choice) and isinstance(dialog.entries, wx.CheckListBox),
                         'model profile and selectable list use native wx controls', checks)
                _require(all(bool(getattr(dialog, name).GetWindowStyleFlag() & wx.TE_READONLY)
                             for name in ('summary', 'details', 'source', 'status')),
                         'model details, estimates, source and status are read-only', checks)
                dialog.search.SetFocus()
                pump_until(lambda: focus_is(dialog.search), 'Model search did not receive focus.')
                _require(dialog.entries.GetCount() == len(models('fast'))
                         and '0 models selected' in dialog.summary.GetValue(),
                         'native model manager starts with the full Fast catalogue and no selection', checks)

                def search(value):
                    dialog.search.ChangeValue(value)
                    event(dialog.search, wx.EVT_TEXT)
                    _require(dialog.search.GetValue() == value, 'native search readback preserves ' + value, checks)

                search('Ukrainian')
                _require(dialog.entries.GetCount() == 1 and 'ukr' in dialog.entries.GetString(0),
                         'Ukrainian search resolves the native ukr row', checks)
                dialog.entries.SetSelection(0)
                event(dialog.entries, wx.EVT_LISTBOX, integer=0)
                _require('Identifier: ukr' in dialog.details.GetValue()
                         and REVISIONS['fast'] in dialog.details.GetValue(),
                         'native details read back the selected language and exact revision', checks)
                dialog.entries.Check(0, True)
                event(dialog.entries, wx.EVT_CHECKLISTBOX, integer=0)
                _require('1 models selected' in dialog.summary.GetValue(),
                         'native checklist selection updates the spoken-text summary', checks)

                search('Latin')
                _require(any('script/Latin' in dialog.entries.GetString(i)
                             for i in range(dialog.entries.GetCount())),
                         'Latin search exposes the official script model', checks)
                _require('1 outside the current search' in dialog.summary.GetValue(),
                         'filtering retains and discloses the hidden Ukrainian selection', checks)
                event(dialog.select_button, wx.EVT_BUTTON)
                pump_until(lambda: focus_is(dialog.entries), 'Select shown did not restore list focus.')
                _require(all(dialog.entries.IsChecked(i) for i in range(dialog.entries.GetCount())),
                         'Select all shown checks actual visible native rows', checks)
                fast_summary = dialog.summary.GetValue()

                dialog.profile.SetSelection(1)
                event(dialog.profile, wx.EVT_CHOICE, integer=1)
                _require('0 models selected' in dialog.summary.GetValue()
                         and 'profile: best' in dialog.details.GetValue()
                         and REVISIONS['best'] in dialog.details.GetValue(),
                         'Best profile has separate selections and its own exact revision', checks)
                event(dialog.select_button, wx.EVT_BUTTON)
                best_summary = dialog.summary.GetValue()
                dialog.profile.SetSelection(0)
                event(dialog.profile, wx.EVT_CHOICE, integer=0)
                _require(dialog.summary.GetValue() == fast_summary,
                         'returning to Fast preserves all Fast selections and estimate', checks)
                dialog.profile.SetSelection(1)
                event(dialog.profile, wx.EVT_CHOICE, integer=1)
                _require(dialog.summary.GetValue() == best_summary,
                         'returning to Best preserves its independent selection', checks)
                event(dialog.clear_button, wx.EVT_BUTTON)
                _require('0 models selected' in dialog.summary.GetValue()
                         and not any(dialog.entries.IsChecked(i) for i in range(dialog.entries.GetCount())),
                         'Clear selection clears every Best selection', checks)
                dialog.profile.SetSelection(0)
                event(dialog.profile, wx.EVT_CHOICE, integer=0)
                _require(dialog.summary.GetValue() == fast_summary,
                         'clearing Best leaves Fast selections unchanged', checks)
                event(dialog.clear_button, wx.EVT_BUTTON)
                search('no-model-matches-this-fixture')
                _require(dialog.entries.GetCount() == 0 and 'No model matches' in dialog.details.GetValue(),
                         'an empty native search has readable recovery guidance', checks)
                search('Latin')
                _require(dialog.entries.GetCount() > 0 and '0 models selected' in dialog.summary.GetValue(),
                         'native search recovers without restoring cleared selections', checks)
                _require(not dialog.pending and dialog.result is None and not dialog.cancel_button.IsEnabled()
                         and dialog.source.GetValue() == '',
                         'inspection has not started an installation or selected a source folder', checks)
                _require(parent_note.GetValue() == original_note,
                         'parent unsaved note is unchanged while the model dialog is open', checks)
                event(dialog.close_button, wx.EVT_BUTTON)
            except Exception:
                errors.append(traceback.format_exc())
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        def expired():
            if dialog.IsModal():
                errors.append('The native OCR model-manager modal loop timed out.')
                dialog.EndModal(wx.ID_CANCEL)

        watchdog = wx.CallLater(25000, expired)
        wx.CallAfter(scenarios)
        try:
            modal_result = dialog.ShowModal()
        finally:
            watchdog.Stop()
        if errors or failures:
            raise RuntimeError('\n'.join((*errors, *failures)))
        _require(modal_result == wx.ID_CANCEL and dialog.closed and not dialog.pending,
                 'Close completes the actual native modal without installing models', checks)
        dialog.executor.shutdown(wait=False, cancel_futures=True)
        dialog.Destroy()
        dialog = None
        gui.Yield()
        parent_note.SetFocus()
        pump_until(lambda: focus_is(parent_note), 'Parent note did not regain native focus.')
        _require(parent_note.GetValue() == original_note and frame.IsEnabled(),
                 'parent note and usable parent window survive dialog closure', checks)
        _require(not tuple(root.iterdir()), 'native model inspection creates no model or application files', checks)
        return wx.version(), wx.Platform
    finally:
        if dialog is not None:
            dialog.closed = True
            dialog.cancelled.set()
            dialog.executor.shutdown(wait=False, cancel_futures=True)
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)
            dialog.Destroy()
        if frame is not None:
            frame.Destroy()
        gui.Yield()
        gui.Destroy()
        # Keep the Windows runtime activation handle live until all controls close.
        del native_handle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-only', action='store_true', help='Verify installed wheel and CLI without opening wx')
    parser.add_argument('--allow-other-platform', action='store_true', help='Run native wx on a non-Windows test host')
    args = parser.parse_args(argv)
    if not args.package_only and os.name != 'nt' and not args.allow_other_platform:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required for the default GUI check.',
                          'native_gui': 'NOT_RUN', 'windows_execution': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        return 77

    def hard_timeout():
        print(json.dumps({'status': 'FAIL', 'reason': 'OCR model smoke exceeded its 60-second watchdog.',
                          'nvda': 'NOT_RUN'}), flush=True)
        os._exit(124)

    watchdog = Timer(60, hard_timeout)
    watchdog.daemon = True
    watchdog.start()
    checks = []
    attempts = _network_guard()
    try:
        with TemporaryDirectory(prefix='research-ocr-model-native-') as temporary:
            root = Path(temporary)
            digests = package_checks(root, checks)
            version = native_platform = None
            if not args.package_only:
                version, native_platform = native_checks(root, checks, allow_other_platform=args.allow_other_platform)
            _require(not attempts, 'no Python network operation was attempted', checks)
        print(json.dumps({'status': 'PASS', 'checks': len(checks), 'cases': checks,
            'installed_file_sha256': digests, 'wx_version': version, 'wx_platform': native_platform,
            'native_gui': 'NOT_RUN' if args.package_only else 'PASS',
            'windows_execution': 'PASS' if not args.package_only and os.name == 'nt' else 'NOT_RUN',
            'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN', 'models_installed': False,
            'models_downloaded': False, 'network_guard': 'Python socket operations denied'},
            ensure_ascii=False), flush=True)
        return 0
    except Exception:
        print(json.dumps({'status': 'FAIL', 'error': traceback.format_exc(), 'checks': checks,
                          'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        return 1
    finally:
        watchdog.cancel()


if __name__ == '__main__':
    raise SystemExit(main())

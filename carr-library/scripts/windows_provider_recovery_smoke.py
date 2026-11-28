"""Actual Windows provider settings import and passive recovery controls.

Synthetic nonsecret settings and ledger history only. Native controls and real
storage/export workers execute; picker/confirmation decisions are injected.
No vault, provider request, hardware audio or NVDA qualification is claimed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic
import traceback

MODULES = (
    'carr_library.providers', 'carr_library.provider_recovery',
    'carr_library.library_backup', 'carr_library.ui.api_connections',
    'carr_library.ui.export_destination', 'carr_library.research.export_file',
)


def module_identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('Native recovery must import the installed wheel: ' + name)
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def run(work):
    if os.name != 'nt' or not sys.flags.isolated:
        raise RuntimeError('Actual isolated Windows execution is required.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library import providers
    from carr_library import provider_recovery as recovery
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.ui.api_connections import show_api_connections

    work = Path(work).absolute()
    work.mkdir(mode=0o700)
    source = LibraryApp(work / 'source-library')
    workspace = source.create_workspace('Synthetic provider recovery')
    source.add_text(workspace, 'Незмінне джерело', 'Exact synthetic source. Український текст.')
    profile = providers.ProviderProfile('recovered', 'Recovered synthetic account',
        'openai-compatible', 'https://example.com/v1', 'reviewed-model', ('llm',),
        credential_ref='url-api-' + 'a' * 32)
    # This is nonsecret metadata only; no native credential is provisioned.
    (source.root / 'api-connections.json').write_text(json.dumps({
        'version': 1, 'profiles': [asdict(profile)]}), encoding='utf-8')
    ledger = providers.BudgetLedger(source.root / 'api-operations.sqlite')
    for index, state in enumerate(('complete', 'uncertain')):
        request = providers.ProviderRequest('llm', 'Synthetic prior request ' + str(index))
        approval = providers.JobApproval('job_' + str(index), profile.fingerprint,
            (request.fingerprint(profile),), budget_micros=20, mode='APPROVED_REMOTE')
        operation = 'operation_' + str(index)
        ledger.reserve(operation, approval, request.fingerprint(profile), 10)
        ledger.finish(operation, output_sha256='b' * 64 if state == 'complete' else None)

    checks, failures, messages = [], [], []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    snapshot = work / 'complete-backup'
    digest = create_backup(source, snapshot)
    manifest = verify_backup(snapshot, digest)
    retained = [row for row in manifest['sidecars'] if recovery.is_state_path(row['path'])]
    check('actual_complete_backup_retains_provider_record', len(retained) == 1)
    check('actual_complete_backup_verifies', manifest['kind'] == 'carr-library')
    restored = LibraryApp(restore_backup(snapshot, work / 'restored-library', digest))
    check('actual_restore_keeps_profiles_and_ledger_inactive',
          not (restored.root / 'api-connections.json').exists()
          and not (restored.root / 'api-operations.sqlite').exists())
    record_path, = recovery.state_paths(restored.root)
    record_raw, record = recovery.read_record(record_path)
    check('actual_recovery_binds_exact_disconnected_settings',
          record_path.stem == sha256(record_raw).hexdigest()
          and record['profiles']['profiles'][0]['credential_ref'] is None)
    check('actual_recovery_retains_exact_usage_states',
          [row['state'] for row in record['ledger']['operations']] == ['complete', 'uncertain'])
    check('actual_recovery_contains_no_credential_reference',
          b'url-api-' not in record_raw and b'access_token' not in record_raw)

    selected_profile = replace(profile, id='imported', name='Explicitly imported account', credential_ref=None)
    import_path, export_path = work / 'selected-profiles.json', work / 'history-export.json'
    import_path.write_text(json.dumps({'version': 1, 'profiles': [asdict(selected_profile)]}), encoding='utf-8')
    before_catalog = restored.browse(workspace)
    counters = {'vault': 0, 'requests': 0, 'confirmations': 0}
    original_vault, original_perform = providers.WindowsCredentialVault, providers.ProviderGateway.perform
    def unavailable_vault(*args, **kwargs):
        counters['vault'] += 1
        raise AssertionError('Passive recovery must not open the vault.')
    def unavailable_request(*args, **kwargs):
        counters['requests'] += 1
        raise AssertionError('Passive recovery must not send a provider request.')
    providers.WindowsCredentialVault, providers.ProviderGateway.perform = unavailable_vault, unavailable_request
    gui = wx.App(False)

    class Frame(wx.Frame):
        def __init__(self):
            super().__init__(None, title='Native provider recovery acceptance')
            self.library = restored
            self._closing = self.working = False
            self.cancel_event = Event()
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.future = None

        def report(self, message):
            messages.append(str(message))

        def run_job(self, title, action, done=None, failed=None):
            if self._closing or self.working:
                return False
            self.working = True
            self.cancel_event.clear()
            future = self.executor.submit(action)
            self.future = future
            def receive(completed):
                def apply():
                    self.working, self.future = False, None
                    try:
                        result = completed.result()
                        if done and not self._closing:
                            done(result)
                    except BaseException:
                        failures.append(traceback.format_exc())
                wx.CallAfter(apply)
            future.add_done_callback(receive)
            return True

    frame = Frame()
    frame.Show()
    gui.SetTopWindow(frame)
    picker, message_box = wx.FileDialog, wx.MessageBox
    picks = []
    class Picked:
        def __init__(self, parent, title, **kwargs):
            paths = {'Import profiles without API keys': import_path,
                     'Select passive provider recovery record': record_path,
                     'Export passive provider recovery history': export_path}
            if title not in paths:
                raise AssertionError('Unexpected native file selection: ' + title)
            self.path = paths[title]
            picks.append(title)
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            return str(self.path)
        def Destroy(self):
            pass
    decline_recovered = [True]
    def confirm(message, title, *args, **kwargs):
        counters['confirmations'] += 1
        if title == 'Review recovered provider settings' and decline_recovered[0]:
            decline_recovered[0] = False
            return wx.NO
        return wx.YES
    wx.FileDialog, wx.MessageBox = Picked, confirm
    timer, phase = wx.Timer(frame), [0]
    deadline = monotonic() + 75

    def child(dialog, name):
        control = wx.FindWindowByName(name, dialog)
        if control is None:
            raise AssertionError('Missing named native control: ' + name)
        return control

    def press(control):
        if not isinstance(control, wx.Button) or not control.IsEnabled():
            raise AssertionError('Native action is unavailable.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        if not control.GetEventHandler().ProcessEvent(event):
            raise AssertionError('Native action did not handle its event.')

    def dialogs():
        return [window for window in wx.GetTopLevelWindows()
                if isinstance(window, wx.Dialog) and window.IsShown() and window.IsModal()]

    def stop_dialogs():
        for window in reversed(dialogs()):
            window.EndModal(wx.ID_CANCEL)

    def drive(event):
        try:
            if failures:
                raise AssertionError(failures[0])
            if monotonic() > deadline:
                raise TimeoutError('Native provider controls exceeded their bounded wait.')
            windows = dialogs()
            connections = next((window for window in windows if window.GetTitle() == 'AI and API connections'), None)
            recovered = next((window for window in windows
                              if window.GetTitle() == 'Recovered provider settings and usage history'), None)
            store = providers.ProfileStore(restored.root / 'api-connections.json')
            if phase[0] == 0 and connections is not None:
                action = child(connections, 'Import profiles')
                check('native_import_action_named_and_available', action.IsEnabled())
                check('native_recovery_action_named_and_available',
                      child(connections, 'Recover saved provider settings').IsEnabled())
                action.SetFocus()
                phase[0] = 1
            elif phase[0] == 1 and connections is not None:
                if wx.Window.FindFocus() is not child(connections, 'Import profiles'):
                    return
                check('native_import_keyboard_focus', True)
                press(child(connections, 'Import profiles'))
                check('native_explicit_profile_import_completes',
                      store.profiles() == (selected_profile,) and counters['confirmations'] == 1)
                phase[0] = 2
            elif phase[0] == 2 and connections is not None:
                before = store.path.read_bytes()
                press(child(connections, 'Import profiles'))
                check('native_duplicate_import_keeps_existing_accounts', store.path.read_bytes() == before)
                phase[0] = 3
            elif phase[0] == 3 and connections is not None:
                press(child(connections, 'Recover saved provider settings'))
                phase[0] = 4
            elif phase[0] == 4 and recovered is not None:
                status = child(recovered, 'Passive provider settings and usage history; no restored authority')
                check('native_recovery_summary_is_readonly', not status.IsEditable())
                check('native_recovery_history_counts', 'Historical operations: 2' in status.GetValue()
                      and 'uncertain: 1' in status.GetValue())
                press(child(recovered, 'Import recovered settings'))
                check('native_recovery_import_requires_confirmation',
                      store.profiles() == (selected_profile,) and not decline_recovered[0])
                phase[0] = 5
            elif phase[0] == 5 and recovered is not None:
                press(child(recovered, 'Import recovered settings'))
                check('native_recovered_settings_import_disconnected',
                      {row.id for row in store.profiles()} == {'imported', 'recovered'}
                      and all(row.credential_ref is None for row in store.profiles()))
                check('native_recovery_keeps_history_inactive',
                      not (restored.root / 'api-operations.sqlite').exists())
                phase[0] = 6
            elif phase[0] == 6 and recovered is not None:
                press(child(recovered, 'Export recovery history'))
                phase[0] = 7
            elif phase[0] == 7 and not windows and not frame.working and export_path.exists():
                check('native_history_export_preserves_exact_bytes', export_path.read_bytes() == record_raw)
                check('native_all_storage_workers_settled', frame.future is None and bool(messages))
                phase[0] = 8
                timer.Stop()
                wx.CallAfter(gui.ExitMainLoop)
        except BaseException:
            failures.append(traceback.format_exc())
            timer.Stop()
            frame.cancel_event.set()
            stop_dialogs()
            wx.CallAfter(gui.ExitMainLoop)

    frame.Bind(wx.EVT_TIMER, drive, timer)
    timer.Start(50)
    def open_settings():
        try:
            show_api_connections(frame)
        except BaseException:
            failures.append(traceback.format_exc())
            wx.CallAfter(gui.ExitMainLoop)
    wx.CallAfter(open_settings)
    try:
        gui.MainLoop()
        if failures or phase[0] != 8:
            raise AssertionError('\n'.join(failures) or 'Native provider flow did not finish.')
        next_backup = work / 'complete-rebackup'
        next_digest = create_backup(restored, next_backup)
        next_manifest = verify_backup(next_backup, next_digest)
        next_records = [row for row in next_manifest['sidecars'] if recovery.is_state_path(row['path'])]
        check('actual_rebackup_preserves_prior_recovery_record',
              len(next_records) == 2 and any(row['sha256'] == sha256(record_raw).hexdigest() for row in next_records))
        check('actual_original_catalog_is_unchanged', restored.browse(workspace) == before_catalog)
        check('no_vault_or_provider_request_during_recovery', counters['vault'] == counters['requests'] == 0)
        check('native_selected_recovery_record_remains_immutable', record_path.read_bytes() == record_raw)
        metrics = {'disconnected_profiles': len(providers.ProfileStore(
            restored.root / 'api-connections.json').profiles()),
            'recovered_history_operations': len(record['ledger']['operations']),
            'recovery_records_after_rebackup': len(next_records),
            'provider_requests': counters['requests'], 'vault_calls': counters['vault'],
            'explicit_import_confirmations': counters['confirmations']}
        return {'status': 'PASS', 'actual_windows': True, 'isolated': True,
            'checks': checks, 'metrics': metrics, 'module_origins': module_identities(),
            'native_controls': True, 'selection_decisions': 'injected explicit synthetic file/confirmation choices',
            'actual_credential_vault': 'NOT_USED', 'actual_remote_provider': 'NOT_RUN',
            'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}
    finally:
        timer.Stop()
        frame._closing = True
        frame.cancel_event.set()
        stop_dialogs()
        frame.executor.shutdown(wait=True, cancel_futures=True)
        wx.FileDialog, wx.MessageBox = picker, message_box
        providers.WindowsCredentialVault, providers.ProviderGateway.perform = original_vault, original_perform
        frame.Destroy()
        wx.YieldIfNeeded()
        gui.Destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    owner = None
    report = None
    try:
        if sys.platform != 'win32' or not sys.flags.isolated:
            raise ValueError('Use an actual isolated installed Windows interpreter.')
        from carr_library.native_runtime import activate_bundled_runtime
        owner = activate_bundled_runtime()
        before = module_identities()
        report = run(args.work)
        if report['module_origins'] != before:
            raise ValueError('Installed provider module identities changed during execution.')
    except BaseException as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
            'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
            'nvda': 'NOT_RUN'}
    finally:
        if owner is not None:
            try:
                owner.close()
            except BaseException as error:
                report = {'status': 'FAIL', 'reason': 'Native-runtime cleanup: ' + str(error),
                    'nvda': 'NOT_RUN'}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

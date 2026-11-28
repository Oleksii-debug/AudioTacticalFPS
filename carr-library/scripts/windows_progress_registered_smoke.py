"""Actual Windows progress settings with reviewed library-local voice registrations.

The Piper manifest contains public synthetic nonexecutable assets. This probe
qualifies native selection, binding and passive recovery, not model inference,
audio-device output, listening quality or NVDA.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from time import monotonic
import traceback

MODULES = (
    'carr_library.progress', 'carr_library.progress_audio',
    'carr_library.runtime_settings', 'carr_library.audio.engines',
    'carr_library.audio.model_manifest', 'carr_library.ui.operation_progress',
    'carr_library.library_backup',
)


def identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in path.parts}:
            raise ValueError('The native progress probe imported checkout application code.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def installation(root, name):
    root.mkdir()
    assets = {'piper.exe': b'Public synthetic metadata fixture. Never execute.',
              'voice.onnx': b'Public synthetic model bytes. No inference.',
              'voice.json': b'{}'}
    for path, raw in assets.items():
        (root / path).write_bytes(raw)
    value = {'schema_version': 1, 'engine': 'piper', 'model_id': name,
             'revision': 'a' * 40, 'license': 'CC0-1.0 synthetic test bytes',
             'license_accepted': True, 'trusted_weights': True,
             'files': {path: sha256(raw).hexdigest() for path, raw in assets.items()},
             'settings': {'executable': 'piper.exe', 'model': 'voice.onnx',
                          'config': 'voice.json', 'language': 'uk', 'name': name}}
    path = root / 'manifest.json'
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def run(work):
    if sys.platform != 'win32' or not sys.flags.isolated:
        raise ValueError('Actual isolated installed Windows execution is required.')
    print('PHASE importing actual progress controls', flush=True)
    import wx
    from carr_library.application import LibraryApp
    from carr_library import progress, progress_audio
    from carr_library.runtime_settings import EngineProfileStore
    from carr_library.ui.operation_progress import OperationProgress, show_progress_settings
    from carr_library.library_backup import create_backup, restore_backup

    work.mkdir(mode=0o700)
    first, second = LibraryApp(work / 'first-library'), LibraryApp(work / 'second-library')
    first.create_workspace('First native progress scope')
    second.create_workspace('Second native progress scope')
    first_manifest = installation(work / 'first-profile', 'First registered metadata voice')
    second_manifest = installation(work / 'second-profile', 'Second registered metadata voice')
    first_store, second_store = EngineProfileStore(first.root), EngineProfileStore(second.root)
    a = first_store.register(first_manifest, name='First registered metadata voice')
    b = second_store.register(second_manifest, name='Second registered metadata voice')
    assets_before = {str(path): path.read_bytes()
                     for root in (first_manifest.parent, second_manifest.parent) for path in root.iterdir()}
    gui = wx.App(False)
    frame = wx.Frame(None, title='Registered progress voice acceptance', size=(720, 320))
    frame.library, frame.messages = first, []
    frame.report = frame.messages.append
    panel = wx.Panel(frame)
    gauge = wx.Gauge(panel, range=100)
    label = wx.StaticText(panel, label='Passive progress voice qualification')
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(gauge, 0, wx.ALL | wx.EXPAND, 8)
    layout.Add(label, 0, wx.ALL | wx.EXPAND, 8)
    panel.SetSizer(layout)
    view = OperationProgress(frame, gauge, label, wx_module=wx, start_timer=False)
    frame.Show()
    gui.SetTopWindow(frame)
    checks, errors, timers = [], [], []
    deadline = monotonic() + 150

    def check(name, valid):
        if not valid:
            raise AssertionError(name)
        checks.append(name)
        print('CHECK ' + name, flush=True)

    def descendants(parent):
        values = []
        for child in parent.GetChildren():
            values.append(child)
            values.extend(descendants(child))
        return values

    def named(parent, name):
        values = [child for child in descendants(parent) if child.GetName() == name]
        if len(values) != 1:
            raise AssertionError('Expected one named owned native control: ' + name)
        return values[0]

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Required native control is unavailable.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        print('ACTION ' + button.GetName(), flush=True)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The native command was not handled.')

    def close_modals():
        for window in reversed(wx.GetTopLevelWindows()):
            try:
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass

    def later(callback):
        def invoke():
            if errors:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Registered native progress controls exceeded their deadline.')
                callback()
            except Exception:
                errors.append(traceback.format_exc())
                print(errors[-1], file=sys.stderr, flush=True)
                close_modals()
        timers.append(wx.CallLater(25, invoke))

    def settings_window():
        values = []
        for window in wx.GetTopLevelWindows():
            if (not window or not isinstance(window, wx.Dialog) or window.IsBeingDeleted()
                    or not window.IsShown() or not window.IsModal()
                    or window.GetTitle() != 'Progress feedback'):
                continue
            if window.GetParent() is not frame:
                raise AssertionError('An active progress settings dialog belongs to another owner.')
            values.append(window)
        if len(values) > 1:
            raise AssertionError('Multiple native progress settings owners appeared.')
        return values[0] if values else None

    def select_profile(profile, *, cancel=False, change_library=None):
        state = {'stage': 'open', 'named': False, 'voice_choices': (), 'language_choices': (),
                 'selection_focus': False, 'changed_refusal': False}
        prefix = 'local-' + profile.id + ':'

        def advance():
            dialog = settings_window()
            if dialog is None:
                later(advance)
                return
            if state['stage'] == 'open':
                mode = named(dialog, 'Progress audio mode')
                interval = named(dialog, 'Notification interval in seconds, 0.5 to 60')
                volume = named(dialog, 'Notification volume, 0 to 1')
                choose = named(dialog, 'Choose local progress voice')
                status = named(dialog, 'Progress settings status')
                state['named'] = all(control.GetName() for control in (mode, interval, volume, choose, status))
                if cancel:
                    volume.ChangeValue('0.83')
                    state['stage'] = 'done'
                    press(wx.FindWindowById(wx.ID_CANCEL, dialog))
                    return
                state['stage'] = 'voice'
                press(choose)
            if state['stage'] == 'voice':
                candidates = [window for window in wx.GetTopLevelWindows()
                              if window and isinstance(window, wx.SingleChoiceDialog)
                              and not window.IsBeingDeleted() and window.IsShown() and window.IsModal()
                              and window.GetTitle() == 'Local progress voice' and window.GetParent() is dialog]
                if candidates:
                    if len(candidates) != 1:
                        raise AssertionError('Multiple native voice selectors appeared.')
                    selector = candidates[0]
                    lists = [child for child in descendants(selector) if isinstance(child, wx.ListBox)]
                    if len(lists) != 1:
                        raise AssertionError('The native voice selector does not expose one list.')
                    rows = tuple(lists[0].GetStrings())
                    matches = [index for index, row in enumerate(rows) if prefix in row]
                    if len(matches) != 1:
                        raise AssertionError('The exact registered voice is not uniquely selectable.')
                    selector.SetSelection(matches[0])
                    lists[0].SetFocus()
                    state['selection_focus'] = wx.Window.FindFocus() is lists[0]
                    state['voice_choices'] = rows
                    if change_library is not None:
                        frame.library = change_library
                        state['stage'] = 'changed'
                    else:
                        state['stage'] = 'language'
                    print('ACTION accept exact registered native voice', flush=True)
                    selector.EndModal(wx.ID_OK)
            elif state['stage'] == 'language':
                candidates = [window for window in wx.GetTopLevelWindows()
                              if window and isinstance(window, wx.SingleChoiceDialog)
                              and not window.IsBeingDeleted() and window.IsShown() and window.IsModal()
                              and window.GetTitle() == 'Progress notice language' and window.GetParent() is dialog]
                if candidates:
                    if len(candidates) != 1:
                        raise AssertionError('Multiple native language selectors appeared.')
                    selector = candidates[0]
                    lists = [child for child in descendants(selector) if isinstance(child, wx.ListBox)]
                    if len(lists) != 1:
                        raise AssertionError('Expected one native notice-language list.')
                    state['language_choices'] = tuple(lists[0].GetStrings())
                    selector.SetSelection(0)
                    state['stage'] = 'verified'
                    print('ACTION accept exact registered notice language', flush=True)
                    selector.EndModal(wx.ID_OK)
            elif state['stage'] == 'verified':
                status = named(dialog, 'Progress settings status').GetLabel()
                if 'Voice checked.' in status:
                    # Save metadata with audio Off: no synthetic model executes.
                    named(dialog, 'Progress audio mode').SetSelection(0)
                    state['stage'] = 'done'
                    press(wx.FindWindowById(wx.ID_OK, dialog))
                    return
            elif state['stage'] == 'changed':
                # The product must discard the old choice before a second
                # selector or a save can acquire the newly selected library.
                if not any(isinstance(window, wx.SingleChoiceDialog) and window.IsModal()
                           for window in wx.GetTopLevelWindows()):
                    press(wx.FindWindowById(wx.ID_OK, dialog))
                    state['changed_refusal'] = ('library changed' in
                        named(dialog, 'Progress settings status').GetLabel().lower())
                    state['stage'] = 'done'
                    press(wx.FindWindowById(wx.ID_CANCEL, dialog))
                    return
            if state['stage'] != 'done':
                later(advance)

        later(advance)
        result = show_progress_settings(view)
        if errors:
            raise AssertionError('\n'.join(errors))
        if state['stage'] != 'done':
            raise AssertionError('The native settings sequence did not finish.')
        state['result'] = result
        return state

    try:
        original_output = view.notifications._output
        check('native_progress_owner_bound_to_first_library',
              view.library is first and original_output._library_root == first.root
              and view.notifications._thread is None)
        print('PHASE select actual first-library registration in native controls', flush=True)
        one = select_profile(a)
        check('native_progress_settings_controls_named', one['named'])
        check('native_registered_voice_has_keyboard_focus', one['selection_focus'])
        check('native_first_library_registration_is_selectable',
              one['result'] is True and any('local-' + a.id + ':' in row for row in one['voice_choices'])
              and not any('local-' + b.id + ':' in row for row in one['voice_choices']))
        check('native_notice_language_selection_is_explicit', one['language_choices'] == ('Українська',))
        selected_a = progress.load_preferences(first.root)
        registry_a = progress_audio.configured_registry(first.root)
        check('native_saved_voice_matches_current_registration_fingerprint',
              selected_a == view.preferences and selected_a.voice_id.startswith('local-' + a.id + ':')
              and selected_a.voice_language == 'uk'
              and selected_a.voice_fingerprint == progress_audio.voice_fingerprint(
                  registry_a, selected_a.voice_id, 'uk'))
        check('native_synthetic_registration_did_not_enable_audio',
              selected_a.mode == 'off' and view.notifications._thread is None)
        first_raw = (first.root / progress.FILENAME).read_bytes()
        first_token = view._preferences_sha256
        cancelled = select_profile(a, cancel=True)
        check('native_cancel_keeps_exact_preferences_and_runtime',
              cancelled['result'] is False and (first.root / progress.FILENAME).read_bytes() == first_raw
              and view.preferences == selected_a and view._preferences_sha256 == first_token
              and view.notifications._output is original_output)

        frame.library = second
        view.change_root()
        check('native_library_change_rebinds_same_private_output',
              view.library is second and view.notifications._output is original_output
              and original_output._library_root == second.root and original_output._registry is None)
        print('PHASE select actual second-library registration', flush=True)
        two = select_profile(b)
        selected_b = progress.load_preferences(second.root)
        check('native_second_library_has_its_own_registered_choice',
              two['result'] is True and any('local-' + b.id + ':' in row for row in two['voice_choices'])
              and not any('local-' + a.id + ':' in row for row in two['voice_choices']))
        check('native_second_library_saves_separate_voice_metadata',
              selected_b.voice_id.startswith('local-' + b.id + ':')
              and selected_b.voice_id != selected_a.voice_id and selected_b.mode == 'off'
              and (first.root / progress.FILENAME).read_bytes() == first_raw)
        second_raw = (second.root / progress.FILENAME).read_bytes()
        second_token = view._preferences_sha256
        changed = select_profile(b, change_library=first)
        check('native_changed_owner_refuses_old_voice_selection',
              changed['result'] is False and changed['changed_refusal'] is True
              and changed['language_choices'] == ()
              and view.preferences == selected_b and view._preferences_sha256 == second_token
              and (second.root / progress.FILENAME).read_bytes() == second_raw)
        view.change_root()
        first_store.set_enabled(a.id, False)
        refused = False
        try:
            progress_audio.voice_fingerprint(
                progress_audio.configured_registry(first.root), selected_a.voice_id, 'uk')
        except ValueError:
            refused = True
        check('actual_disabled_registration_cannot_reuse_saved_selection',
              refused and (first.root / progress.FILENAME).read_bytes() == first_raw)
        backup = work / 'backup'
        digest = create_backup(first, backup)
        restored = restore_backup(backup, work / 'restored', digest)
        restored_profiles = EngineProfileStore(restored).snapshot().profiles
        refused = False
        try:
            progress_audio.voice_fingerprint(
                progress_audio.configured_registry(restored), selected_a.voice_id, 'uk')
        except ValueError:
            refused = True
        check('actual_restore_preserves_preferences_without_machine_activation',
              progress.load_preferences(restored) == selected_a and restored_profiles == () and refused)
        check('actual_original_voice_assets_unchanged',
              assets_before == {str(path): path.read_bytes()
                for root in (first_manifest.parent, second_manifest.parent) for path in root.iterdir()})
        view.close()
        if view._voice_executor is not None:
            view._voice_executor.shutdown(wait=True, cancel_futures=True)
        check('native_progress_selection_workers_settled',
              view.closed and view.notifications._thread is None
              and (view._voice_executor is None or not any(
                  thread.is_alive() for thread in view._voice_executor._threads)))
        return {'status': 'PASS', 'actual_windows': True, 'isolated': True,
                'native_controls': True, 'checks': checks,
                'metrics': {'registered_libraries': 2, 'selected_registered_voices': 2,
                            'restored_active_registrations': 0, 'notification_audio_threads': 0,
                            'changed_owner_refusals': 1},
                'module_origins': identities(), 'synthetic_registration_assets': True,
                'model_inference': 'NOT_RUN', 'model_quality': 'NOT_QUALIFIED',
                'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    finally:
        for timer in timers:
            timer.Stop()
        close_modals()
        view.close()
        frame.Destroy()
        wx.YieldIfNeeded()
        gui.Destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    faulthandler.enable()
    faulthandler.dump_traceback_later(200, exit=True)
    print('PHASE native registered-progress probe started', flush=True)
    handle = None
    try:
        if sys.platform != 'win32' or not sys.flags.isolated:
            raise ValueError('Actual isolated Windows execution is required.')
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        before = identities()
        report = run(args.work.absolute())
        if report['module_origins'] != before:
            raise ValueError('Installed progress implementation changed during qualification.')
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
                  'model_inference': 'NOT_RUN', 'model_quality': 'NOT_QUALIFIED',
                  'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native runtime cleanup: ' + str(error)}
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

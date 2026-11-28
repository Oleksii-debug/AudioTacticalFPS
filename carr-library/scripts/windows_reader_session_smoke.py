"""Installed Windows acceptance for explicit one-reader session controls.

The probe registers real User32 hotkeys and creates a real tray icon. It posts
WM_HOTKEY notifications to the owned native receiver; physical key presses are
not claimed. SAPI synthesis is real, while PCM playback reuses the explicitly
controlled device from the existing native live-reading probe.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def _helper(name, filename):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader import live_reading as live_core
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.speech import live_adapter
    from carr_library.ui import builtin_keyboard_commands, command_registry
    from carr_library.ui import desktop_app, live_reading, reader_background, reading_desk

    acceptance = _helper('_session_source_binding', 'windows_publishing_smoke.py')
    device_fixture = _helper('_session_controlled_pcm', 'windows_live_reading_smoke.py')
    modules = (reader_background, live_reading, live_core, reading_desk,
               builtin_keyboard_commands, command_registry, desktop_app, live_adapter)
    sources = acceptance.verify_installed_sources(*modules)
    checks, errors, renders, requests, lifetime = [], [], [], [], []
    holders, readers, parents, menus = [], [], [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if window and isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    gui.SetExitOnFrameDelete(False)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.UnregisterHotKey.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL
    modifiers = 0x0001 | 0x0002 | 0x0004
    next_identity = 0x4000
    frame = sentinel = watchdog = None
    registered = {}

    def pump(predicate, label, timeout=60):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError(label)
            gui.Yield()
            sleep(.01)

    def click(control):
        check('live native button: ' + control.GetName(), bool(control) and control.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        check('actual button handler: ' + control.GetName(), control.GetEventHandler().ProcessEvent(event))

    def find(parent, name):
        value = wx.Window.FindWindowByName(name, parent)
        check('named native control: ' + name, value is not None and bool(value))
        return value

    def reserve(key):
        nonlocal next_identity
        identity = next_identity
        next_identity += 1
        if next_identity >= 0x5000:
            raise RuntimeError('The acceptance fixture exhausted its own IDs.')
        if user32.RegisterHotKey(sentinel.GetHandle(), identity, modifiers | 0x4000, key):
            registered[identity] = key
            return identity
        return None

    def release(identity):
        check('owned native reservation released',
              identity in registered and bool(user32.UnregisterHotKey(sentinel.GetHandle(), identity)))
        del registered[identity]

    def available_keys(keys, label):
        for key in keys:
            identity = reserve(key)
            check(label + ': native key is available', identity is not None)
            release(identity)

    def settings_snapshot(root):
        # Reader positions change through their existing producer; this snapshot
        # covers only application/reader preference files, including any new file
        # someone might mistakenly persist below either typed settings subtree.
        paths = []
        for name in ('state/ui', 'state/speech'):
            directory = root / name
            if directory.exists():
                paths.extend(path for path in directory.rglob('*') if path.is_file())
        paths.extend(root / name for name in ('progress-preferences.json', 'keyboard-shortcuts.json')
                     if (root / name).is_file())
        return {str(path.relative_to(root)): path.read_bytes() for path in paths}

    def later_settings(callback):
        deadline = monotonic() + 20
        def locate():
            try:
                popups = [window for window in wx.GetTopLevelWindows()
                          if window and window.IsShown()
                          and window.GetTitle() == 'Tray and session hotkeys']
                if not popups:
                    if monotonic() >= deadline:
                        raise TimeoutError('The actual session-controls modal did not open.')
                    wx.CallLater(20, locate)
                    return
                popup, = popups
                check('actual session-controls modal', popup.IsModal())
                callback(popup)
            except BaseException:
                errors.append(traceback.format_exc())
                for window in wx.GetTopLevelWindows():
                    if window and isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
        wx.CallLater(20, locate)

    def post(owner, action, received):
        native = owner.native
        mapping = dict(zip((name for name, _value in owner.options.bindings), native.keys))
        identity = mapping[action]
        shortcut = dict(owner.options.bindings)[action]
        key = 0x70 + int(shortcut.rsplit('F', 1)[1]) - 1
        before = received.count(action)
        check('post to the exact registered native receiver: ' + action,
              owner.enabled and native.panel_alive and bool(native.panel)
              and bool(user32.PostMessageW(native.panel.GetHandle(), 0x0312,
                                          identity, modifiers | (key << 16))))
        pump(lambda: received.count(action) == before + 1,
             'Actual WM_HOTKEY delivery: ' + action, 10)

    def observe(owner):
        received = []
        for (action, _shortcut), identity in zip(owner.options.bindings, owner.native.keys):
            def delivered(event, selected=action):
                received.append(selected)
                event.Skip()
            owner.native.panel.Bind(wx.EVT_HOTKEY, delivered, id=identity)
        return received

    def capture_callbacks(owner):
        return tuple(owner.native.keys.items())

    def late_callbacks(owner, callbacks, label):
        controls = owner.controls
        check(label + ': reader is actually deleted', not owner.dialog and controls.closed and owner.closed)
        forbidden, delivered = [], []
        originals = {}
        def denied(name):
            def invoke(*args, **kwargs):
                forbidden.append(name)
                raise AssertionError('A revoked session touched ' + name)
            return invoke
        # Only downstream calls are observed. The captured production closures
        # and their dispatch/ownership checks remain unmodified.
        for target, names in ((controls, ('pause', 'stop', '_report')),
                              (owner.dialog, ('Show', 'IsIconized', 'Iconize', 'Raise'))):
            for name in names:
                originals[(id(target), name)] = getattr(target, name)
                setattr(target, name, denied(name))
        try:
            for identity, handler in callbacks:
                def apply(selected=handler, native_id=identity):
                    selected(wx.CommandEvent(wx.EVT_HOTKEY.typeId, native_id))
                    delivered.append(native_id)
                wx.CallAfter(apply)
            pump(lambda: len(delivered) == len(callbacks), label + ': actual late CallAfter delivery', 10)
            check(label + ': revoked callbacks never touch reader controls', not forbidden)
        finally:
            for target, names in ((controls, ('pause', 'stop', '_report')),
                                  (owner.dialog, ('Show', 'IsIconized', 'Iconize', 'Raise'))):
                for name in names:
                    setattr(target, name, originals[(id(target), name)])
        return delivered

    check('real wx Windows runtime', wx.Platform == '__WXMSW__')
    check('all selected package modules come from the installed wheel', len(sources) == len(modules))

    temporary = TemporaryDirectory(prefix='resonance-native-reader-session-')
    try:
        library = LibraryApp(Path(temporary.name) / 'Library — Україна')
        root = library.root
        workspace = library.create_workspace('Explicit reader session acceptance')
        raw = 'This reader remains yours. Stop means stop.'
        book = library.add_text(workspace, 'Session controls synthetic source', raw, language='en')
        frame = desktop_app.DesktopFrame(library)
        holders.append(frame)
        frame.Show()
        gui.SetTopWindow(frame)
        sentinel = wx.Panel(frame)
        sentinel.Hide()
        held_callbacks = []
        selected_keys = []
        settings_before = settings_snapshot(root)

        def discover(**kwargs):
            check('actual voice discovery uses the selected library',
                  Path(kwargs['configuration_root']) == root)
            return live_adapter.discover_voices(**kwargs)

        def render(request, target, **kwargs):
            check('actual synthesis uses the selected library',
                  Path(kwargs['configuration_root']) == root)
            requests.append(request.text)
            result = live_adapter.render_speech(request, target, **kwargs)
            check('actual SAPI synthesis returned PCM',
                  request.voice_id.startswith('sapi5-') and result.duration_seconds > 0)
            renders.append({'voice_id': request.voice_id, 'sha256': result.sha256,
                            'duration_seconds': result.duration_seconds})
            return result

        def run_reader(label, destruction, *, play=False):
            nonlocal selected_keys
            parent = wx.Frame(frame, title='Session lifetime parent: ' + label)
            parent.library = library
            parent.keyboard_commands = frame.keyboard_commands
            parents.append(parent)
            parent.Show()
            fixture = device_fixture.ControlledPCMPlayer()
            desk = ReadingDesk(library, workspace, book['edition'])
            reader = reading_desk.ReadingDialog(parent, desk, ReadingState(root),
                guard=lambda: None,
                live_factory=lambda source, **kwargs: live_core.LiveReader(source,
                    discover=discover, render=render, player_factory=lambda: fixture, **kwargs))
            readers.append(reader)
            controls = reader.live
            native_id, destroyed = reader.dialog.GetId(), []
            context = {'owner': None, 'callbacks': (), 'finished': False}
            def on_destroy(event):
                if event.GetId() == native_id:
                    destroyed.append(event.GetId())
                event.Skip()
            reader.dialog.Bind(wx.EVT_WINDOW_DESTROY, on_destroy)

            def drive():
                try:
                    pump(lambda: reader.controller.current is not None and not reader.busy
                         and bool(controls._voices) and not controls.core.busy,
                         label + ': source and actual SAPI discovery')
                    check(label + ': actual modal source reader', reader.dialog.IsModal())
                    check(label + ': existing desktop owns native session receiver', controls._host is frame)
                    check(label + ': no automatic session owner', not hasattr(controls, '_background_controls'))
                    check(label + ': no automatic voice choice', controls.voice.GetSelection() == wx.NOT_FOUND)
                    check(label + ': source is exact', reader.controller.current.raw_text == raw)
                    if not selected_keys:
                        local = reader_background._local_shortcuts(controls)
                        for number in range(1, 12):
                            shortcut = 'Ctrl+Alt+Shift+F' + str(number)
                            if shortcut.casefold() in local:
                                continue
                            reader_background.SessionOptions(show=shortcut)
                            key = 0x70 + number - 1
                            identity = reserve(key)
                            if identity is not None:
                                release(identity)
                                selected_keys.append(key)
                            if len(selected_keys) == 3:
                                break
                        check('three explicitly selected native combinations are available',
                              len(selected_keys) == 3)
                    shortcuts = ['Ctrl+Alt+Shift+F' + str(key - 0x70 + 1) for key in selected_keys]
                    before = settings_snapshot(root)

                    def configure(popup):
                        check(label + ': tray defaults off', popup.tray.GetValue() is False)
                        check(label + ': global keys default off', popup.hotkeys.GetValue() is False)
                        check(label + ': every key defaults blank',
                              all(field.GetValue() == '' for field in popup.fields.values()))
                        popup.tray.SetValue(True)
                        popup.hotkeys.SetValue(True)
                        for action, shortcut in zip(('show', 'pause', 'stop'), shortcuts):
                            popup.fields[action].ChangeValue(shortcut)
                        if play:
                            blocker = reserve(selected_keys[1])
                            check('actual separate receiver owns conflicting second key', blocker is not None)
                            try:
                                click(find(popup, 'Apply for this reader'))
                                owner = controls._background_controls
                                check('real native collision revokes partial session',
                                      not owner.enabled and owner.native is None and owner.slot.owner is None)
                                check('collision is visibly reported', 'unavailable' in popup.notice.GetValue())
                                available_keys([selected_keys[0]], 'partial registration rollback')
                            finally:
                                release(blocker)
                        click(find(popup, 'Apply for this reader'))
                        owner = controls._background_controls
                        check(label + ': exact one-reader session enabled',
                              owner.enabled and owner.slot.owner is owner
                              and owner.core is controls.core and owner.dialog is reader.dialog)
                        check(label + ': actual native icon is installed',
                              owner.native.tray.IsIconInstalled())
                        check(label + ': receiver belongs to the existing desktop',
                              owner.native.panel.GetParent() is frame)
                        context['owner'] = owner
                        click(find(popup, 'Close'))

                    later_settings(configure)
                    click(controls.command_buttons['reader.session_controls'][0])
                    if errors:
                        raise RuntimeError('\n'.join(errors))
                    owner = context['owner']
                    check(label + ': explicit controls outlive their settings modal', owner is not None and owner.enabled)
                    check(label + ': session settings write no preference file', settings_snapshot(root) == before)
                    received = observe(owner)
                    for previous, callbacks in held_callbacks:
                        old_generation = controls._start_generation
                        late_callbacks(previous, callbacks, label + ': prior reader cannot acquire this one')
                        check(label + ': new owner is unchanged',
                              owner.enabled and owner.slot.owner is owner
                              and controls._start_generation == old_generation)
                    count_before = len(renders)
                    reader.dialog.Iconize(True)
                    pump(reader.dialog.IsIconized, label + ': minimize exact reader', 10)
                    post(owner, 'show', received)
                    pump(lambda: reader.dialog.IsShown() and not reader.dialog.IsIconized(),
                         label + ': Show restores exact reader', 10)
                    check(label + ': Show never starts speech',
                          len(renders) == count_before and not controls.core.busy)

                    menu = owner.native.tray.CreatePopupMenu()
                    menus.append(menu)
                    first_item = menu.GetMenuItems()[0]
                    command = wx.CommandEvent(wx.EVT_MENU.typeId, first_item.GetId())
                    check(label + ': actual tray Show handler', menu.ProcessEvent(command))
                    check(label + ': tray Show never starts speech',
                          len(renders) == count_before and not controls.core.busy)
                    menu.Destroy()
                    menus.remove(menu)

                    if play:
                        index = next(index for index, voice in enumerate(controls._voices)
                                     if voice.id.startswith('sapi5-'))
                        controls.voice.SetSelection(index)
                        controls.capabilities()
                        pump(lambda: controls.command_buttons['reader.speech_all'][0].IsEnabled(),
                             'Explicit selected voice admits reading')
                        click(controls.command_buttons['reader.speech_all'][0])
                        pump(lambda: controls.core.status().state == 'playing',
                             'Real SAPI synthesis reaches controlled PCM playback')
                        check('the same current source was rendered', requests == [raw])
                        reader.text.SetFocus()
                        pump(lambda: wx.Window.FindFocus() is reader.text, 'Reader text focus', 10)
                        fixture.advance(min(.05, fixture.duration / 4))
                        post(owner, 'pause', received)
                        pump(lambda: controls.core.status().state == 'paused', 'Pause reaches current owner')
                        paused = fixture.position
                        fixture.advance(.1)
                        check('controlled PCM remains paused', fixture.position == paused)
                        post(owner, 'pause', received)
                        pump(lambda: controls.core.status().state == 'playing' and fixture.resume_count == 1,
                             'Resume reaches the same player')
                        check('background pause/resume preserves text focus', wx.Window.FindFocus() is reader.text)
                        stale_menu = owner.native.tray.CreatePopupMenu()
                        menus.append(stale_menu)
                        post(owner, 'stop', received)
                        pump(lambda: not controls.core.busy and controls.core.status().state == 'stopped',
                             'Stop finishes the existing worker')
                        check('Stop releases actual WAV ownership',
                              fixture.stream is None and all(not path.exists() for path in fixture.opened))
                        check('background Stop preserves text focus', wx.Window.FindFocus() is reader.text)
                        generation = controls._start_generation
                        stale_stop = stale_menu.GetMenuItems()[2]
                        stale_menu.ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, stale_stop.GetId()))
                        check('stale tray popup cannot act on a later generation',
                              controls._start_generation == generation)
                        stale_menu.Destroy()
                        menus.remove(stale_menu)
                    context['callbacks'] = capture_callbacks(owner)
                    context['finished'] = True
                    if destruction == 'normal':
                        reader.close()
                    elif destruction == 'direct':
                        reader.dialog.Destroy()
                    else:
                        # This real C++ parent operation bypasses the
                        # Python NativeDialog.Destroy override.
                        parent.DestroyChildren()
                except BaseException:
                    errors.append(traceback.format_exc())
                    if reader.dialog and not reader.closed:
                        reader.close()
                    if reader.dialog and reader.dialog.IsModal():
                        reader.dialog.EndModal(wx.ID_CANCEL)

            wx.CallAfter(drive)
            reader.show()
            completion = controls.core.close()
            if completion is not None:
                completion.result(timeout=65)
            reader.executor.shutdown(wait=True, cancel_futures=True)
            if errors:
                raise RuntimeError('\n'.join(errors))
            pump(lambda: not reader.dialog and controls.closed,
                 label + ': actual native reader deletion', 10)
            owner = context['owner']
            check(label + ': exactly one owner destruction event', destroyed == [native_id])
            check(label + ': source cancellation remains scoped to disposed reader',
                  reader.closed and reader.cancelled.is_set() and desk.cancelled())
            pump(lambda: owner.native is None and owner.slot.owner is None,
                 label + ': confirmed native session cleanup', 10)
            check(label + ': completion was reached', context['finished'])
            available_keys(selected_keys, label + ': hotkeys released after reader closes')
            delivered = late_callbacks(owner, context['callbacks'], label)
            check(label + ': no new render after native deletion', len(renders) == 1)
            check(label + ': no retained playback stream', fixture.stream is None)
            held_callbacks.append((owner, context['callbacks']))
            lifetime.append({'mode': destruction, 'native_id': native_id,
                             'destruction_events': destroyed, 'late_callback_ids': delivered})
            if parent:
                parent.Destroy()
            gui.Yield()

        def expired():
            errors.append('Native session acceptance exceeded its total 240-second bound.')
            for window in wx.GetTopLevelWindows():
                if window and isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
        watchdog = wx.CallLater(240000, expired)
        run_reader('normal close with actual synthesis', 'normal', play=True)
        run_reader('direct Destroy', 'direct')
        run_reader('native parent DestroyChildren', 'parent')
        check('one actual synthesis for the explicitly requested read', len(renders) == 1)
        check('source bytes unchanged after all sessions',
              ReadingDesk(library, workspace, book['edition']).page(1).raw_text == raw)
        check('no saved session preferences', settings_snapshot(root) == settings_before)
        check('all real OS reservations released', not registered)
        check('single session slot is empty', reader_background._slot(wx).owner is None)
        check('installed module bytes remain exact', acceptance.verify_installed_sources(*modules) == sources)
        watchdog.Stop()
        watchdog = None
    finally:
        if watchdog is not None:
            watchdog.Stop()
        for menu in menus:
            if menu:
                menu.Destroy()
        for reader in readers:
            reader.dispose()
            if reader.dialog:
                reader.dialog.Destroy()
            completion = reader.live.core.close()
            if completion is not None:
                completion.result(timeout=65)
            reader.executor.shutdown(wait=True, cancel_futures=True)
            background = getattr(reader.live, '_background_controls', None)
            if background is not None and background.close() is not True:
                errors.append('Owned session cleanup remained unconfirmed.')
        if sentinel:
            for identity in tuple(registered):
                if user32.UnregisterHotKey(sentinel.GetHandle(), identity):
                    del registered[identity]
                else:
                    errors.append('Owned acceptance hotkey cleanup remained unconfirmed.')
            if not registered:
                sentinel.Destroy()
        for parent in parents:
            if parent:
                parent.Destroy()
        for holder in holders:
            holder.cancel_event.set()
            holder._closing = True
            holder.executor.shutdown(wait=True, cancel_futures=True)
            if holder:
                holder.Destroy()
        gui.Yield()
        # The synthetic library is removed only after every owned native/worker
        # cleanup attempt; Windows cannot delete a still-open playback file.
        temporary.cleanup()
    if errors:
        raise RuntimeError('\n'.join(errors))
    return {'status': 'PASS', 'checks': checks, 'installed_sources': sources,
            'native_hotkey_registration_collision_and_release': 'PASS',
            'native_notification_delivery': 'POSTED_WM_HOTKEY_TO_EXACT_REGISTERED_RECEIVER',
            'tray_icon_and_bound_menu_dispatch': 'PASS', 'one_existing_reader_owner': 'PASS',
            'actual_sapi_synthesis': renders, 'native_lifetime': lifetime,
            'player': 'CONTROLLED_PCM_TEST_FIXTURE', 'physical_global_keypresses': 'NOT_RUN',
            'physical_audio_device': 'NOT_RUN', 'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the installed Windows probe with Python -I -B -X utf8.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        result = _native()
    finally:
        if native_handle is not None:
            native_handle.close()
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n', encoding='utf-8')
    print(encoded, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

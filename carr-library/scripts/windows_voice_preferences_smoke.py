"""Installed native voice preferences with explicitly synthetic enumeration.

Real Windows controls, keys and the durable preference store are exercised.
No engine is discovered, configured or synthesized; this is not voice quality,
reader speech, human keyboard or NVDA acceptance.
"""
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.',
                          'native_keyboard': 'NOT_RUN', 'speech_synthesis': 'NOT_RUN'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        return run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech.runtime import Voice
    import carr_library.speech.voice_preferences as preferences
    import carr_library.ui.voice_preferences as ui
    import carr_library.ui.speech_dialog as reader

    checks, errors, identities, evidence = [], [], {}, {}

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label); print('PASS: ' + label, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve()
                 if os.environ.get('CARR_PROGRAM_DIR') else Path(sysconfig.get_path('purelib')).resolve())
    for module in (preferences, ui, reader):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, actual.is_relative_to(installed)
              and not actual.is_relative_to(checkout) and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest

    frame, current, owner_loop = None, None, None
    timers, held = [], [None]
    completed = False

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    app = ProbeApp(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    check('actual native Windows wx controls', wx.Platform == '__WXMSW__')
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        value = held[0]
        if value is not None:
            check('native key and modifiers released', simulator.KeyUp(*value))
            held[0] = None

    def abort(detail):
        errors.append(detail); print('NATIVE_VOICE_PREFERENCES_FAILURE: ' + detail, file=sys.stderr, flush=True)
        try:
            try:
                release()
            except Exception:
                errors.append(traceback.format_exc())
            # This probe owns one modal at a time. Closing it permits its
            # ShowModal to return before the retained outer loop exits.
            if current is not None:
                current.alive = False
                if current.IsModal():
                    current.EndModal(wx.ID_CANCEL)
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            loop = owner_loop if owner_loop is not None else app.GetMainLoop()
            if loop is None:
                raise RuntimeError('The native voice-preference owner loop is unavailable.')
            loop.ScheduleExit(1)

    def later(callback, delay=25):
        def invoke():
            nonlocal owner_loop
            if owner_loop is None:
                owner_loop = app.GetMainLoop()
            if errors:
                return
            try:
                callback()
            except Exception:
                abort(traceback.format_exc())
        timer = wx.CallLater(delay, invoke)
        timers.append(timer)
        return timer

    def until(predicate, complete, *, timeout=10):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError('Native voice-preference stage did not finish.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        top, owner = wx.GetTopLevelParent(control), wintypes.DWORD()
        handle = user32.GetForegroundWindow()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = (owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        return ready

    def key(control, code, complete, modifiers=wx.MOD_NONE):
        def down():
            check('target owns native foreground and focus: ' + control.GetName(), foreground(control))
            held[0] = (code, modifiers)
            check('native key down accepted', simulator.KeyDown(code, modifiers))
            def up():
                release(); later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(name, complete):
        button = current.buttons[name]
        check('native voice preference action enabled: ' + name, isinstance(button, wx.Button) and button.IsEnabled())
        key(button, wx.WXK_SPACE, complete)

    def choose_ukrainian(complete):
        # The real Choice includes All, en-us, uk-ua in its declared order.
        check('declared language choices are exact', current.languages == ('en-us', 'uk-ua'))
        choice = current.language
        key(choice, wx.WXK_HOME,
            lambda: key(choice, wx.WXK_DOWN, lambda: key(choice, wx.WXK_DOWN, complete)))

    with TemporaryDirectory(prefix='resonance-native-voice-preferences-') as temporary:
        library = LibraryApp(Path(temporary) / 'library')
        library.create_workspace('Synthetic voice preference controls')
        store = preferences.VoicePreferenceStore(library.root)
        values = (
            Voice('fixture-en', 'English fixture', 'Synthetic enumeration only', 'en-US'),
            Voice('fixture-uk-a', 'Alpha Ukrainian fixture', 'Synthetic enumeration only', 'uk-UA'),
            Voice('fixture-uk-b', 'Bravo Ukrainian fixture', 'Synthetic enumeration only', 'uk-UA'),
        )
        selected_id = values[2].id
        original_preferences = None

        def guard():
            if frame is None or getattr(frame, '_closing', False) or frame.library is not library:
                raise ValueError('The owned native voice-preference library changed.')
            store.guard()

        def first_opened():
            check('opening preferences does not create saved choices', not store.path.exists())
            check('named native choices and status are present',
                  isinstance(current.language, wx.Choice) and isinstance(current.voices, wx.ListBox)
                  and current.language.GetName() == 'Declared language or dialect'
                  and current.status.GetName() == 'Voice preference status'
                  and bool(current.status.GetWindowStyleFlag() & wx.TE_READONLY))
            choose_ukrainian(select_nonfirst)

        def select_nonfirst():
            check('no default does not invent a selected voice',
                  current.key() == 'uk-ua' and current.selected() is None and not current.buttons['use'].IsEnabled())
            key(current.voices, wx.WXK_END, save_default)

        def save_default():
            check('real list key selected the non-first language voice', current.selected().id == selected_id)
            press('default', save_favorite)

        def save_favorite():
            saved, _ = store.load()
            check('real button durably saved the exact declared-language default',
                  dict(saved.defaults) == {'uk-ua': selected_id} and saved.favorites == ())
            press('favorite', first_saved)

        def first_saved():
            nonlocal original_preferences
            saved, _ = store.load()
            check('real button durably saved the selected favorite',
                  dict(saved.defaults) == {'uk-ua': selected_id} and saved.favorites == (selected_id,))
            check('favorite order retains exact selected identity',
                  current.rows[0].id == selected_id and current.selected().id == selected_id
                  and current.voices.GetString(0).startswith('Favorite; '))
            original_preferences = store.path.read_bytes()
            press('close', lambda: None)

        def reopened():
            check('reopening retains exact preference bytes', store.path.read_bytes() == original_preferences)
            choose_ukrainian(saved_default_selected)

        def saved_default_selected():
            check('saved default is selected only for its exact declared language',
                  current.key() == 'uk-ua' and current.selected().id == selected_id
                  and 'Saved default for uk-ua' in current.status.GetValue())
            check('reopening preferences does not implicitly apply a reader selection', current.selected_id is None)
            press('use', lambda: None)

        def missing_opened():
            choose_ukrainian(missing_selected)

        def missing_selected():
            check('unavailable default has no silent fallback to the available voice',
                  len(current.rows) == 1 and current.rows[0].id == values[1].id
                  and current.selected() is None and current.voices.GetSelection() == wx.NOT_FOUND
                  and not current.buttons['use'].IsEnabled())
            check('native status explains unavailable default and favorite',
                  'no fallback was selected' in current.status.GetValue()
                  and '1 saved favorite(s) are unavailable' in current.status.GetValue())
            check('unavailable defaults remain preserved for explicit review', store.path.read_bytes() == original_preferences)
            evidence['unavailable_default'] = 'NO_FALLBACK'
            key(current.language, wx.WXK_ESCAPE, lambda: None)

        def launch(index):
            nonlocal current, completed
            check('previous native modal returned before the next opens', current is None)
            print('STAGE: voice preference session ' + str(index + 1), flush=True)
            dialog = ui.create_voice_preferences(frame, library_root=library.root,
                voices=values if index < 2 else values[:2], guard=guard)
            current = dialog
            later((first_opened, reopened, missing_opened)[index])
            try:
                result = dialog.ShowModal()
                returned = dialog.selected_id
            finally:
                current = None
                dialog.alive = False
                dialog.Destroy()
                release()
            if errors:
                return
            if index == 1:
                check('explicit native Use returns the saved exact voice identity', result == wx.ID_OK and returned == selected_id)
                evidence['returned_voice_id'] = returned
            else:
                check('Close or Escape returns no implicit reader choice', result == wx.ID_CANCEL and returned is None)
            if index < 2:
                # Only schedule the next modal after ShowModal and Destroy
                # completed. No inactive outer dialog is closed under a child.
                later(lambda: launch(index + 1))
            else:
                check('all sessions preserved the explicitly saved preference bytes', store.path.read_bytes() == original_preferences)
                evidence['preferences_sha256'] = sha256(original_preferences).hexdigest()
                evidence['language_default'] = dict(store.load()[0].defaults)
                evidence['favorite_ids'] = list(store.load()[0].favorites)
                completed = True
                owner_loop.ScheduleExit(0)

        def timeout():
            raise TimeoutError('Native voice preferences exceeded their 120-second budget.')

        try:
            frame = wx.Frame(None, title='CARR native voice preference fixture', size=(820, 660))
            frame.library = library
            app.SetTopWindow(frame); frame.Show()
            later(timeout, 120000)
            later(lambda: launch(0), 75)
            app.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('normal native MainLoop completed all three preference sessions', completed)
        finally:
            try:
                release()
            finally:
                try:
                    for timer in timers:
                        try:
                            timer.Stop()
                        except Exception:
                            errors.append(traceback.format_exc())
                finally:
                    if frame is not None:
                        frame._closing = True
                        frame.Destroy(); app.Yield()
        if errors:
            raise RuntimeError('\n'.join(errors))
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'source_identity': identities,
            'evidence': evidence, 'native_keyboard': 'PASS', 'durable_preferences': 'PASS',
            'synthetic_voice_enumeration': 'EXPLICIT_FIXTURE_ONLY', 'voice_discovery': 'NOT_RUN',
            'speech_synthesis': 'NOT_RUN', 'voice_quality': 'NOT_RUN',
            'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)

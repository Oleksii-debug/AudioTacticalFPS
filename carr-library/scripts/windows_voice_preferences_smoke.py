"""Actual voice-preference controls through the existing native reader worker.

Only declared synthetic Voice metadata is supplied. No voice synthesizes audio,
no provider installation runs, and NVDA/acoustic quality remain NOT_RUN.
"""
from hashlib import sha256
import faulthandler
import importlib.util
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback

_spec = importlib.util.spec_from_file_location('_carr_voice_preference_acceptance_helpers',
    Path(__file__).with_name('windows_reading_presentation_smoke.py'))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
_release_key, _cleanup, _unwind = _helpers._release_key, _helpers._cleanup, _helpers._unwind


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(100, repeat=True, file=sys.stderr)
    native = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        return _native()
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _native():
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.speech.session import ReaderDraftStore
    import carr_library.runtime_settings as settings
    import carr_library.durable_state as durable_module
    import carr_library.speech.runtime as runtime_module
    import carr_library.speech.reading as reading_module
    import carr_library.speech.session as session_module
    import carr_library.speech.tabs as tabs_module
    import carr_library.ui.voice_preferences as preferences_module
    import carr_library.ui.speech_dialog as reader_module
    import carr_library.ui.builtin_keyboard_commands as commands_module
    import carr_library.ui.reading_shortcuts as shortcuts_module
    import carr_library.ui.worker_lifetime as lifetime_module

    checks, errors, identities, evidence = [], [], {}, {}
    stage = ['starting', monotonic()]
    held, loop, reentry, aborting = [None], [None], [False], [False]
    timer = frame = None
    deadline = monotonic() + 130
    provider_closed, renders = Event(), []
    close_count = [0]
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        if label not in checks:
            checks.append(label)
            print('PASS:', label, flush=True)
    def advance(label):
        stage[:] = [label, monotonic()]
        print('STAGE:', label, flush=True)
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False
    app = ProbeApp(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    def release():
        _release_key(simulator, held)
    def abort(detail):
        errors.append(detail)
        print('FAIL:', detail, flush=True)
        if aborting[0]:
            return
        aborting[0] = True
        if timer is not None:
            _cleanup((timer.Stop,), errors)
        _unwind(wx, frame, loop[0] or app.GetMainLoop(), release,
                lambda callback: wx.CallLater(20, callback), errors)
    def released():
        try:
            release()
        except Exception:
            abort(traceback.format_exc())
    def key(control, code, next_stage, modifiers=wx.MOD_NONE):
        if held[0] is not None:
            return
        top, owner = wx.GetTopLevelParent(control), wintypes.DWORD()
        foreground = user32.GetForegroundWindow()
        if foreground:
            user32.GetWindowThreadProcessId(foreground, ctypes.byref(owner))
        if (owner.value != os.getpid() or int(foreground or 0) != int(top.GetHandle())
                or wx.Window.FindFocus() is not control):
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
            return
        advance(next_stage)
        held[0] = code, modifiers
        if not simulator.KeyDown(code, modifiers):
            raise AssertionError('Native preference key press failed.')
        wx.CallLater(40, released)
    def named(name, parent):
        value = wx.FindWindowByName(name, parent)
        if value is None:
            raise AssertionError('Missing native control: ' + name)
        return value
    def window(title):
        return next((item for item in wx.GetTopLevelWindows()
                     if item and isinstance(item, wx.Dialog) and item.IsShown()
                     and item.GetTitle() == title), None)

    checkout = Path(__file__).resolve().parents[1]
    installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                 else Path(sysconfig.get_path('purelib')).resolve())
    for module in (settings, durable_module, runtime_module, reading_module, session_module,
                   tabs_module, preferences_module, reader_module, commands_module,
                   shortcuts_module, lifetime_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__,
              actual.is_relative_to(installed) and not actual.is_relative_to(checkout)
              and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest
    check('actual native Windows preference controls', wx.Platform == '__WXMSW__')

    class Provider:
        name = 'Synthetic voice declarations; no inference'
        def voices(self):
            return (Voice('native:uk', 'Ukrainian declaration', self.name, 'uk'),
                    Voice('native:sk', 'Slovak declaration', self.name, 'sk'))
        def render(self, *args, **kwargs):
            renders.append(True)
            raise AssertionError('Preference controls must not synthesize.')
        def close(self):
            close_count[0] += 1
            provider_closed.set()

    with TemporaryDirectory(prefix='resonance-voice-preferences-') as temporary:
        library = LibraryApp(Path(temporary) / 'Бібліотека 😀')
        library.create_workspace('Native voice preferences acceptance')
        state = library.root / 'state'
        drafts = ReaderDraftStore(library.root)
        original_registry = settings.voice_registry
        settings.voice_registry = lambda *args, **kwargs: VoiceRegistry([Provider()])
        frame = wx.Frame(None, title='Native reader voice preferences acceptance', size=(650, 400))
        frame.library, frame._closing = library, False
        frame.report = lambda value: print('READER:', value, flush=True)
        frame.Show()
        app.SetTopWindow(frame)

        def open_reader():
            if loop[0] is None:
                loop[0] = app.GetMainLoop()
            try:
                reader_module.show_speech_reader(frame, text='Voice preference metadata fixture.')
                release()
                if not errors:
                    advance('cleanup')
            except Exception:
                abort(traceback.format_exc())

        def tick_body():
            if errors or held[0] is not None:
                return
            if monotonic() > deadline or monotonic() - stage[1] > 25:
                raise TimeoutError('Native voice preferences stage timed out: ' + stage[0])
            if stage[0] == 'cleanup':
                if not provider_closed.is_set():
                    return
                check('reader cleanup settles the original provider once', close_count[0] == 1)
                check('preference workflow never synthesizes or plays audio', not renders)
                evidence['complete'] = True
                timer.Stop()
                loop[0].ScheduleExit(0)
                return
            reader = window('Voice reader and reading copy')
            preferences = window('Local voice preferences')
            if reader is None:
                return
            if stage[0] == 'starting':
                button = named('Voice preferences', reader)
                if not button.IsEnabled():
                    return
                check('opening preferences is silent and does not write',
                      not renders and not settings.settings_path(state).exists())
                key(button, wx.WXK_SPACE, 'opened')
            elif stage[0] == 'opened':
                if preferences is not None:
                    key(named('Add or remove favorite', preferences), wx.WXK_SPACE, 'favorite')
            elif preferences is not None:
                idle = named('Add or remove favorite', preferences).IsEnabled()
                if not idle:
                    return
                if stage[0] == 'favorite':
                    check('native favorite persists',
                          settings.load_voice_preferences(state)['favorites'] == ['native:uk'])
                    key(named('Save default for language', preferences), wx.WXK_SPACE, 'default')
                elif stage[0] == 'default':
                    check('native exact-language default persists',
                          settings.load_voice_preferences(state)['defaults'] == {'uk': 'native:uk'})
                    named('Voice profile name', preferences).ChangeValue('Нативний профіль')
                    named('Profile synthesis rate, minus ten to ten', preferences).SetValue(-2)
                    named('Profile volume, zero to one hundred', preferences).SetValue(55)
                    key(named('Save voice profile', preferences), wx.WXK_SPACE, 'profile_saved')
                elif stage[0] == 'profile_saved':
                    choice = named('Saved voice profiles', preferences)
                    choice.SetSelection(0)
                    event = wx.CommandEvent(wx.EVT_CHOICE.typeId, choice.GetId())
                    event.SetEventObject(choice)
                    choice.GetEventHandler().ProcessEvent(event)
                    key(named('Use voice profile', preferences), wx.WXK_SPACE, 'profile_applied')
                elif stage[0] == 'profile_applied':
                    saved = drafts.load()
                    check('native profile applies exact declared controls',
                          saved.voice_id == 'native:uk' and saved.rate == -2 and saved.volume == 55
                          and named('Synthesis rate, minus ten to ten', reader).GetValue() == -2
                          and named('Speech volume, zero to one hundred', reader).GetValue() == 55)
                    evidence['before'] = settings.settings_path(state).read_bytes()
                    evidence['status'] = named('Voice preference status', preferences).GetValue()
                    frame.library = SimpleNamespace(root=library.root)
                    key(named('Save default for language', preferences), wx.WXK_SPACE, 'revoked')
                elif stage[0] == 'revoked':
                    check('changed library refuses native save',
                          settings.settings_path(state).read_bytes() == evidence['before'])
                    check('changed library suppresses stale native notices',
                          named('Voice preference status', preferences).GetValue() == evidence['status'])
                    frame.library = library
                    key(named('Close voice preferences', preferences), wx.WXK_SPACE, 'preferences_closed')
            elif stage[0] == 'preferences_closed':
                check('closed modal has no pending worker callback',
                      named('Voice preferences', reader).IsEnabled())
                key(named('Reading copy (editable; Ctrl+S saves)', reader),
                    ord('S'), 'reader_saved', wx.MOD_CONTROL)
            elif stage[0] == 'reader_saved':
                check('actual reader save keeps selected voice without a project picker',
                      drafts.load().voice_id == 'native:uk'
                      and 'saved for recovery' in named('Speech and save status', reader).GetValue())
                key(wx.FindWindowById(wx.ID_CANCEL, reader), wx.WXK_SPACE, 'reader_closing')

        def tick(event=None):
            if reentry[0]:
                return
            reentry[0] = True
            try:
                tick_body()
            except Exception:
                abort(traceback.format_exc())
            finally:
                reentry[0] = False

        timer = wx.Timer(frame)
        frame.Bind(wx.EVT_TIMER, tick, timer)
        timer.Start(25)
        wx.CallAfter(open_reader)
        try:
            app.MainLoop()
        finally:
            def restore():
                settings.voice_registry = original_registry
            def destroy():
                if frame:
                    frame.library = library
                    frame._closing = True
                    frame.Destroy()
            _cleanup((timer.Stop, release, restore, destroy, app.Yield), errors)
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('actual reader preferences workflow and cleanup completed', evidence.get('complete', False))
    print(json.dumps({'result': 'PASS', 'checks': len(checks), 'check_names': checks,
        'source_identity': identities, 'native_voice_preferences': 'PASS',
        'actual_reader_worker': 'PASS', 'voice_inventory': 'SYNTHETIC_DECLARATIONS',
        'speech_engine': 'NOT_RUN', 'acoustic_quality': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'},
        ensure_ascii=True))
    return 0


if __name__ == '__main__':
    import threading
    def timeout():
        print('FAIL: native voice preferences watchdog expired during work or cleanup.', flush=True)
        os._exit(124)
    watchdog = threading.Timer(180, timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        raise SystemExit(main())
    finally:
        watchdog.cancel()

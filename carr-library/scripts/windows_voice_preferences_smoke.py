"""Native voice preferences: real controls, durable state and fresh metadata.

Synthetic declarations isolate the control/lifetime contract. This probe never
claims voice inference, pronunciation, hardware listening or NVDA qualification.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import faulthandler
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import time
import traceback


def _write_report(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + '\n',
                    encoding='utf-8')


def _module_evidence(args):
    checkout = Path(__file__).resolve().parents[1]
    if args.source:
        if args.expect_installed or args.source.resolve(strict=True) != checkout:
            raise ValueError('Use this exact source checkout or the installed wheel.')
        sys.path.insert(0, str(checkout / 'src'))
    import carr_library
    package = Path(carr_library.__file__).resolve(strict=True)
    owner = None
    if args.expect_installed:
        if not sys.flags.isolated or package.is_relative_to(checkout):
            raise RuntimeError('Installed acceptance requires -I and the actual wheel package.')
        owners = []
        for distribution in importlib.metadata.distributions(name='universal-research-library'):
            for member in distribution.files or ():
                if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                    continue
                try:
                    declared = Path(distribution.locate_file(member)).resolve(strict=True)
                except FileNotFoundError:
                    continue
                if declared == package:
                    owners.append(distribution)
        if len(owners) != 1:
            raise RuntimeError('Exactly one wheel RECORD must own the imported package.')
        owner = owners[0]
    elif package.parent != (checkout / 'src/carr_library').resolve(strict=True):
        raise RuntimeError('Source acceptance imported another package.')
    helper = checkout / 'scripts/windows_clipboard_reader_smoke.py'
    if not helper.is_file() or helper.stat().st_size > 128 * 1024:
        raise ValueError('The shared native provenance helper is missing or excessive.')
    namespace = runpy.run_path(str(helper))
    modules = [(name.replace('.', '/') + '.py', importlib.import_module('carr_library.' + name))
               for name in ('runtime_settings', 'speech.runtime', 'ui.voice_preferences',
                            'language_qualification.catalog')]
    return namespace['_module_evidence'](checkout, package.parent, modules,
        source_mode=not args.expect_installed, distribution=owner)


def _run(args, report):
    if os.name != 'nt':
        raise RuntimeError('This acceptance probe requires actual native Windows controls.')
    report['modules'] = _module_evidence(args)
    _write_report(args.report, report)
    import wx
    from carr_library import runtime_settings
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.ui import voice_preferences

    checks, errors, applied, futures = report['checks'], [], [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS ' + label, flush=True)
        _write_report(args.report, report)
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    app = App(False)
    app.SetExitOnFrameDelete(False)
    frame = wx.Frame(None, title='Local voice preference acceptance')
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    authorized, callback_count = [True], [0]
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='voice-preference-acceptance')
    main_thread, review_threads = get_ident(), []
    held, release = Event(), Event()
    block_inventory = [False]
    fresh_providers = []

    def guard():
        if not authorized[0]:
            raise ValueError('The active library changed.')

    def execute(work, done, *, on_error):
        future = executor.submit(work)
        futures.append(future)
        callback_count[0] += 1
        def deliver():
            try:
                try:
                    result = future.result()
                except Exception as exc:
                    on_error(exc)
                else:
                    done(result)
            finally:
                callback_count[0] -= 1
        future.add_done_callback(lambda _future: wx.CallAfter(deliver))
        return True

    def pump(predicate, message, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            app.Yield()
            time.sleep(.01)

    def find(window, name):
        result = wx.Window.FindWindowByName(name, window)
        check('native control: ' + name, result is not None)
        return result

    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(), 'Native preference focus')
        check('native Space down', simulator.KeyDown(wx.WXK_SPACE))
        try:
            app.Yield()
        finally:
            check('native Space up', simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()

    with TemporaryDirectory(prefix='native-voice-preferences-') as temporary:
        state = Path(temporary) / 'state'
        voices = (Voice('native:uk', 'Українська декларація', 'Synthetic metadata only', 'uk'),
                  Voice('native:sk', 'Slovenské údaje', 'Synthetic metadata only', 'sk'))
        inventory = [voices]
        class Provider:
            name = 'Native preference metadata fixture'
            def __init__(self, *, fresh=False):
                self.fresh, self.closed = fresh, False
            def voices(self):
                if self.fresh and block_inventory[0]:
                    held.set()
                    if not release.wait(5):
                        raise TimeoutError('The bounded native review barrier was not released.')
                return inventory[0]
            def fingerprint(self, _identity):
                return 'a' * 64
            def close(self):
                self.closed = True
        original_provider = Provider()
        registry = VoiceRegistry([original_provider])
        def fresh_registry():
            provider = Provider(fresh=True)
            fresh_providers.append(provider)
            return VoiceRegistry([provider])
        def review(preferences, **kwargs):
            review_threads.append(get_ident())
            return runtime_settings.review_voice_preference(state, registry, preferences,
                registry_factory=fresh_registry, **kwargs)
        selected = {'voice_id': voices[0].id, 'language': 'uk', 'rate': 0, 'volume': 80}
        def drive():
            try:
                window, = [item for item in wx.GetTopLevelWindows()
                           if isinstance(item, wx.Dialog) and item.IsShown() and item.IsModal()
                           and item.GetTitle() == 'Local voice preferences']
                check('opening preferences is silent and does not write',
                      not applied and not runtime_settings.settings_path(state).exists())
                press(find(window, 'Add or remove favorite'))
                pump(lambda: callback_count[0] == 0, 'Favorite save did not settle')
                check('native favorite persists', runtime_settings.load_voice_preferences(state)['favorites'] == ['native:uk'])
                press(find(window, 'Save default for language'))
                pump(lambda: callback_count[0] == 0, 'Default save did not settle')
                check('native exact-language default persists',
                      runtime_settings.load_voice_preferences(state)['defaults'] == {'uk': 'native:uk'})
                find(window, 'Voice profile name').ChangeValue('Нативний профіль')
                find(window, 'Profile synthesis rate, minus ten to ten').SetValue(-2)
                find(window, 'Profile volume, zero to one hundred').SetValue(55)
                press(find(window, 'Save voice profile'))
                pump(lambda: callback_count[0] == 0, 'Profile save did not settle')
                profile = find(window, 'Saved voice profiles')
                profile.SetSelection(0)
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, profile.GetId())
                event.SetEventObject(profile)
                profile.GetEventHandler().ProcessEvent(event)
                press(find(window, 'Use voice profile'))
                pump(lambda: callback_count[0] == 0, 'Profile review did not settle')
                check('native profile applies exact declared controls', applied == [{
                    'voice_id': 'native:uk', 'language': 'uk', 'rate': -2, 'volume': 55}])
                inventory[0] = voices[1:]
                press(find(window, 'Use voice profile'))
                pump(lambda: callback_count[0] == 0, 'Removed voice review did not settle')
                check('fresh inventory refuses a removed voice without substitution',
                      len(applied) == 1 and 'no substitute' in find(window, 'Voice preference status').GetValue())
                inventory[0] = voices
                original = runtime_settings.settings_path(state).read_bytes()
                authorized[0] = False
                press(find(window, 'Save default for language'))
                check('changed library refuses native save',
                      'library changed' in find(window, 'Voice preference status').GetValue()
                      and runtime_settings.settings_path(state).read_bytes() == original)
                authorized[0] = True
                block_inventory[0] = True
                press(find(window, 'Use voice profile'))
                pump(held.is_set, 'Actual review worker did not reach the owned barrier')
                check('review is pending before close', callback_count[0] == 1)
                press(find(window, 'Close voice preferences'))
                release.set()
            except Exception:
                errors.append(traceback.format_exc())
                release.set()
                for item in wx.GetTopLevelWindows():
                    if isinstance(item, wx.Dialog) and item.IsModal():
                        item.EndModal(wx.ID_CANCEL)

        def scenario():
            try:
                wx.CallLater(100, drive)
                voice_preferences.show_voice_preferences(frame, state_root=state, voices=voices,
                    preferences=runtime_settings.load_voice_preferences(state), selected=selected,
                    execute=execute, guard=guard, apply_selection=applied.append,
                    review_selection=review)
                pump(lambda: callback_count[0] == 0 and all(item.done() for item in futures),
                     'Native preference cleanup did not settle')
                check('closed modal has no pending worker callback', callback_count[0] == 0)
                check('close suppresses the queued selection', len(applied) == 1)
                check('fresh review stays off the GUI thread',
                      bool(review_threads) and all(item != main_thread for item in review_threads))
                check('every fresh registry closes while the reader registry remains borrowed',
                      fresh_providers and all(item.closed for item in fresh_providers)
                      and not original_provider.closed)
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                release.set()
                frame.Destroy()
                wx.CallAfter(app.ExitMainLoop)

        try:
            frame.Show()
            app.SetTopWindow(frame)
            wx.CallAfter(scenario)
            app.MainLoop()
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)
            registry.close()
    if errors:
        raise RuntimeError('\n'.join(errors))
    report.update(status='PASS', phase='completed', native=True,
                  acoustic_quality='NOT_RUN', human_listening='NOT_RUN', NVDA='NOT_RUN')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', '--installed', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'starting', 'checks': [],
              'installed': args.expect_installed, 'python': sys.version,
              'source_commit': os.environ.get('GITHUB_SHA'),
              'scope': 'native preference controls with explicit synthetic voice metadata',
              'acoustic_quality': 'NOT_RUN', 'human_listening': 'NOT_RUN', 'NVDA': 'NOT_RUN'}
    _write_report(args.report, report)
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, exit=True)
    try:
        _run(args, report)
        _write_report(args.report, report)
        return 0
    except BaseException as exc:
        report.update(status='FAIL', failure={'type': type(exc).__name__, 'message': str(exc)[:6000]})
        _write_report(args.report, report)
        raise
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())

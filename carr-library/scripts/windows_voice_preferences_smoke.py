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
import wave


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
                            'language_qualification.catalog', 'speech.session', 'ui.reader_synthesis',
                            'ui.speech_dialog', 'ui.reader_tabs', 'state_recovery', 'backup_producers')]
    return namespace['_module_evidence'](checkout, package.parent, modules,
        source_mode=not args.expect_installed, distribution=owner)


def _run(args, report):
    if os.name != 'nt':
        raise RuntimeError('This acceptance probe requires actual native Windows controls.')
    report['modules'] = _module_evidence(args)
    _write_report(args.report, report)
    import wx
    from carr_library import runtime_settings
    from carr_library.speech import ReadingDocument, SpeechSettings, Voice, VoiceRegistry
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.ui import voice_preferences
    from carr_library.ui.reader_synthesis import ReaderSynthesisControls

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

        def synthesis_options_scenario():
            native_voice = Voice('native:settings', 'Native option fixture', 'Synthetic PCM only', 'uk',
                                 ('rate', 'volume', 'pitch', 'punctuation'))
            limited_voice = Voice('native:limited', 'Default-only native option fixture', 'Synthetic PCM only', 'uk')
            accepted, stops, calls, rendered = [], [], [], []
            controls = ReaderSynthesisControls(frame, alive=lambda: not frame.IsBeingDeleted(),
                busy=lambda: callback_count[0] != 0, guard=guard,
                before_apply=lambda: stops.append('confirmed synthetic owner idle'),
                changed=lambda: accepted.append(controls.snapshot()),
                report=lambda message, **kwargs: errors.append(message), wx=wx)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(controls.button, 0, wx.ALL, 8)
            frame.SetSizerAndFit(layout)
            controls.sync(native_voice, enabled=True)

            def open_options(driver):
                def drive_options():
                    try:
                        view = controls.view
                        check('native synthesis options own the current modal',
                              view is not None and view.window.IsModal() and view.window.IsShown())
                        driver(view)
                    except Exception:
                        errors.append(traceback.format_exc())
                        view = controls.view
                        if view is not None and not view.destroyed and view.window.IsModal():
                            view.close()
                wx.CallLater(100, drive_options)
                return controls.show()

            def set_options(view):
                press(find(view.window, 'Use native voice pitch'))
                view.pitch.SetValue(62)
                press(find(view.window, 'Read punctuation aloud'))

            def cancel_options(view):
                set_options(view)
                press(find(view.window, 'Cancel native voice options'))
            check('native Cancel retains default synthesis options', not open_options(cancel_options)
                  and controls.snapshot() == (None, False) and not accepted and not stops)

            def apply_options(view):
                set_options(view)
                press(find(view.window, 'Apply native voice options'))
            check('native Apply captures pitch and punctuation without automatic synthesis',
                  open_options(apply_options) and controls.snapshot() == (62, True)
                  and accepted == [(62, True)] and len(stops) == 1 and not calls)

            class SettingsProvider:
                name = 'Native control synthetic PCM fixture'
                def voices(self): return (native_voice, limited_voice)
                def render(self, text, voice, output, settings, cancelled):
                    if cancelled():
                        raise InterruptedError('Native settings fixture was cancelled.')
                    calls.append((voice.id, settings, get_ident()))
                    with wave.open(str(output), 'wb') as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(8000)
                        audio.writeframes(b'\x10\x00' * 400)

            settings = controls.settings(-2, 55, native_voice)
            synthesis_registry = VoiceRegistry([SettingsProvider()])
            try:
                execute(lambda: synthesis_registry.synthesize('Synthetic option forwarding, only.',
                    native_voice.id, Path(temporary) / 'native-options.wav', settings=settings),
                    rendered.append, on_error=lambda error: errors.append(str(error)))
                pump(lambda: callback_count[0] == 0, 'Native synthesis settings worker did not settle')
                check('native-selected settings reach the actual registry on the existing worker',
                      len(calls) == 1 and calls[0][0] == native_voice.id
                      and calls[0][1] == SpeechSettings(rate=-2, volume=55, pitch=62, punctuation=True)
                      and calls[0][2] != main_thread and len(rendered) == 1
                      and rendered[0].path.is_file() and rendered[0].frames == 400)
            finally:
                synthesis_registry.close()
            store = ReaderDraftStore(Path(temporary) / 'reader-library')
            store.save(ReaderResume(ReadingDocument('Exact original!'), native_voice.id, -2, 55,
                                   pitch=settings.pitch, punctuation=settings.punctuation))
            recovered = store.load()
            check('native options survive passive exact draft recovery',
                  (recovered.pitch, recovered.punctuation, recovered.rate, recovered.volume)
                  == (62, True, -2, 55) and recovered.document.source_text == 'Exact original!')
            check('incompatible current voice preserves and gates the requested settings',
                  not controls.sync(limited_voice, enabled=True) and controls.snapshot() == (62, True))
            try:
                controls.settings(-2, 55, limited_voice)
            except ValueError:
                check('unsupported native request refuses without fallback or worker entry', len(calls) == 1)
            else:
                raise AssertionError('Unsupported native request was admitted.')

            def reset_options(view):
                check('unsupported native controls and Apply are disabled',
                      not view.pitch_enabled.IsEnabled() and not view.pitch.IsEnabled()
                      and not view.punctuation.IsEnabled() and not view.apply.IsEnabled()
                      and 'does not support' in view.status.GetValue())
                press(find(view.window, 'Reset native voice options'))
                check('explicit native default reset makes Apply available', view.apply.IsEnabled())
                press(find(view.window, 'Apply native voice options'))
            check('native reset retains rate and volume without starting another render',
                  open_options(reset_options) and controls.snapshot() == (None, False)
                  and controls.settings(-2, 55, limited_voice) == SpeechSettings(rate=-2, volume=55)
                  and len(calls) == 1)
            controls.sync(native_voice, enabled=True)
            controls.restore(62, True)
            before = (controls.snapshot(), len(accepted), len(stops))
            def destroy_options(view):
                view.pitch.SetValue(31)
                view.window.Destroy()
            check('direct native modal destruction cannot publish pending options',
                  not open_options(destroy_options) and controls.view.destroyed
                  and (controls.snapshot(), len(accepted), len(stops)) == before)
            check('all native settings operations leave the shared worker idle',
                  callback_count[0] == 0 and all(item.done() for item in futures))

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
                synthesis_options_scenario()
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
              'scope': 'native preference and synthesis-option controls with explicit synthetic metadata and PCM',
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

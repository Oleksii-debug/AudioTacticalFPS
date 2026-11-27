"""Native exact-unit selection and actual SAPI synthesis over an original source.

The existing quiet-dispatch callback is retained without executing it. This
qualifies text selection and generated PCM, not MCI, hardware or listening.
"""
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
import wave


def _release_held_key(held, simulator, check):
    if held[0] is not None:
        check('native key and modifiers released', simulator.KeyUp(*held[0]))
        held[0] = None


def _abort_reader(wx, owner_loop, frame, reader, release, errors):
    """There is one owned modal; never close foreign or inactive parent dialogs."""
    try:
        if frame is not None:
            frame._closing = True
        try:
            release()
        except Exception:
            errors.append(traceback.format_exc())
        if reader is not None and reader.GetParent() is frame and reader.IsModal():
            # show_speech_reader owns cancellation, child join and WAV cleanup
            # in its ShowModal-return finally; do not bypass that lifetime.
            reader.EndModal(wx.ID_CANCEL)
    except Exception:
        errors.append(traceback.format_exc())
    finally:
        if owner_loop is not None:
            owner_loop.ScheduleExit(1)


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.',
            'native_keyboard': 'NOT_RUN', 'actual_sapi': 'NOT_RUN', 'mci': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
        return 77
    faulthandler.enable(all_threads=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
    native_handle = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        return _run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import NativeSapiProvider, VoiceRegistry
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.speech.source import read_source_range
    import carr_library.runtime_settings as runtime_settings
    import carr_library.speech.units as units_module
    import carr_library.speech.spelling as spelling_module
    import carr_library.speech.runtime as runtime_module
    import carr_library.speech.sapi_native_worker as sapi_worker_module
    import carr_library.speech.session as session_module
    import carr_library.speech.source as source_module
    import carr_library.conversion.planning as planning_module
    import carr_library.ui.speech_dialog as reader_module
    import carr_library.ui.ebook_model as position_module

    checks, errors, identities, observed = [], [], {}, {}
    calls, results, registries, deferred = [], [], [], []
    timers, held, owner_loop, frame = [], [None], None, None

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    origin = Path(carr_library.__file__).resolve()
    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
    check('installed package origin', origin.is_relative_to(bundle) if bundle else 'site-packages' in origin.parts)
    for module in (units_module, spelling_module, planning_module, runtime_module, sapi_worker_module,
                   session_module, source_module, reader_module, position_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
              and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failure(traceback.format_exc())
            return False

    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        _release_held_key(held, simulator, check)

    def reader():
        return next((window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)
                     and window.GetTitle() == 'Voice reader and reading copy'
                     and window.GetParent() is frame), None)

    def failure(message):
        errors.append(message)
        print('NATIVE_READING_UNIT_FAILURE: ' + message, file=sys.stderr, flush=True)
        _abort_reader(wx, owner_loop, frame, reader(), release, errors)

    def later(callback, delay=25):
        def invoke():
            nonlocal owner_loop
            if owner_loop is None:
                owner_loop = gui.GetMainLoop()
            if errors or observed.get('finished'):
                return
            try:
                callback()
            except Exception:
                failure(traceback.format_exc())
        timer = wx.CallLater(delay, invoke)
        timers.append(timer)
        return timer

    def until(predicate, complete, *, timeout=15):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                dialog = reader()
                field = wx.FindWindowByName('Speech and save status', dialog) if dialog is not None else None
                raise TimeoutError('The native reading-unit stage did not finish. Reader status: '
                                   + (field.GetValue() if field is not None else 'not open'))
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
            try:
                check('native key down accepted', simulator.KeyDown(code, modifiers))
            except BaseException:
                release()
                raise
            def up():
                release()
                later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def named(name):
        control = wx.FindWindowByName(name, observed['reader'])
        if control is None:
            raise AssertionError('Missing native named control: ' + name)
        return control

    def press(name, complete):
        control = named(name)
        check('native text-unit action enabled: ' + name, isinstance(control, wx.Button) and control.IsEnabled())
        key(control, wx.WXK_SPACE, lambda: until(
            lambda: named('Select current text unit').IsEnabled(), complete, timeout=40))

    def mode(index, complete):
        control = named('Text unit: word, sentence or paragraph')
        def selected():
            check('explicit native unit mode: ' + str(index), control.GetSelection() == index)
            complete()
        if index == 1:
            key(control, wx.WXK_HOME, lambda: key(control, wx.WXK_DOWN, selected))
        else:
            key(control, wx.WXK_HOME if index == 0 else wx.WXK_END, selected)

    class ObservedRegistry(VoiceRegistry):
        closed = False
        def synthesize(self, text, voice_id, destination, **kwargs):
            calls.append((text, voice_id))
            result = super().synthesize(text, voice_id, destination, **kwargs)
            results.append(result)
            return result
        def close(self):
            value = super().close()
            self.closed = value is not False
            return value

    with TemporaryDirectory(prefix='resonance-native-reading-units-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Original native reading-unit acceptance')
        original = ('Long Unicode prefix 😀 Україна ' + 'visual wrapping boundary ' * 14
                    + '.\r\nAlpha Beta. Next sentence.\r\n\r\nLast paragraph.')
        imported = library.add_text(workspace, 'Original reading-unit source', original)
        desk = ReadingDesk(library, workspace, imported['edition'])
        source = read_source_range(desk, 1, 1)
        asset = library.assets.resolve(imported['asset'])
        asset_hash = sha256(asset.read_bytes()).hexdigest()
        slot = 'native-reading-units'
        store = ReaderDraftStore(library.root, slot=slot)
        frame = wx.Frame(None, title='Resonance exact reading-unit native acceptance', size=(650, 400))
        frame.library = library
        frame.report = lambda message: observed.setdefault('notices', []).append(str(message))
        original_registry, original_dispatch = runtime_settings.voice_registry, reader_module.play_when_quiet

        def actual_sapi(state_root, *, cancelled=lambda: False, **kwargs):
            if Path(state_root) != library.root / 'state':
                raise AssertionError('Voice discovery lost the selected library.')
            registry = ObservedRegistry((NativeSapiProvider(),), cancelled=cancelled)
            registries.append(registry)
            if not registry.voices():
                notice = '; '.join(registry.notices)
                registry.close()
                raise RuntimeError('Actual installed SAPI is required: ' + notice)
            return registry

        def retain_playback(progress, action, *, schedule, alive, on_error):
            deferred.append({'action': action, 'alive': alive})
            # Deliberately do not call action: device playback is a separate gate.

        runtime_settings.voice_registry, reader_module.play_when_quiet = actual_sapi, retain_playback

        def ready():
            dialog = reader()
            if dialog is None:
                return False
            observed['reader'] = dialog
            return named('Select current text unit').IsEnabled() and named('Voice (installed locally)').GetCount() > 0

        def editor():
            return named('Reading copy (editable; Ctrl+S saves)')

        def status():
            return named('Speech and save status').GetValue()

        def caret(offset):
            reader_module._restore_character_cursor(editor(), observed['initial'], offset)
            check('exact Unicode/line-end cursor fixture',
                  reader_module._cursor_character_offset(editor(), observed['initial']) == offset)

        def selected(expected, kind):
            actual = editor().GetStringSelection()
            print('UNIT_SELECTION: ' + json.dumps({'actual': actual, 'expected': expected,
                  'native_range': list(editor().GetSelection()), 'status': status()}, ensure_ascii=False), flush=True)
            check('native exact ' + kind + ' selection', actual == expected and status().startswith(kind.title()))
            check('selection never starts synthesis or playback', not calls and not deferred)
            check('navigation preserves current reading text', editor().GetValue() == observed['initial'])
            check('navigation preserves retained original bytes', sha256(asset.read_bytes()).hexdigest() == asset_hash)
            saved = store.load()
            check('navigation retains source lineage without quotation authority', saved is not None
                  and saved.document.source_text == source.text
                  and saved.document.source_anchor == source.reading_document().source_anchor
                  and saved.document.source_anchor['quotation_authority'] is False)
            if 'document' not in observed:
                observed['document'] = saved.document.to_dict()
            check('navigation preserves reading revision and undo history', saved.document.to_dict() == observed['document'])

        def opened():
            observed['initial'] = editor().GetValue()
            check('fixture contains long native Unicode and line boundaries',
                  '😀' in observed['initial'] and 'Україна' in observed['initial']
                  and '\n' in observed['initial'] and observed['initial'].index('Alpha') > 300)
            check('reader does not synthesize on open', not calls and not deferred)
            check('editor uses stored-line native coordinates', bool(editor().GetWindowStyleFlag() & wx.TE_DONTWRAP))
            key(named('Voice (installed locally)'), wx.WXK_HOME, word_mode)

        def word_mode():
            voice_index = named('Voice (installed locally)').GetSelection()
            check('explicit actual installed SAPI voice selected', voice_index == 0)
            observed['voice_id'] = registries[-1].voices()[voice_index].id
            caret(observed['initial'].index('Alpha') + 2)
            mode(0, lambda: press('Select current text unit', word_current))

        def word_current():
            selected('Alpha', 'word')
            press('Select next text unit', word_next)
        def word_next():
            selected('Beta', 'word')
            press('Select previous text unit', word_previous)
        def word_previous():
            selected('Alpha', 'word')
            mode(1, lambda: press('Select current text unit', sentence_current))
        def sentence_current():
            selected('Alpha Beta.', 'sentence')
            press('Select next text unit', sentence_next)
        def sentence_next():
            selected('Next sentence.', 'sentence')
            mode(2, lambda: press('Select current text unit', paragraph_current))
        def paragraph_current():
            selected('Alpha Beta. Next sentence.', 'paragraph')
            press('Select next text unit', paragraph_next)
        def paragraph_next():
            selected('Last paragraph.', 'paragraph')
            observed['boundary_selection'] = editor().GetSelection()
            press('Select next text unit', boundary_refused)
        def boundary_refused():
            check('native final boundary refuses without wrapping or changing selection',
                  'does not wrap' in status() and editor().GetStringSelection() == 'Last paragraph.'
                  and editor().GetSelection() == observed['boundary_selection'] and not calls)
            caret(observed['initial'].index('Alpha') + len('Alpha'))
            mode(0, lambda: press('Read current text unit', gap_refused))
        def gap_refused():
            check('current-word gap never falls back to all-text synthesis',
                  'No current word' in status() and not calls and not deferred and editor().GetStringSelection() == '')
            caret(observed['initial'].index('Alpha') + 2)
            mode(1, lambda: press('Read current text unit', synthesized))
        def synthesized():
            check('actual SAPI receives only the explicitly bound sentence',
                  calls == [('Alpha Beta.', observed['voice_id'])] and len(results) == 1)
            result = results[0]
            with wave.open(str(result.path), 'rb') as audio:
                check('actual existing SAPI worker produced nonempty PCM', audio.getnframes() == result.frames > 0
                      and audio.getsampwidth() == 2 and audio.getnchannels() == result.channels
                      and audio.getframerate() == result.sample_rate)
            check('exact synthesized text and PCM receipts match',
                  result.text_sha256 == sha256(b'Alpha Beta.').hexdigest()
                  and result.sha256 == sha256(result.path.read_bytes()).hexdigest()
                  and editor().GetStringSelection() == 'Alpha Beta.')
            check('one owned playback callback is deliberately deferred', len(deferred) == 1 and deferred[0]['alive']())
            observed['pcm'] = {'sha256': result.sha256, 'text_sha256': result.text_sha256,
                'frames': result.frames, 'sample_rate': result.sample_rate, 'channels': result.channels}
            key(editor(), ord('S'), saved, wx.MOD_CONTROL)
        def saved():
            record = store.load()
            check('real draft save preserves exact reading source and history',
                  record.document.to_dict() == observed['document'] and sha256(asset.read_bytes()).hexdigest() == asset_hash)
            observed['saved_cursor'] = record.cursor
            check('saved cursor uses verified character coordinates', record.cursor ==
                  reader_module._cursor_character_offset(editor(), observed['initial']))
            observed['first_complete'] = True
            key(editor(), wx.WXK_ESCAPE, lambda: None)

        def reopened():
            check('reopen recovers exact reading text and character cursor',
                  editor().GetValue() == observed['initial'] and reader_module._cursor_character_offset(
                      editor(), observed['initial']) == observed['saved_cursor'])
            check('reopen does not resynthesize or revive stale playback',
                  len(calls) == len(results) == len(deferred) == 1 and not deferred[0]['alive']())
            key(editor(), ord('S'), close_reopened, wx.MOD_CONTROL)
        def close_reopened():
            observed['reopen_complete'] = True
            key(editor(), wx.WXK_ESCAPE, lambda: None)

        def open_reader(reopen=False):
            until(ready, reopened if reopen else opened, timeout=40)
            reader_module.show_speech_reader(frame, title=source.title, recovery_slot=slot,
                source_loader=lambda cancelled: read_source_range(desk, 1, 1, cancelled=cancelled))
            release()  # An Escape key-down can return the modal before its timed key-up.
            if errors:
                return
            check('Reader returned only after owned registry cleanup', bool(registries) and registries[-1].closed)
            check('Reader returned after deleting owned temporary audio', all(not value.path.exists() for value in results))
            if not reopen:
                check('all native unit stages completed before the first close', observed.get('first_complete'))
                later(lambda: open_reader(True))
            else:
                check('recovery session completed before the second close', observed.get('reopen_complete'))
                observed['finished'] = True
                owner_loop.ScheduleExit(0)

        def watchdog():
            raise TimeoutError('The native reading-unit probe exceeded 120 seconds.')

        try:
            frame.Show()
            gui.SetTopWindow(frame)
            later(open_reader, 60)
            later(watchdog, 120000)
            gui.MainLoop()
        finally:
            def cleanup(action):
                try:
                    action()
                except Exception:
                    errors.append(traceback.format_exc())
            cleanup(release)
            for timer in timers:
                cleanup(timer.Stop)
            runtime_settings.voice_registry, reader_module.play_when_quiet = original_registry, original_dispatch
            frame._closing = True
            cleanup(frame.Destroy)
            cleanup(gui.Yield)
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('both real Reader modal sessions completed', observed.get('finished') and len(registries) == 2)
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
            'source_identity': identities, 'retained_source_sha256': asset_hash, 'pcm': observed['pcm'],
            'native_keyboard': 'PASS', 'actual_sapi': 'PASS', 'native_unit_selection': 'PASS',
            'physical_playback': 'NOT_RUN', 'mci': 'NOT_RUN', 'voice_quality': 'NOT_RUN',
            'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)

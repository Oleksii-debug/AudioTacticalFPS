"""Installed Windows spelling review with a synthetic dictionary and real Spylls child.

Native controls, button keyboard input, source-bound reading edits and native
owner deletion are real. Voice enumeration alone is synthetic. No language
quality, physical-user keyboard, screen-reader or dictionary-license claim.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        return _native()
    finally:
        if handle is not None:
            handle.close()


def _native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.speech import reading, spellcheck, spell_worker
    from carr_library.speech.source import read_source_range
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.ui import speech_dialog, spellcheck_dialog, pronunciation_dialog
    from carr_library import runtime_settings

    helper = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_spelling_installed_bindings', helper)
    bindings = importlib.util.module_from_spec(spec); spec.loader.exec_module(bindings)
    source_identity = bindings.verify_installed_sources(reading, spellcheck, spell_worker,
        speech_dialog, spellcheck_dialog, pronunciation_dialog, runtime_settings)
    backend = spellcheck._backend(lambda: False)
    checks, errors, state = [], [], {}
    deadline, held = monotonic() + 120, [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def release_key():
        if held[0] is not None:
            key, held[0] = held[0], None
            if not simulator.KeyUp(key):
                raise AssertionError('Native spelling key release failed.')

    def fail():
        errors.append(traceback.format_exc())
        release_key()
        for window in wx.GetTopLevelWindows():
            if window and isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            fail(); return False

    gui, simulator = App(False), wx.UIActionSimulator()
    gui.SetExitOnFrameDelete(False)
    check('real native Windows controls', wx.Platform == '__WXMSW__')

    def later(callback, delay=25):
        def invoke():
            if errors:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native spelling acceptance exceeded its time allowance.')
                callback()
            except Exception:
                fail()
        wx.CallLater(delay, invoke)

    def until(predicate, done, timeout=15):
        end = monotonic() + timeout
        def poll():
            if predicate():
                done()
            elif monotonic() >= end:
                raise TimeoutError('Native spelling stage did not finish.')
            else:
                later(poll)
        later(poll)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(window, name):
        return next(child for child in children(window) if child.GetName() == name)

    def top(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if window and isinstance(window, wx.Dialog) and window.GetTitle() == title), None)

    def press(button, done):
        def focused():
            owner = wx.GetTopLevelParent(button)
            if wx.Window.FindFocus() is button and int(user32.GetForegroundWindow() or 0) == int(owner.GetHandle()):
                return True
            owner.Raise(); user32.SetForegroundWindow(owner.GetHandle()); button.SetFocus()
            return False
        def down():
            held[0] = wx.WXK_SPACE
            check('native Space activates focused spelling action', simulator.KeyDown(wx.WXK_SPACE))
            later(lambda: (release_key(), later(done)), 40)
        until(focused, down, timeout=5)

    class EnumerationOnly:
        name = 'Synthetic spelling acceptance enumeration'
        def voices(self): return (Voice('fixture:spelling', 'Synthetic enumeration', 'fixture', 'en'),)
        def synthesize(self, *args, **kwargs): raise AssertionError('Spelling must not start voice synthesis.')
        def close(self): state['provider_closed'] = True

    with TemporaryDirectory(prefix='resonance-native-spelling-') as directory:
        base = Path(directory)
        selected = base / 'fixture.aff'
        selected.write_text('SET UTF-8\nTRY abcdefghijklmnopqrstuvwxyz\n', encoding='utf-8')
        selected.with_suffix('.dic').write_text('4\nhello\nworld\nretained\nsource\n', encoding='utf-8')
        dictionary_bytes = (selected.read_bytes(), selected.with_suffix('.dic').read_bytes())
        library = LibraryApp(base / 'library')
        workspace = library.create_workspace('Synthetic spelling acceptance')
        original = '😀 retained source.\r\nhello wurld'
        imported = library.add_text(workspace, 'Spelling source', original)
        desk = ReadingDesk(library, workspace, imported['edition'])
        source = read_source_range(desk, 1, 1)
        asset = library.assets.resolve(imported['asset']); asset_bytes = asset.read_bytes()
        frame = wx.Frame(None, title='Resonance spelling acceptance')
        frame.library = library; frame.report = lambda value: None
        previous_registry = runtime_settings.voice_registry
        runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((EnumerationOnly(),))
        executors = []

        def loaded():
            reader = top('Voice reader and reading copy')
            if reader is None:
                return False
            action = named(reader, 'Review spelling')
            return action.IsEnabled() and state.get('view') is not None and not state['view']['live'].core.busy

        def open_review():
            reader = top('Voice reader and reading copy')
            state['reader'], state['editor'] = reader, named(reader, 'Reading copy (editable; Ctrl+S saves)')
            state['initial'] = state['editor'].GetValue()
            press(named(reader, 'Review spelling'), lambda: until(
                lambda: top('Review spelling in reading copy') is not None, review_ready))

        def review_ready():
            dialog = top('Review spelling in reading copy'); state['review'] = dialog
            dialog.dictionary.SetValue(str(selected))
            press(dialog.buttons['review'], lambda: until(lambda: not dialog.busy, reviewed))

        def reviewed():
            dialog = state['review']
            check('actual bounded Spylls child returned a source-bound result', dialog.review is not None)
            check('actual synthetic dictionary finds one misspelling',
                dialog.review.complete and [issue.word for issue in dialog.review.issues] == ['wurld'])
            index = list(dialog.review.issues[0].suggestions).index('world')
            dialog.suggestions.SetSelection(index)
            press(dialog.buttons['preview'], previewed)

        def previewed():
            dialog = state['review']
            check('native preview keeps original reading text', state['editor'].GetValue() == state['initial'])
            check('exact proposed correction', dialog.before.GetValue() == 'wurld' and dialog.after.GetValue() == 'world')
            press(dialog.buttons['apply'], lambda: until(lambda: not dialog.busy, applied))

        def applied():
            check('native apply uses the existing reversible reading copy',
                  state['editor'].GetValue() == state['initial'].replace('wurld', 'world'))
            check('retained source and dictionary bytes unchanged', asset.read_bytes() == asset_bytes and
                  dictionary_bytes == (selected.read_bytes(), selected.with_suffix('.dic').read_bytes()))
            press(state['review'].buttons['undo'], undone)

        def undone():
            check('native undo restores exact reading text', state['editor'].GetValue() == state['initial'])
            press(state['review'].buttons['close'], lambda: until(
                lambda: top('Review spelling in reading copy') is None, close_reader))

        def close_reader():
            press(state['view']['buttons']['save'], saved_reader)

        def saved_reader():
            check('actual Save reading copy clears the dirty state', not state['view']['document']().dirty)
            close = next(c for c in children(state['reader']) if isinstance(c, wx.Button) and c.GetLabel() == 'C&lose')
            press(close, lambda: None)

        def lifetime(mode, done):
            owner = wx.Frame(None, title='Spelling native owner ' + mode)
            doc, cancelled = reading.ReadingDocument('hello wurld'), Event()
            executor = ThreadPoolExecutor(max_workers=1); executors.append(executor)
            pending, destroyed, delivered, violations = [], [], [], []
            def run(work, completed, *, on_failed):
                cancelled.clear()
                pending.append((executor.submit(work), completed, on_failed))
                return True
            captured = lambda: pronunciation_dialog.ReadingSnapshot.capture(doc, (0, 0))
            dialog = spellcheck_dialog.create_spellcheck_dialog(owner, library_root=library.root,
                capture=captured, guard=lambda: None, run=run, cancel=cancelled,
                apply=lambda value: (_ for _ in ()).throw(AssertionError('late apply')),
                undo=lambda value: (_ for _ in ()).throw(AssertionError('late undo')))
            identity = dialog.GetId()
            def destroying(event):
                if event.GetId() == identity:
                    destroyed.append(identity)
                event.Skip()
            dialog.Bind(wx.EVT_WINDOW_DESTROY, destroying)
            original_report = dialog.report
            def report(value):
                if not dialog.alive:
                    violations.append('late report')
                original_report(value)
            dialog.report = report
            owner.Show(); dialog.Show(); dialog.dictionary.SetValue(str(selected))
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, dialog.buttons['review'].GetId())
            event.SetEventObject(dialog.buttons['review'])
            dialog.buttons['review'].GetEventHandler().ProcessEvent(event)
            def destroy():
                check('real spelling worker finished before held callback replay', pending[0][0].result().complete)
                if mode == 'direct':
                    dialog.Destroy()
                else:
                    wx.Window.DestroyChildren(owner)
                until(lambda: not dialog and destroyed == [identity], replay)
            def replay():
                check('native deletion detached the spelling owner', not dialog.alive and cancelled.is_set())
                def deliver():
                    future, completed, _failed = pending[0]
                    completed(future.result()); delivered.append(True)
                wx.CallAfter(deliver)
                until(lambda: bool(delivered), finished)
            def finished():
                check('queued real result ignored after ' + mode + ' native deletion',
                      violations == [] and doc.text == doc.source_text and dialog.review is None)
                executor.shutdown(wait=True); owner.Destroy(); done()
            until(lambda: bool(pending) and pending[0][0].done(), destroy)

        def finish():
            check('source remains byte-identical after all native cases', asset.read_bytes() == asset_bytes)
            state['complete'] = True
            frame.Destroy(); gui.ExitMainLoop()

        def launch():
            frame.Show(); until(loaded, open_review)
            speech_dialog.show_speech_reader(frame, title='Spelling acceptance copy', recovery_slot='spellcheck-native',
                source_loader=lambda cancelled: read_source_range(desk, 1, 1, cancelled=cancelled),
                on_ready=lambda dialog, view: state.update(view=view))
            release_key()
            if errors:
                return
            resumed = ReaderDraftStore(library.root, slot='spellcheck-native').for_source(source).load()
            check('saved recovery preserves source lineage and original text', resumed is not None
                  and resumed.document.source_anchor == source.reading_document().source_anchor
                  and resumed.document.text == source.text)
            check('owned enumeration registry closed', state.get('provider_closed') is True)
            lifetime('direct', lambda: lifetime('parent', finish))

        try:
            later(launch); gui.MainLoop()
        finally:
            release_key(); runtime_settings.voice_registry = previous_registry
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=True)
            for window in tuple(wx.GetTopLevelWindows()):
                if window:
                    window.Destroy()
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('whole native spelling workflow completed', state.get('complete') is True)
        print(json.dumps({'status': 'PASS', 'checks': len(checks), 'source_identity': source_identity,
            'backend_version': backend['version'], 'backend_wheel_sha256': backend['wheel_sha256'],
            'source_sha256': sha256(asset_bytes).hexdigest(), 'dictionary': 'AUTHORED_SYNTHETIC_PAIR',
            'dictionary_aff_sha256': sha256(dictionary_bytes[0]).hexdigest(),
            'dictionary_dic_sha256': sha256(dictionary_bytes[1]).hexdigest(),
            'native_keyboard_automation': 'PASS', 'native_file_picker': 'NOT_RUN',
            'language_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)

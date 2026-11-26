"""Actual native wx keyboard/save lifecycle checks with local temporary state.

Run against the installed package on Windows. Controls, accelerator key presses,
voice discovery, recovery files and filesystem failures are real. A delayed
synthetic source fixture exercises pending-source behavior; no GUI is mocked.
Audible listening, manual file pickers and NVDA remain separate acceptance work.
"""
import json
from contextlib import ExitStack
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback
from types import SimpleNamespace


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'manual_nvda': 'NOT_RUN', 'manual_listening': 'NOT_RUN'}))
        return 77

    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.studio.project import ProjectStore
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.speech_dialog import show_speech_reader
    from carr_library.ui.studio_dialog import show_studio

    checks, loop_errors, abort_hooks = [], [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            loop_errors.append(traceback.format_exc())
            for hook in tuple(abort_hooks):
                try:
                    hook()
                except Exception:
                    loop_errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    app = ProbeApp(False)
    check('actual native Windows wx controls', wx.Platform == '__WXMSW__')
    keys = wx.UIActionSimulator()

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def named(dialog, prefix):
        matches = [control for control in descendants(dialog) if control.GetName().startswith(prefix)]
        if len(matches) != 1:
            raise AssertionError(f'Expected one native control named {prefix!r}; found {len(matches)}.')
        return matches[0]

    def button(dialog, label):
        matches = [control for control in descendants(dialog) if isinstance(control, wx.Button)
                   and control.GetLabel().replace('&', '') == label]
        if len(matches) != 1:
            raise AssertionError(f'Expected one native button {label!r}.')
        return matches[0]

    def press_save(control):
        control.SetFocus()
        if not keys.Char(ord('S'), wx.MOD_CONTROL):
            raise RuntimeError('Native Ctrl+S could not be delivered.')

    def press_button(control):
        control.SetFocus()
        if not keys.Char(wx.WXK_SPACE):
            raise RuntimeError('The native Space key could not activate the button.')

    def unique_access_keys(dialog):
        access_keys = []
        for control in descendants(dialog):
            if isinstance(control, (wx.StaticText, wx.Button, wx.CheckBox)):
                label = control.GetLabel()
                for index, char in enumerate(label[:-1]):
                    if char == '&' and label[index + 1] != '&':
                        access_keys.append(label[index + 1].casefold())
        return len(access_keys) == len(set(access_keys))

    def drive(title_prefix, opener, scenario, *, timeout=180, on_abort=lambda: None):
        state = {'step': 0, 'complete': False}
        active, timer = True, None
        deadline = monotonic() + timeout
        failures = []
        abort_hooks.append(on_abort)

        def tick():
            nonlocal timer
            if not active:
                return
            try:
                if loop_errors:
                    raise RuntimeError('\n'.join(loop_errors))
                if monotonic() > deadline:
                    raise TimeoutError(f'Native {title_prefix} scenario timed out at step {state["step"]}.')
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsModal()
                           and window.GetTitle().startswith(title_prefix)]
                if dialogs:
                    scenario(dialogs[-1], state)
            except Exception:
                failures.append(traceback.format_exc())
                try:
                    on_abort()
                except Exception:
                    failures.append(traceback.format_exc())
                for window in reversed(wx.GetTopLevelWindows()):
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                return
            timer = wx.CallLater(80, tick)

        timer = wx.CallLater(80, tick)
        try:
            opener()
        except BaseException:
            on_abort()
            raise
        finally:
            active = False
            if timer is not None:
                timer.Stop()
            abort_hooks.remove(on_abort)
        if failures:
            raise RuntimeError('\n'.join(failures))
        if loop_errors:
            raise RuntimeError('\n'.join(loop_errors))
        check(title_prefix + ' scenario completed through native controls', state['complete'])
        app.Yield()

    frame = None
    with TemporaryDirectory(prefix='carr-audio-native-lifecycle-', ignore_cleanup_errors=True) as temporary, ExitStack() as cleanup:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Native audio lifecycle')
        frame = DesktopFrame(library)
        def cleanup_frame():
            if frame is not None:
                frame._closing = True
                frame.cancel_event.set()
                frame.executor.shutdown(wait=False, cancel_futures=True)
                frame.Destroy()
                app.Yield()
        cleanup.callback(cleanup_frame)
        frame.Show()
        frame.Raise()
        app.SetTopWindow(frame)
        app.Yield()
        manager = frame.keyboard_commands
        check('primary save, reader, studio and assistant commands share the registry',
            {'file.save', 'speech.reader', 'speech.book', 'studio.open', 'assistant.open'} <= manager.bindings.keys())
        # Deliberately assign main-window F6/F8 commands and remap its Save key.
        # The modal audio windows must still own their local Ctrl+S/F6/F8 keys.
        manager.apply_overrides({'file.save': 'Ctrl+Shift+S', 'assistant.open': 'F6', 'studio.open': 'F8'})
        draft_path = library.root / 'state' / 'studio' / 'draft.json'
        wanted = 'Native keyboard draft. Preserve punctuation and Український текст.'

        def studio_scenario(dialog, state):
            editor = named(dialog, 'Script —')
            title = named(dialog, 'Project title')
            status = named(dialog, 'Production and save status')
            if state['step'] == 0:
                if not editor.IsEnabled():
                    return
                check('Studio dialog access keys are unique', unique_access_keys(dialog))
                check('Studio uses an actual native text editor', isinstance(editor, wx.TextCtrl))
                check('fresh recovery path is empty before fault injection', not draft_path.exists())
                # A directory at the intended file path causes a real safe-write
                # failure on Windows; no backend or UI method is substituted.
                draft_path.mkdir()
                editor.SetValue(wanted)
                title.SetValue('Native lifecycle project')
                press_save(editor)
                state['step'] = 1
            elif state['step'] == 1:
                if 'Project validation failed' not in status.GetValue():
                    return
                check('native Ctrl+S failure retains the complete editor', editor.GetValue() == wanted)
                check('modal save remains local after the Main-window save is remapped',
                    manager.shortcut('file.save') == 'Ctrl+Shift+S'
                    and not manager.available('file.save') and not frame.IsEnabled())
                status.ChangeValue('Waiting for the modal F6 render command.')
                if not keys.Char(wx.WXK_F6):
                    raise RuntimeError('Native F6 could not be delivered.')
                state['step'] = 11
            elif state['step'] == 11:
                if 'Project validation failed' not in status.GetValue():
                    return
                check('modal F6 uses the Studio command despite a Main-window F6 assignment',
                      editor.GetValue() == wanted and not manager.available('assistant.open'))
                if not keys.Char(wx.WXK_F8):
                    raise RuntimeError('Native F8 could not be delivered.')
                press_button(button(dialog, 'Close'))
                state['step'] = 2
            elif state['step'] == 2:
                if 'window remains open' not in status.GetValue():
                    return
                check('failed close keeps a real modal dialog and its draft', dialog.IsModal() and editor.GetValue() == wanted)
                draft_path.rmdir()
                press_save(editor)
                state['step'] = 3
            elif state['step'] == 3:
                if 'Saved project revision' not in status.GetValue():
                    return
                recovery = json.loads(draft_path.read_text(encoding='utf-8'))
                stored = ProjectStore(draft_path.parent / 'projects').load(recovery['project_id'])
                check('native Ctrl+S publishes real project and bound recovery',
                      recovery['text'] == wanted and stored.original_script == wanted
                      and stored.title == 'Native lifecycle project')
                state['complete'] = True
                press_button(button(dialog, 'Close'))

        def clear_studio_fault():
            if draft_path.is_dir():
                draft_path.rmdir()
        try:
            drive('Audio Studio', lambda: show_studio(frame), studio_scenario, on_abort=clear_studio_fault)
        finally:
            if draft_path.is_dir():
                draft_path.rmdir()

        source_started, release_source, source_finished = threading.Event(), threading.Event(), threading.Event()
        cleanup.callback(release_source.set)
        source_text = 'Synthetic source fixture. The original must remain exactly unchanged.'
        reader_slot = 'native-pending-source-lifecycle'
        recovery = ReaderDraftStore(library.root, slot=reader_slot)

        def source_loader(cancelled):
            source_started.set()
            try:
                while not release_source.wait(.02):
                    if cancelled():
                        raise InterruptedError('Synthetic source loading cancelled')
                return SimpleNamespace(text=source_text, title='Native source fixture', positions=(1,), warnings=())
            finally:
                source_finished.set()

        def reader_scenario(dialog, state):
            editor = named(dialog, 'Reading copy (editable;')
            status = named(dialog, 'Speech and save status')
            if state['step'] == 0:
                if not source_started.is_set():
                    return
                check('Reader dialog access keys are unique', unique_access_keys(dialog))
                check('pending source cannot be edited before its original is known', not editor.IsEnabled())
                press_save(button(dialog, 'Save reading copy'))
                state['step'] = 1
            elif state['step'] == 1:
                if 'still loading' not in status.GetValue():
                    return
                check('native Ctrl+S during source load cannot claim recovery', not recovery.path.exists())
                if not keys.Char(wx.WXK_F8):
                    raise RuntimeError('Native F8 could not be delivered.')
                state['step'] = 11
            elif state['step'] == 11:
                if not source_finished.is_set() or 'cancelled' not in status.GetValue().casefold():
                    return
                check('modal F8 cancels its source worker without invoking Main-window Studio',
                      not recovery.path.exists() and not manager.available('studio.open'))
                release_source.set()
                press_button(button(dialog, 'Reload voices'))
                state['step'] = 2
            elif state['step'] == 2:
                if not editor.IsEnabled() or editor.GetValue() != source_text:
                    return
                editor.SetValue('An edited native narration derivative.')
                press_save(editor)
                state['step'] = 3
            elif state['step'] == 3:
                if 'saved for recovery' not in status.GetValue():
                    return
                snapshot = recovery.load()
                check('native save retains immutable source and edited derivative',
                    snapshot.document.source_text == source_text
                    and snapshot.document.text == 'An edited native narration derivative.')
                state['complete'] = True
                press_button(button(dialog, 'Close'))

        try:
            drive('Voice reader', lambda: show_speech_reader(frame, source_loader=source_loader,
                recovery_slot=reader_slot), reader_scenario, timeout=180, on_abort=release_source.set)
        finally:
            release_source.set()
        check('delayed source worker finished before temporary state cleanup', source_finished.wait(5))
        frame.Close()
        app.Yield()
        frame = None

    print(json.dumps({'status': 'PASS', 'checks': checks, 'gui': 'actual native wx / Windows',
        'fault': 'real directory collision at recovery-file path',
        'manual_nvda': 'NOT_RUN', 'manual_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

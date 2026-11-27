"""Installed native spelling review with the actual reviewed code-only backend.

Uses explicit synthetic Hunspell files, the real narration editor and shared
operation session. No bundled dictionary, full-language quality or NVDA claim.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.reader import spellcheck
    from carr_library.reader.narration import NarrationStore, new_project
    from carr_library.operations import ResourceBroker
    from carr_library.ui import spellcheck_dialog as spelling
    from carr_library.ui.live_reading import native_offset, selected_source_offsets
    from carr_library.ui.narration_editor import open_narration_editor

    checks, errors, timers, futures = [], [], [], []
    settled = Event()
    def watchdog():
        if not settled.wait(60):
            print('FAIL: native spelling review exceeded its watchdog', flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    app = wx.App(False)
    event_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(event_loop)
    check('native Windows wx controls', wx.Platform == '__WXMSW__')
    for module in (spellcheck, spelling):
        check('installed module ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).parts])
    check('exact reviewed code-only backend', spellcheck._backend(lambda: False)['version'] == '0.1.7+carr.1')

    with TemporaryDirectory(prefix='carr-native-spelling-') as temporary:
        base = Path(temporary)
        root = base / 'library'; root.mkdir()
        source_file = base / 'Original source Україна.txt'
        source = '😀 retained.\r\nwurld outside; hello wurld books world.'
        source_file.write_bytes(source.encode('utf-8'))
        original_bytes = source_file.read_bytes()
        aff = base / 'Selected dictionary.aff'
        dic = aff.with_suffix('.dic')
        aff.write_text('SET UTF-8\nTRY abcdefghijklmnopqrstuvwxyz\nSFX S Y 1\nSFX S 0 s .\n', encoding='utf-8')
        dic.write_text('3\nhello\nworld\nbook/S\n', encoding='utf-8')
        pair = (aff.read_bytes(), dic.read_bytes())
        store = NarrationStore(root)
        project = new_project(source, title='Native spelling Україна', original_path=str(source_file))
        first, last = source.rindex('wurld'), source.rindex('wurld') + 5
        frame = wx.Frame(None, title='Installed spelling acceptance')
        frame.library = type('Library', (), {'root': root})()
        frame.cancel_event = Event()
        frame.working = frame._closing = False
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-spelling-operation')

        def run_job(title, operation, done=None, failed=None, **options):
            if frame.working:
                return False
            frame.working = True
            frame.cancel_event.clear()
            future = executor.submit(operation); futures.append(future)
            def finished(future):
                def apply():
                    frame.working = False
                    try:
                        result = future.result()
                    except Exception as exc:
                        if failed is not None:
                            failed(exc)
                    else:
                        if done is not None:
                            done(result)
                wx.CallAfter(apply)
            future.add_done_callback(finished)
        frame.run_job = run_job
        frame.Show(); app.SetTopWindow(frame)
        state = {'active': True, 'phase': 'reader', 'view': None, 'session': None,
                 'reader': None, 'controller': None, 'controls': None, 'complete': False}
        deadline = monotonic() + 45

        def press(button):
            check('enabled native button ' + button.GetLabel(),
                  isinstance(button, wx.Button) and button.IsEnabled())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('handled native button ' + button.GetLabel(),
                  button.GetEventHandler().ProcessEvent(event))

        def schedule():
            timers.append(wx.CallLater(20, poll))

        def fail(error):
            errors.append(str(error))
            session = state.get('session')
            if session is not None:
                session.request_close()
            state['phase'] = 'failed'
            schedule()

        def poll():
            if not state['active']:
                return
            try:
                if state['phase'] != 'failed' and monotonic() >= deadline:
                    raise TimeoutError('The spelling native flow exceeded its time budget.')
                phase, view = state['phase'], state['view']
                if phase == 'reader':
                    if state['reader'] is None:
                        schedule(); return
                    controls = state['controls']
                    check('spelling is an explicit reading action', 'spelling' in controls)
                    check('existing read-aloud action is retained', 'live_reading' in controls)
                    controls['editor'].SetSelection(
                        native_offset(controls['editor'], source, first),
                        native_offset(controls['editor'], source, last))
                    state['phase'] = 'dialog'
                    schedule()
                    press(controls['spelling'])
                    return
                if phase == 'dialog':
                    if view is None or not view.IsModal():
                        schedule(); return
                    check('owned actual spelling modal', view.GetParent() is state['reader'])
                    check('native selection maps emoji and CRLF to exact source',
                          view.initial.selection == (first, last))
                    check('only explicit selected dictionary', not view.dictionary.GetValue())
                    check('selected text remains the default scope', view.scope.GetSelection() == 1)
                    view.dictionary.SetValue(str(aff))
                    view.dictionary.SetFocus()
                    state['phase'] = 'focus'
                elif phase == 'focus':
                    if wx.Window.FindFocus() is not view.dictionary:
                        schedule(); return
                    check('dictionary control receives native keyboard focus', True)
                    state['phase'] = 'reviewed'
                    press(view.buttons['review'])
                elif phase == 'reviewed':
                    if view.busy:
                        schedule(); return
                    check('actual backend returned a complete selected review',
                          view.review is not None and view.review.complete and view.review.total_tokens == 1)
                    issue = view.review.issues[0]
                    check('actual selected misspelling and suggestion',
                          issue.word == 'wurld' and 'world' in issue.suggestions)
                    view.issues.SetSelection(0); view.on_issue()
                    view.suggestions.SetSelection(issue.suggestions.index('world'))
                    press(view.buttons['preview'])
                    check('visible correction preview', view.before.GetValue() == 'wurld'
                          and view.after.GetValue() == 'world')
                    state['phase'] = 'applied'
                    press(view.buttons['apply'])
                elif phase == 'applied':
                    if view.busy:
                        schedule(); return
                    controller = state['controller']
                    expected = source[:first] + 'world' + source[last:]
                    check('only selected word is changed in the reading copy',
                          controller.pending_text == expected and controller.project.original_text == source)
                    check('one existing undo revision was added', len(controller.project.revisions) == 2)
                    check('native edit retains exact source offsets',
                          selected_source_offsets(state['controls']['editor'], expected) == (first, first + 5))
                    draft, _ = store.load(project.id, draft=True)
                    check('existing recovery producer retains correction', draft.current_text == expected)
                    press(view.buttons['undo'])
                    check('native Undo restores exact source text', controller.pending_text == source)
                    check('original file and selected dictionary unchanged',
                          source_file.read_bytes() == original_bytes and (aff.read_bytes(), dic.read_bytes()) == pair)
                    # A second real operation proves the owned close/cancel route.
                    view.scope.SetSelection(1)
                    view.initial = spelling.capture_spelling(controller, (first, last))
                    state['controls']['editor'].SetSelection(
                        native_offset(state['controls']['editor'], source, first),
                        native_offset(state['controls']['editor'], source, last))
                    state['phase'] = 'closing'
                    press(view.buttons['review'])
                    check('shared frame owns the asynchronous operation', state['session'].busy)
                    press(view.buttons['close'])
                    check('close requests cancellation through the current session',
                          state['session'].close_requested and state['session'].cancel.is_set())
                elif phase == 'closing':
                    if not state['session'].closed:
                        schedule(); return
                    check('spelling process admission is released after settlement',
                          not ResourceBroker.for_app(root).snapshot()['leases'])
                    check('cancelled repeat did not add a correction', state['controller'].pending_text == source)
                    state['phase'] = 'done'
                    state['controller'].save()
                    state['complete'] = True
                    press(state['controls']['close'])
                    return
                elif phase == 'failed':
                    if state['session'] is not None and not state['session'].closed:
                        schedule(); return
                    if state['reader'] is not None and state['reader'].IsModal():
                        # Close the existing editor only after its worker settled.
                        state['controller'].save()
                        press(state['controls']['close'])
                    return
                schedule()
            except Exception as exc:
                fail(exc)

        def ready(dialog, controller, controls):
            state.update(reader=dialog, controller=controller, controls=controls)
            schedule()

        def spelling_ready(view, session):
            state.update(view=view, session=session)

        def spelling_factory(parent, **kwargs):
            return spelling.show_spellcheck_dialog(parent, **kwargs, on_ready=spelling_ready)

        try:
            result = open_narration_editor(frame, store, project, on_ready=ready,
                                           spelling_factory=spelling_factory)
            check('native flow completed', state['complete'] and not errors)
            check('original content digest retained', result.original_sha256 == sha256(original_bytes).hexdigest())
            check('all submitted operations settled', all(future.done() for future in futures))
        finally:
            state['active'] = False
            frame.cancel_event.set()
            if state['session'] is not None:
                state['session'].cancel.set()
            for timer in timers:
                timer.Stop()
            executor.shutdown(wait=True, cancel_futures=False)
            frame.Destroy()
            if native_handle is not None:
                native_handle.close()
            settled.set()
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'backend': '0.1.7+carr.1',
        'dictionary': 'explicit synthetic aff/dic', 'language_quality': 'NOT_RUN',
        'nvda': 'NOT_RUN'}, sort_keys=True))
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    options = parser.parse_args()
    if not options.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows required.'}))
        return 77
    try:
        return run_native()
    except BaseException:
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

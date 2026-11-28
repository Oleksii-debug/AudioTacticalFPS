"""Native source-range A–B and sleep through the existing live readers.

Source import, source maps, SAPI synthesis, native widgets/keys, the current
LiveReader worker and every cleanup result are real. The PCM output device is
the existing disclosed controlled fixture; the exact unsaved-copy question is
answered NO without changing or saving the copy. This is not physical output,
audible timing, voice quality or NVDA acceptance.
"""
import argparse
from dataclasses import replace
import faulthandler
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import mkdtemp
from time import monotonic, sleep
from types import SimpleNamespace as N
import traceback


def _sibling(name, identity):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(identity, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='Actual native Windows is required.')), flush=True)
        return 77
    if not args.native_child:
        from carr_library.audio.models import AudioBudget
        from carr_library.audio.process import run_process
        outcome = run_process(
            [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
            budget=AudioBudget(timeout=360, max_log_bytes=2 * 1024 * 1024),
            owned_tree=True, allow_failure=True)
        sys.stdout.write(outcome.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
        sys.stderr.write(outcome.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
        return outcome.returncode
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _native()
    finally:
        if runtime is not None:
            runtime.close()
        faulthandler.cancel_dump_traceback_later()


def _native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, durable_state, runtime_settings
    from carr_library.reader import live_reading, live_state, live_player, reading_units
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.speech import live_adapter, reading, session, source, player, listening
    from carr_library.ui import desktop_app, live_reading as live_controls, live_range_controls
    from carr_library.ui import reading_desk as source_ui, speech_dialog
    from carr_library.audio import cleanup, process
    from carr_library.application import LibraryApp

    binding = _sibling('windows_publishing_smoke.py', '_live_range_bindings')
    helpers = _sibling('windows_speech_preparation_smoke.py', '_live_range_owned_modals')
    existing_pcm = _sibling('windows_live_reading_smoke.py', '_live_range_disclosed_pcm')
    modules = (application, durable_state, runtime_settings, live_reading, live_state,
        live_player, reading_units, live_adapter, reading, session, source, player, listening,
        desktop_app, live_controls, live_range_controls, source_ui, speech_dialog, cleanup, process)
    before = binding.verify_installed_sources(*modules)
    script_paths = tuple(Path(__file__).with_name(name) for name in (
        Path(__file__).name, 'windows_publishing_smoke.py',
        'windows_speech_preparation_smoke.py', 'windows_live_reading_smoke.py'))
    script_hashes = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in script_paths}
    checks, errors, peers, cores, players, requests, results, worker_threads = [], [], [], [], [], [], [], []
    frame, timer, failure_handler = None, None, None
    state = N(reader=None, view=None, returned=True, waiting=None, description='',
              entered=False, finished=False, started=monotonic())
    original_factory = live_range_controls.create_live_range_controls
    original_message = wx.MessageBox
    unsaved_answers = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def failed(detail):
        errors.append(detail)
        print('FAIL: live source A–B and sleep\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
                for core in cores:
                    core.stop('The native acceptance observer failed.')
                if timer is not None:
                    timer.Stop()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def inside(peer, ancestor):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer is ancestor:
                return True
            peer = peer.GetParent()
        raise ValueError('Native owner chain exceeds its bound.')

    def owners():
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owner inventory exceeds its bound.')
        modals = [p for p in windows if p and isinstance(p, wx.Dialog) and p.IsModal()
                  and frame and inside(p, frame)]
        hidden = [p for p in modals if not p.IsShown()]
        if hidden:
            chosen = hidden[0]
        elif modals:
            focused = wx.Window.FindFocus()
            top = wx.GetTopLevelParent(focused) if focused else None
            active = [p for p in modals if p.IsEnabled()]
            chosen = next((p for p in active if p is top), None)
            if chosen is None:
                if len(active) != 1:
                    raise RuntimeError('The current owned modal is still returning.')
                chosen = active[0]
        else:
            return ()
        return (N(dialog=chosen, close=lambda value=chosen: value.EndModal(wx.ID_CANCEL)),)

    abort = helpers._OwnedModalAbort(wx, gui, owners, failed)
    def select_failure(value):
        nonlocal failure_handler
        failure_handler = value
    helpers._nested_modal_failure_acceptance(wx, gui, check, select_failure)

    def reviewed_message(*arguments, **options):
        if (len(arguments) == 4 and not options
                and arguments[0] == 'Save the reading copy and its undo history before closing?'
                and arguments[1] == 'Unsaved reading copy'
                and arguments[2] == wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION
                and arguments[3] is state.reader):
            unsaved_answers.append(arguments[1])
            return wx.NO
        return original_message(*arguments, **options)
    wx.MessageBox = reviewed_message

    def pump(predicate, message, seconds=8):
        deadline = monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError('A genuine native callback failed.')
            if monotonic() >= deadline:
                raise TimeoutError(message)
            gui.Yield()
            sleep(.01)

    def focus(control):
        check('named control is alive, visible and enabled',
              bool(control) and control.IsShownOnScreen() and control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle()
             and control.GetParent().GetClientRect().Contains(control.GetRect()),
             'The complete named control did not receive visible foreground focus.')

    def key(code, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down', simulator.KeyDown(code))
            pressed.append(code)
            sleep(.015)
        finally:
            for code in reversed(pressed):
                check('native key released', simulator.KeyUp(code))

    def press(control):
        focus(control)
        key(wx.WXK_SPACE)

    def choose(control, index):
        focus(control)
        key(wx.WXK_HOME)
        for _ in range(index):
            key(wx.WXK_DOWN)
        key(wx.WXK_TAB)
        pump(lambda: control.GetSelection() == index, 'Actual voice selection did not arrive.')

    def enter(control, value):
        focus(control)
        key(ord('A'), (wx.WXK_CONTROL,))
        check('native ASCII numeric text entered', simulator.Text(str(value).encode('ascii')))
        key(wx.WXK_TAB)
        pump(lambda: control.GetValue() == str(value), 'Actual numeric entry did not arrive.')

    def waiting(predicate, message, seconds=30):
        return predicate, message, monotonic() + seconds

    def modal():
        rows = [record for record in peers if record.dialog and record.dialog.IsShown()
                and record.dialog.IsModal()]
        if len(rows) > 1:
            raise ValueError('More than one owned repeat view is active.')
        return rows[0] if rows else None

    def observe_factory(parent, **options):
        view = original_factory(parent, **options)
        record = N(dialog=view, native_id=view.GetId(), deaths=[], returned=False)
        def destroyed(event):
            if event.GetId() == record.native_id:
                record.deaths.append(event.GetId())
            event.Skip()
        view.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        peers.append(record)
        return view

    raw_root = Path(mkdtemp(prefix='resonance-native-live-range-'))
    identity = raw_root.lstat()
    root = durable_state.local_path(raw_root, exists=True)
    check('authored fixture keeps its actual canonical directory identity',
          (root.lstat().st_dev, root.lstat().st_ino) == (identity.st_dev, identity.st_ino))
    library = LibraryApp(root / 'library')
    workspace = library.create_workspace('Native live range acceptance')
    first = ('😀 retained\r\nRepeat exact source words over several genuine speech chunks. '
             'The middle stays untouched. Repeat Ω only after the checked A–B boundary.\r\nEnd.')
    original = first + '\fThis second source location must never be read by repeat.'
    imported = library.add_text(workspace, 'Actual native A–B source', original, language='en')
    desk = ReadingDesk(library, workspace, imported['edition'])
    asset = library.assets.resolve(imported['asset'])
    original_bytes = asset.read_bytes()
    a, b = first.index('Repeat'), first.rindex('End.')
    check('authored exact A–B interval needs two or more actual chunks', b - a > 64)
    frame = desktop_app.DesktopFrame(library)
    frame.SetSize((1180, 980))
    frame.Show()
    gui.SetTopWindow(frame)
    desktop_executor = frame.executor
    live_range_controls.create_live_range_controls = observe_factory

    def render(request, target, **options):
        check('real synthesis uses the selected private library',
              Path(options['configuration_root']) == library.root)
        worker_threads.append(threading.get_ident())
        requests.append(request)
        outcome = live_adapter.render_speech(request, target, **options)
        check('the requested explicit installed SAPI voice produced actual PCM',
              request.voice_id.startswith('sapi5-') and outcome.duration_seconds > 0)
        results.append(outcome)
        return outcome

    def core_factory(sequence, **options):
        pcm = existing_pcm.ControlledPCMPlayer()
        players.append(pcm)
        core = live_reading.LiveReader(sequence, render=render, player_factory=lambda: pcm,
            budget=live_reading.LiveBudget(chunk_characters=64), **options)
        cores.append(core)
        return core

    def loaded():
        return (state.reader and state.view is not None and state.view['live'].ready()
                and not state.view['live'].core.busy and state.view['live']._voices)

    def open_source():
        check('the prior native reader returned before another opens', state.returned)
        state.returned, state.reader, state.view = False, None, None
        def launch():
            try:
                local_desk = existing_pcm.independent_reading_desk(desk)
                reader = source_ui.ReadingDialog(frame, local_desk, ReadingState(library.root),
                    guard=lambda: None, live_factory=core_factory)
                state.reader = reader.dialog
                state.view = dict(live=reader.live, text=reader.text, owner=reader)
                reader.show()
            finally:
                state.returned = True
        wx.CallAfter(launch)

    def open_copy():
        check('the prior native reader returned before another opens', state.returned)
        state.returned, state.reader, state.view = False, None, None
        def launch():
            try:
                local_desk = existing_pcm.independent_reading_desk(desk)
                speech_dialog.show_speech_reader(frame, title='Actual source-bound A–B copy',
                    recovery_slot='native-live-range',
                    source_loader=lambda cancelled: source.read_source_range(local_desk, 1, 1, cancelled=cancelled),
                    live_factory=core_factory,
                    on_ready=lambda dialog, view: (setattr(state, 'reader', dialog), setattr(state, 'view', view)))
            finally:
                state.returned = True
        wx.CallAfter(launch)

    def choose_voice():
        controls = state.view['live']
        check('discovery does not select a voice implicitly', controls.voice.GetSelection() == wx.NOT_FOUND)
        available = [(index, voice) for index, voice in enumerate(controls._voices)
                     if voice.id.startswith('sapi5-')]
        check('an actual installed SAPI voice is available', bool(available))
        index, voice = available[0]
        choose(controls.voice, index)
        return voice.id

    def open_controls():
        button, _ = state.view['live'].command_buttons['reader.repeat_sleep']
        press(button)
        yield waiting(lambda: modal() is not None, 'The real A–B controls button did not open its modal')

    def close_controls(record):
        press(record.dialog.close_button)
        yield waiting(lambda: not record.dialog or not record.dialog.IsModal(),
                      'The repeat modal did not return before its parent')
        yield waiting(lambda: not record.dialog, 'The repeat modal was not destroyed once')
        check('the actual repeat controls receive one native destruction',
              record.deaths == [record.native_id])

    def scenario():
        open_source()
        yield waiting(loaded, 'Actual source reader and voice discovery did not become ready', 60)
        controls = state.view['live']
        core, pcm = controls.core, players[-1]
        voice = choose_voice()
        check('source reader still displays the exact retained page', controls.current() == (1, first))
        yield from open_controls()
        record = modal()
        check('opening repeat controls starts no synthesis or player', not requests and not pcm.opened)
        enter(record.dialog.a, a); enter(record.dialog.b, b)
        press(record.dialog.buttons['repeat'])
        yield waiting(lambda: core.status().state == 'playing', 'Actual first A–B chunk did not play', 65)
        future = core._future
        check('one actual current core owns the selected source range and voice',
              core.navigation_status()['repeat_range'] == (1, a, b)
              and requests[0].voice_id == voice)
        # Device progress is the sole output seam. Each next chunk is genuine
        # current source validation + canonical SAPI synthesis on the same worker.
        while core.navigation_status()['repeat_pass'] < 2:
            opened_count = len(pcm.opened)
            pcm.advance(pcm.duration)
            yield waiting(lambda previous=opened_count: core.status().state == 'error'
                          or (core.status().state == 'playing' and len(pcm.opened) > previous),
                          'The next actual owned A–B chunk did not arrive', 65)
            check('every repeated chunk preserves the same core future',
                  core.status().state != 'error' and core._future is future)
        check('first pass concatenates the exact source with no drops or added text',
              ''.join(request.text for request in requests[:-1]) == first[a:b]
              and requests[-1].text == requests[0].text
              and len(pcm.opened) == len(requests) >= 3
              and core.status().section.ordinal == 1)
        yield from close_controls(record)
        check('closing only repeat controls keeps the same audible owner active',
              core._future is future and core.busy and pcm.stream is not None)
        press(controls.pause_button)
        yield waiting(lambda: core.status().state == 'paused', 'Actual paused state was not acknowledged')
        count = len(requests)
        yield from open_controls()
        record = modal()
        check('busy controls keep range changes disabled and sleep reachable',
              not record.dialog.buttons['repeat'].IsEnabled()
              and record.dialog.buttons['sleep'].IsEnabled())
        enter(record.dialog.seconds, 1)
        press(record.dialog.buttons['sleep'])
        yield waiting(lambda: core.navigation_status()['sleep_remaining'] is not None,
                      'Actual sleep timer request was not admitted')
        yield waiting(lambda: future.done() and core.quiescent, 'Paused sleep did not finish owned cleanup', 65)
        check('monotonic sleep stops paused repeat without resuming or creating another chunk',
              core.status().state == 'stopped' and len(requests) == count
              and pcm.stream is None and pcm.resume_count == 0
              and all(not path.exists() for path in pcm.opened)
              and core.navigation_status()['repeat_range'] is None
              and core.navigation_status()['sleep_remaining'] is None)
        yield from close_controls(record)
        previous = state.view['owner']
        previous.close()
        yield waiting(lambda: state.returned and not state.reader,
                      'Actual source reader did not return and destroy')
        check('source reader closure remains cancelled and keeps the original bytes',
              previous.closed and previous.cancelled.is_set() and asset.read_bytes() == original_bytes)

        open_copy()
        yield waiting(loaded, 'Source-bound reading copy did not load its actual current source', 60)
        controls = state.view['live']
        core, pcm = controls.core, players[-1]
        choose_voice()
        before_document = state.view['document']().to_dict()
        before_count = len(requests)
        yield from open_controls()
        record = modal()
        enter(record.dialog.a, a); enter(record.dialog.b, b)
        press(record.dialog.buttons['repeat'])
        yield waiting(lambda: core.status().state == 'playing', 'Actual reading-copy repeat did not play', 65)
        future = core._future
        retained_repeat, retained_refresh = record.dialog.repeat, record.dialog.refresh
        check('copy repeat preserves the canonical current source map and owner',
              state.view['document']().to_dict() == before_document
              and core.status().section.derivative
              and core.navigation_status()['repeat_range'] == (1, a, b))
        record.dialog.Destroy()
        yield waiting(lambda: not record.dialog, 'Direct native child deletion was not observed')
        check('direct controls deletion destroys exactly one native peer',
              record.deaths == [record.native_id])
        delivered = []
        def late():
            retained_refresh()
            retained_repeat()
            delivered.append(True)
        wx.CallAfter(late)
        yield waiting(lambda: delivered == [True], 'Actual queued withdrawn actions did not return')
        check('withdrawn child callbacks do not start, stop or replace the retained player',
              core._future is future and core.busy and len(requests) == before_count + 1
              and pcm.stream is not None and not controls._range_listeners)
        press(controls.stop_button)
        yield waiting(lambda: future.done() and core.quiescent, 'Actual owning Stop did not confirm cleanup', 65)
        check('canonical reading copy and every original byte remain unchanged after Stop',
              state.view['document']().to_dict() == before_document
              and asset.read_bytes() == original_bytes and pcm.stream is None)
        press(state.view['buttons']['close'])
        yield waiting(lambda: state.returned and not state.reader,
                      'The actual canonical reader did not close')
        check('only the exact current unsaved-copy question receives the disclosed NO answer',
              unsaved_answers == ['Unsaved reading copy'])
        check('one existing Desktop executor and one worker per reader remain selected',
              frame.executor is desktop_executor and len(cores) == 2 and len(players) == 2
              and len(set(worker_threads)) <= 2 and threading.get_ident() not in worker_threads
              and all(core.quiescent and not core.busy for core in cores))
        check('no private current reading audio remains after completed cleanup',
              not list(library.root.glob('.live-reading-*')) and asset.read_bytes() == original_bytes)
        state.finished = True
        gui.ExitMainLoop()

    generator = scenario()
    def tick(event):
        if state.entered:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 270:
                raise TimeoutError('Native live range scenario exceeded its bound: ' + state.description)
            for core in cores:
                view = core.status()
                if view.state == 'error':
                    raise AssertionError('Actual live reader failed: ' + view.message + '; ' + view.warning)
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(generator)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('The native scenario ended without its final proof.')
        except (Exception, KeyboardInterrupt):
            timer.Stop()
            failed(traceback.format_exc())
            for core in cores:
                core.stop('The native acceptance observer failed.')
            abort.request()
        finally:
            state.entered = False

    timer = wx.Timer(frame)
    frame.Bind(wx.EVT_TIMER, tick, timer)
    timer.Start(25)
    try:
        gui.MainLoop()
    finally:
        timer.Stop()
        timer.SetOwner(None)
        abort.stop()
        live_range_controls.create_live_range_controls = original_factory
        wx.MessageBox = original_message
        for core in cores:
            completion = core.close()
            if completion is not None:
                completion.result(65)
        if frame:
            frame._closing = True
            frame.cancel_event.set()
        desktop_executor.shutdown(wait=True, cancel_futures=False)
        if not library.close_search():
            raise RuntimeError('The current library cursor owner has not confirmed cleanup.')
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
    if errors or not state.finished:
        print(json.dumps(dict(status='FAIL', fixture=str(root), checks=len(checks),
                              errors=errors, waiting=state.description), ensure_ascii=False), flush=True)
        return 1
    check('every selected installed product byte still matches its source',
          binding.verify_installed_sources(*modules) == before)
    check('all selected native scripts keep their exact executed bytes',
          {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in script_paths} == script_hashes)
    check('current library cursor cleanup completes without another worker', library.close_search())
    import shutil
    shutil.rmtree(root)
    print(json.dumps(dict(status='PASS', checks=len(checks), selected_sources=before,
        scripts=script_hashes, source_sha256=sha256(original_bytes).hexdigest(),
        actual_sapi_renders=[dict(voice_id=item.voice_id, sha256=item.sha256,
                                 duration_seconds=item.duration_seconds) for item in results],
        scope='ACTUAL_NATIVE_SOURCE_AND_COPY_A_B_SLEEP',
        player='CONTROLLED_PCM_TEST_FIXTURE', unsaved_close='CONTROLLED_EXACT_NO_ANSWER',
        physical_output='NOT_RUN',
        audible_timing='NOT_RUN', voice_quality='NOT_RUN', NVDA='NOT_RUN'), ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Actual native current-reader to Assistant handoff and durable player receipts.

Actual SAPI5 produces the PCM. The existing ControlledPCMPlayer is a disclosed
device fixture: this proves native windows, exact source/session authority,
queued withdrawal and cleanup, not physical output, WinMM or NVDA quality.
"""
import argparse
import faulthandler
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import traceback


def sibling(name):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location('_assistant_player_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path, sha256(path.read_bytes()).hexdigest()


def native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.live_reading import LiveReader
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.speech.live_adapter import discover_voices, render_speech
    from carr_library.ui.reading_desk import ReadingDialog
    from carr_library.assistant import player_tools, service, models
    from carr_library.reader import live_reading, reading_desk
    from carr_library.speech import live_adapter
    from carr_library.ui import assistant_dialog, live_reading as native_controls
    from carr_library.ui import reading_desk as native_reader

    controls_probe, controls_path, controls_digest = sibling('windows_live_reading_smoke.py')
    modal_probe, modal_path, modal_digest = sibling('windows_epub_reading_smoke.py')
    bindings, bindings_path, bindings_digest = sibling('windows_publishing_smoke.py')
    modules = (player_tools, service, models, live_reading, reading_desk, live_adapter,
               assistant_dialog, native_controls, native_reader)
    identities = bindings.verify_installed_sources(*modules)
    checks, errors, failure_handler = [], [], None
    abort = None

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def failed(detail):
        errors.append(detail)
        print(detail, file=sys.stderr, flush=True)
        if abort is not None:
            abort.request()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            (failure_handler or failed)(traceback.format_exc())
            return True

    app = ProbeApp(False)

    def select_failure_handler(callback):
        nonlocal failure_handler
        failure_handler = callback

    modal_probe._nested_modal_failure_acceptance(wx, app, check, select_failure_handler)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    simulator = wx.UIActionSimulator()

    with TemporaryDirectory(prefix='carr-native-assistant-player-') as temporary:
        library = LibraryApp(Path(temporary) / 'Українська бібліотека')
        workspace = library.create_workspace('Current source and its assistant')
        raw = 'Є 🚂.\r\nCurrent source reading remains owned by this reader.'
        book = library.add_text(workspace, 'One exact retained source', raw, language='uk')
        desk = ReadingDesk(library, workspace, book['edition'])
        source_text = desk.page(1).raw_text
        original = library.assets.resolve(desk.identity.asset_sha256).read_bytes()
        fixture = controls_probe.ControlledPCMPlayer()
        rendered, discovered, created = [], [], []

        def discover(**kwargs):
            assert Path(kwargs['configuration_root']) == library.root
            discovered.append(True)
            return discover_voices(**kwargs)

        def render(request, target, **kwargs):
            assert Path(kwargs['configuration_root']) == library.root
            result = render_speech(request, target, **kwargs)
            assert request.voice_id.startswith('sapi5-') and result.duration_seconds > 0
            assert request.text == source_text
            rendered.append({'sha256': result.sha256, 'voice_id': result.voice_id,
                             'duration_seconds': result.duration_seconds})
            return result

        def player():
            created.append(fixture)
            return fixture

        # The actual top-level host exists before MainLoop. It owns only this
        # library and native source window; no alternate playback integration.
        frame = wx.Frame(None, title='Current reader Assistant acceptance', size=(1100, 950))
        frame.library, frame.active, frame._closing = library, (workspace, None), False
        frame.Show()
        reader = ReadingDialog(frame, desk, ReadingState(library.root), guard=lambda: None,
            live_factory=lambda source, **kwargs: LiveReader(source, discover=discover,
                render=render, player_factory=player, **kwargs))
        controls = reader.live
        core = controls.core
        timer = wx.Timer(reader.dialog)
        state = {'phase': 'loading', 'assistant': None, 'key_pending': False,
                 'held': [], 'hold_next': False, 'completed_deliveries': [],
                 'late_delivered': False, 'contexts': [], 'finished': False}
        source_native_id, source_destroyed = reader.dialog.GetId(), []
        deadline = monotonic() + 120
        real_call_after = wx.CallAfter

        def source_destroy(event):
            if event.GetId() == source_native_id:
                source_destroyed.append(event.GetId())
            event.Skip()
        reader.dialog.Bind(wx.EVT_WINDOW_DESTROY, source_destroy)

        def owner_stack():
            context = state['assistant']
            nested = (SimpleNamespace(dialog=context['dialog'], close=context['close'])
                      if context is not None else None)
            return (nested, reader)

        abort = modal_probe._OwnedModalAbort(wx, app, owner_stack, failed)

        def intercepted(callback, *args, **kwargs):
            module = getattr(callback, '__module__', '')
            name = getattr(callback, '__qualname__', '')
            if (state['hold_next'] and module == player_tools.__name__
                    and name.endswith('LivePlayerBridge._request.<locals>.on_ui.<locals>.dispatch')):
                state['hold_next'] = False
                state['held'].append((callback, args, kwargs))
                return None
            if (module == assistant_dialog.__name__
                    and name.endswith('create_assistant_dialog.<locals>.run.<locals>.finish.<locals>.apply')):
                # Observe delivery of the actual completion callback, including
                # the dead-owner no-op. Never substitute its result or queue.
                def delivered():
                    try:
                        callback(*args, **kwargs)
                    finally:
                        state['completed_deliveries'].append(True)
                return real_call_after(delivered)
            return real_call_after(callback, *args, **kwargs)

        wx.CallAfter = intercepted

        def press(control):
            assert control and control.IsEnabled() and not state['key_pending']
            top = wx.GetTopLevelParent(control)
            assert top and top.IsShown()
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
            state['key_pending'] = True
            key_record = {'label': control.GetLabel()[:200], 'native_id': control.GetId(),
                          'down': None, 'up': None}
            state['last_key'] = key_record
            def key():
                try:
                    if errors:
                        return
                    assert control and top and wx.Window.FindFocus() is control
                    assert user32.GetForegroundWindow() == top.GetHandle()
                    key_record['down'] = bool(simulator.KeyDown(wx.WXK_SPACE))
                    key_record['up'] = bool(simulator.KeyUp(wx.WXK_SPACE))
                except (Exception, KeyboardInterrupt):
                    failed(traceback.format_exc())
                finally:
                    state['key_pending'] = False
            wx.CallLater(60, key)

        def opening():
            choices = [item for item in reader.dialog.GetChildren()
                       if isinstance(item, wx.Dialog) and item and item.IsModal()
                       and hasattr(item, 'assistant_controls')]
            if not choices:
                return None
            assert len(choices) == 1
            dialog = choices[0]
            current = {'dialog': dialog, 'service': dialog.assistant_service,
                       'controls': dialog.assistant_controls,
                       'close': dialog.assistant_handlers['close'],
                       'native_id': dialog.GetId(), 'destroyed': []}
            def destroyed(event):
                if event.GetId() == current['native_id']:
                    current['destroyed'].append(event.GetId())
                event.Skip()
            dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            check('real Assistant is bound to this source reader and its existing core',
                  current['service'].player.controls is controls
                  and current['service'].player.core is core
                  and current['service'].player.source is controls.source
                  and current['controls']['scope'].GetStringSelection()
                      == 'Current source and its assistant')
            state['contexts'].append(current)
            state['assistant'] = current
            return current

        def plan(label, tool, arguments=None, *, hold=False):
            current = state['assistant']
            widgets = current['controls']
            assert current['dialog'] and widgets['instruction'].IsEnabled()
            state['plan'] = {'label': label, 'tool': tool, 'hold': hold,
                             'before': set(current['service'].store.identities())}
            widgets['instruction'].SetValue(json.dumps(
                {'steps': [{'tool': tool, 'arguments': arguments or {}}]}, ensure_ascii=False))
            state['phase'] = 'preview'
            press(widgets['buttons']['preview'])

        def control(action, receipt):
            return {'action': action, 'session': receipt['session'],
                    'source_sha256': receipt['source_sha256']}

        def check_receipt(row, action):
            assert row.status == 'complete' and row.completed == row.total == 1, row
            assert len(row.results) == 1
            result = row.results[0]
            check('actual durable ' + action + ' receipt binds the exact source revision',
                  result['schema'] == 'carr-current-player-receipt/1'
                  and result['action'] == action and result['workspace'] == workspace
                  and result['edition'] == desk.identity.edition_id
                  and result['revision'] == desk.identity.revision
                  and result['asset_sha256'] == desk.identity.asset_sha256
                  and result['source_text_sha256'] == sha256(source_text.encode('utf-8')).hexdigest()
                  and result['artifacts'] == [])
            assert core._future is state['reading_future'] and created == [fixture]
            return result

        def finish_plan(row):
            label = state['plan']['label']
            if label == 'first_status':
                result = check_receipt(row, 'status')
                check('status observes playing without creating a player', result['state'] == 'playing')
                state['first_receipt'] = result
                plan('withdraw_stop', 'player.control', control('stop', result), hold=True)
            elif label == 'second_status':
                result = check_receipt(row, 'status')
                check('reopened Assistant receives its own reviewed session',
                      result['session'] != state['first_receipt']['session']
                      and result['source_sha256'] == state['first_receipt']['source_sha256']
                      and result['state'] == 'playing')
                state['receipt'] = result
                plan('pause_once', 'player.control', control('pause', result))
            elif label in ('pause_once', 'pause_twice'):
                result = check_receipt(row, 'pause')
                check('Pause acknowledges the existing quiet operation without Resume',
                      result['state'] == 'paused' and core.status().state == 'paused'
                      and core.busy and core.quiescent and fixture.resume_count == 0)
                if label == 'pause_once':
                    state['paused_position'] = fixture.position
                    plan('pause_twice', 'player.control', control('pause', state['receipt']))
                else:
                    check('a second native reviewed Pause leaves the same position paused',
                          fixture.position == state['paused_position'] and fixture.resume_count == 0)
                    plan('stop', 'player.control', control('stop', state['receipt']))
            elif label == 'stop':
                result = check_receipt(row, 'stop')
                check('Stop completes only after the existing future and PCM resources settle',
                      result['state'] == 'stopped' and result['quiescent']
                      and not result['busy'] and not result['cleanup_pending']
                      and not core.busy and core.quiescent and core._retained_resources is None
                      and fixture.stream is None
                      and all(not path.exists() for path in fixture.opened))
                state['phase'] = 'closing_success'
                press(state['assistant']['controls']['buttons']['close'])
            else:
                raise AssertionError('Unexpected completed native plan: ' + label)

        def diagnostic_snapshot():
            current, selected = state['assistant'], state.get('plan', {})
            details = {'phase': state['phase'], 'plan_label': selected.get('label'),
                       'plan_tool': selected.get('tool'), 'key_pending': state['key_pending'],
                       'last_key': state.get('last_key'), 'hold_next': state['hold_next'],
                       'held_callbacks': len(state['held']),
                       'completion_deliveries': len(state['completed_deliveries'])}
            if current is not None:
                live = bool(current['dialog'])
                details['assistant_alive'] = live
                if live:
                    widgets = current['controls']
                    details['enabled'] = {name: widgets['buttons'][name].IsEnabled()
                                          for name in ('preview', 'run', 'pause', 'close')}
                    details['instruction_enabled'] = widgets['instruction'].IsEnabled()
                    details['instruction'] = widgets['instruction'].GetRange(0, 2048)
                    details['review'] = widgets['review'].GetRange(0, 4096)
                    focus = wx.Window.FindFocus()
                    details['focus'] = ({'id': focus.GetId(), 'name': focus.GetName()[:200]}
                                        if focus else None)
                identities = tuple(current['service'].store.identities())
                fresh = set(identities) - selected.get('before', set())
                details['saved_job_count'] = len(identities)
                details['fresh_job_count'] = len(fresh)
                details['fresh_job_ids'] = sorted(fresh)[:8]
                details['saved_jobs'] = []
                for identity in sorted(identities)[:8]:
                    row = current['service'].get(identity)
                    details['saved_jobs'].append({'id': identity, 'status': row.status,
                        'completed': row.completed, 'total': row.total,
                        'message': row.message[:2048]})
            view = core.status()
            details['reader'] = {'state': view.state, 'busy': core.busy,
                                 'quiescent': core.quiescent,
                                 'pause_requested': core._pause,
                                 'same_future': core._future is state.get('reading_future'),
                                 'fixture_state': fixture.state,
                                 'fixture_resume_count': fixture.resume_count}
            return details

        def advance(event):
            if errors or state['finished']:
                return
            try:
                assert monotonic() < deadline, 'Current reader Assistant acceptance timed out.'
                if state['key_pending']:
                    return
                phase = state['phase']
                if phase == 'loading':
                    if reader.controller.current is None or not controls._voices or core.busy:
                        return
                    check('native source reader requires explicit actual SAPI voice',
                          controls.voice.GetSelection() == wx.NOT_FOUND)
                    choices = [(index, voice) for index, voice in enumerate(controls._voices)
                               if voice.id.startswith('sapi5-')]
                    assert choices, 'No real SAPI5 voice is installed.'
                    controls.voice.SetSelection(choices[0][0])
                    controls.continuous.SetValue(False)
                    state['phase'] = 'playing'
                    press(controls.command_buttons['reader.speech_all'][0])
                elif phase == 'playing':
                    if core.status().state == 'error':
                        raise AssertionError(core.status().message)
                    if core.status().state != 'playing':
                        return
                    check('real SAPI prepared one actual PCM for the existing source core',
                          len(rendered) == len(fixture.opened) == 1 and discovered
                          and fixture.stream is not None)
                    state['reading_future'] = core._future
                    state['phase'] = 'opening_first'
                    press(controls.command_buttons['reader.assistant_tools'][0])
                elif phase in ('opening_first', 'opening_second'):
                    if opening() is not None:
                        plan('first_status' if phase == 'opening_first' else 'second_status', 'player.status')
                elif phase == 'preview':
                    current = state['assistant']
                    if not current['controls']['buttons']['run'].IsEnabled():
                        return
                    assert state['plan']['tool'] in current['controls']['review'].GetValue()
                    state['phase'] = 'running'
                    state['hold_next'] = state['plan']['hold']
                    press(current['controls']['buttons']['run'])
                elif phase == 'running':
                    current = state['assistant']
                    fresh = set(current['service'].store.identities()) - state['plan']['before']
                    if not fresh:
                        return
                    assert len(fresh) == 1
                    identity = next(iter(fresh))
                    if state['plan']['hold']:
                        if not state['held']:
                            return
                        assert len(state['held']) == 1
                        state['withdrawn_job'] = identity
                        state['deliveries_before_close'] = len(state['completed_deliveries'])
                        state['phase'] = 'closing_withdrawn'
                        press(current['controls']['buttons']['close'])
                        return
                    row = current['service'].get(identity)
                    if row.status in ('queued', 'running'):
                        return
                    if not current['controls']['instruction'].IsEnabled():
                        return  # Require actual UI completion delivery as well.
                    assert row.status == 'complete', (row, current['controls']['review'].GetValue())
                    finish_plan(row)
                elif phase == 'closing_withdrawn':
                    current = state['assistant']
                    if current['dialog']:
                        return
                    row = current['service'].get(state['withdrawn_job'])
                    if row.status in ('queued', 'running'):
                        return
                    if len(state['completed_deliveries']) <= state['deliveries_before_close']:
                        return
                    record = current['service'].store.load(row.id)
                    check('native Assistant closure withdraws its genuine queued Stop',
                          current['destroyed'] == [current['native_id']]
                          and current['service'].player.closed.is_set()
                          and row.status == 'paused' and row.completed == 0
                          and record['steps'][0]['intent']['player_dispatch']['action'] == 'stop'
                          and 'player_dispatch_complete' not in record['steps'][0]['intent']
                          and core._future is state['reading_future']
                          and core.status().state == 'playing' and fixture.resume_count == 0)
                    callback, args, kwargs = state['held'][0]
                    def replay():
                        assert not current['dialog']
                        callback(*args, **kwargs)
                        state['late_delivered'] = True
                    real_call_after(replay)
                    state['phase'] = 'late_withdrawn'
                elif phase == 'late_withdrawn':
                    if not state['late_delivered']:
                        return
                    check('actual late CallAfter cannot dispatch a withdrawn command',
                          core.status().state == 'playing' and core.busy
                          and core._future is state['reading_future'] and fixture.resume_count == 0)
                    state['phase'] = 'opening_second'
                    press(controls.command_buttons['reader.assistant_tools'][0])
                elif phase == 'closing_success':
                    current = state['assistant']
                    if current['dialog']:
                        return
                    check('completed native Assistant owns one destruction event',
                          current['destroyed'] == [current['native_id']])
                    state['finished'] = True
                    timer.Stop()
                    reader.close()
                else:
                    raise AssertionError('Unexpected native stage: ' + phase)
            except (Exception, KeyboardInterrupt):
                detail = traceback.format_exc()
                try:
                    print('NATIVE_ASSISTANT_FAILURE ' + json.dumps(diagnostic_snapshot(),
                          ensure_ascii=True, sort_keys=True), flush=True)
                except (Exception, KeyboardInterrupt) as diagnostic_error:
                    print('NATIVE_ASSISTANT_DIAGNOSTIC_ERROR ' + str(diagnostic_error)[:1024],
                          flush=True)
                failed(detail)

        def show_reader():
            try:
                reader.show()
            except (Exception, KeyboardInterrupt):
                failed(traceback.format_exc())
            finally:
                completion = core.close()
                if completion is not None:
                    completion.result(timeout=65)
                if frame:
                    frame.Destroy()
                app.ExitMainLoop()

        reader.dialog.Bind(wx.EVT_TIMER, advance, timer)
        timer.Start(40)
        try:
            real_call_after(show_reader)
            app.MainLoop()
        finally:
            timer.Stop()
            abort.stop()
            wx.CallAfter = real_call_after
            completion = core.close()
            if completion is not None:
                completion.result(timeout=65)
            if frame:
                frame.Destroy()
        if errors:
            raise AssertionError('\n'.join(errors))
        # Process genuine pending native destruction before claiming cleanup.
        end = monotonic() + 5
        while reader.dialog and monotonic() < end:
            app.Yield()
        check('source reader closes once after its current owner has settled',
              state['finished'] and not reader.dialog and controls.closed
              and source_destroyed == [source_native_id] and not core.busy
              and fixture.stream is None and len(rendered) == len(created) == 1)
        check('all original source bytes and pinned source identity remain unchanged',
              library.assets.resolve(desk.identity.asset_sha256).read_bytes() == original
              and sha256(original).hexdigest() == desk.identity.asset_sha256)
        current_desk = controls_probe.independent_reading_desk(desk)
        assert current_desk.page(1).raw_text == source_text
        check('all native Assistant owners are deleted and their bridges withdrawn',
              len(state['contexts']) == 2
              and all(not item['dialog'] and item['destroyed'] == [item['native_id']]
                      and item['service'].player.closed.is_set() for item in state['contexts']))
        assert bindings.verify_installed_sources(*modules) == identities
        for path, digest in ((controls_path, controls_digest), (modal_path, modal_digest),
                             (bindings_path, bindings_digest)):
            assert sha256(path.read_bytes()).hexdigest() == digest
        print(json.dumps({'status': 'PASS', 'checks': checks, 'installed_sources': identities,
            'actual_sapi_renders': rendered, 'player': 'CONTROLLED_PCM_TEST_FIXTURE',
            'physical_audio_device': 'NOT_RUN', 'actual_winmm': 'NOT_RUN', 'nvda': 'NOT_RUN',
            'provider_requests': 0, 'new_playback_owners': 0,
            'scope': 'Actual native SourceSequence reader and Assistant; existing one core/worker/device.'},
            ensure_ascii=False), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('Actual Windows is required; no native PASS is available here.')
    if not options.native_child:
        from carr_library.audio.models import AudioBudget
        from carr_library.audio.process import run_process
        result = run_process([sys.executable, '-I', '-B', '-X', 'utf8',
            str(Path(__file__).resolve()), '--native-child'],
            budget=AudioBudget(timeout=210, max_log_bytes=1024 * 1024),
            owned_tree=True, allow_failure=True)
        sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
        sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
        return result.returncode
    faulthandler.dump_traceback_later(180, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        return native()
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())

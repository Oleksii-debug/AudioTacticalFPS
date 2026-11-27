"""Actual native Audio Studio save/reopen, labels, layout and keyboard probe.

Uses explicit synthetic PCM voices with the real Director, FFmpeg and durable
store. No audio is played. A successful run proves native controls and measured
render/checkpoint behavior, not installed-voice quality, listening or NVDA.
Run the installed package with ``python -I <this-file> --require-windows``.
"""
from argparse import ArgumentParser
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from threading import Event
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import traceback
import wave


def run_probe(wx, *, native_active):
    from carr_library.application import LibraryApp
    from carr_library.studio import ProjectStore
    from carr_library.ui import studio_actions, provider_dialog
    from carr_library.studio import director as director_module
    from carr_library.studio.store import digest_file
    from carr_library.progress import current_progress

    checks, failures, timers = [], [], []
    gui = wx.App(False)
    simulator = wx.UIActionSimulator()

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def event(button):
        command = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        command.SetEventObject(button)
        check('native event handled: ' + button.GetLabel().replace('&', ''),
              button.GetEventHandler().ProcessEvent(command))

    def later(controller, operation, delay=40):
        def guarded():
            if failures:
                return
            try:
                operation()
            except BaseException:
                failures.append(traceback.format_exc())
                if controller.dialog.IsModal():
                    controller.dialog.EndModal(wx.ID_CANCEL)
        timers.append(wx.CallLater(delay, guarded))

    def wait_for(controller, predicate, operation, *, deadline=None):
        deadline = time.monotonic() + 30 if deadline is None else deadline
        if predicate():
            operation()
        elif time.monotonic() >= deadline:
            raise AssertionError('The actual Studio worker did not reach its expected checkpoint.')
        else:
            later(controller, lambda: wait_for(controller, predicate, operation, deadline=deadline))

    with TemporaryDirectory(prefix='carr-native-studio-') as temporary:
        library = LibraryApp(Path(temporary) / 'private library')
        workspace = library.create_workspace('Synthetic native studio fixture')
        library.add_text(workspace, 'Source remains immutable', 'Український оригінал. Slovenský pôvodný text.')
        originals = {p.name: sha256(p.read_bytes()).hexdigest() for p in library.assets_dir.iterdir()}
        frame = wx.Frame(None, title='Audio Studio native test host', size=(950, 850))
        frame.library = library
        frame.menu_bar = wx.MenuBar()
        frame.SetMenuBar(frame.menu_bar)
        provider_dialog.install_provider_actions(frame)
        tools_menu = frame.menu_bar.GetMenu(frame.menu_bar.FindMenu('Tools'))
        check('optional provider menu hook is installed', any(
            item.GetItemLabelText() == 'Optional providers and API keys' for item in tools_menu.GetMenuItems()))
        check('installing provider menu does not open settings or credentials', not (library.root / 'provider-settings').exists())
        frame.Show()
        voices = (SimpleNamespace(name='Synthetic voice metadata', language='uk', engine='fixture', id='fixture-voice'),)
        text = 'Українська оповідь. Slovenský dialóg. English narration.'
        first = studio_actions.StudioDialog(frame, voices, initial_text=text)
        root = first.store.root

        def invalid_draft():
            first.mode.SetSelection(2)
            first.editor.SetValue('{retain this invalid authored JSON draft')
            event(first.actions[0])
            check('invalid draft remains editable', first.editor.GetValue() == '{retain this invalid authored JSON draft')
            check('invalid draft does not replace saved revision', first.store.load().revision == first.saved_revision)
            check('failed save is readable and focused', 'Could not save' in first.status.GetValue()
                  and wx.Window.FindFocus() is first.status)
            first.mode.SetSelection(0)
            first.editor.SetValue(text)
            event(first.actions[0])
            first.close()

        def backward_tab():
            check('Shift Tab returns to named status', wx.Window.FindFocus() is first.status)
            invalid_draft()

        def forward_tab():
            focus = wx.Window.FindFocus()
            check('Tab leaves read-only multiline status', focus is not None and focus is not first.status)
            simulator.KeyDown(wx.WXK_SHIFT)
            simulator.Char(wx.WXK_TAB)
            simulator.KeyUp(wx.WXK_SHIFT)
            later(first, backward_tab)

        def saved():
            project = first.store.load()
            check('actual Unicode project save', project.scenes[0].lines[0].text == text)
            check('saved status receives native focus', wx.Window.FindFocus() is first.status)
            check('status is keyboard accessible', first.status.AcceptsFocusFromKeyboard())
            rows = {button.GetPosition().y for button in first.actions}
            check('eight actions occupy two rows', len(rows) == 2)
            client = first.dialog.GetClientSize()
            display = wx.Display.GetFromWindow(first.dialog)
            area = wx.Display(display).GetClientArea()
            bounds = first.dialog.GetScreenRect()
            check('Studio remains inside the monitor work area',
                  bounds.x >= area.x and bounds.y >= area.y
                  and bounds.GetRight() <= area.GetRight() and bounds.GetBottom() <= area.GetBottom())
            check('long editor form scrolls separately from fixed controls', isinstance(first.form, wx.ScrolledWindow))
            check('all studio actions fit the dialog', all(button.GetRect().GetRight() <= client.width
                  and button.GetRect().GetBottom() <= client.height for button in first.actions))
            simulator.Char(wx.WXK_TAB)
            later(first, forward_tab)

        def begin():
            check('native editor label', first.editor.GetName() == 'Narration derivative or authored script')
            check('native status label', first.status.GetName() == 'Audio Studio status')
            check('native mode label', first.mode.GetName() == 'Narration editor mode')
            check('role voice and position labels', all(name.GetName() and voice.GetName() and pan.GetName()
                  for name, voice, pan in first.cast))
            check('selected source text starts as an unsaved derivative', first.saved_form is None)
            event(first.actions[0])
            later(first, saved)

        try:
            later(first, begin, 120)
            watchdog = wx.CallLater(12000, lambda: (failures.append('Native studio probe exceeded its time budget.'),
                first.dialog.EndModal(wx.ID_CANCEL) if first.dialog.IsModal() else None))
            first.show()
            watchdog.Stop()
            if not failures:
                reopened = studio_actions.StudioDialog(frame, voices, store=ProjectStore(root))
                original_director = director_module.Director
                entered, release = Event(), Event()
                control = {'armed': True}
                observations, speech_calls = [], []

                def synthetic_speech(request, target, cancelled=None):
                    if cancelled is not None:
                        cancelled()
                    speech_calls.append(request.text)
                    with wave.open(str(target), 'wb') as output:
                        output.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
                        output.writeframes(b'\x00\x00' * 2400)
                    return dict(path=str(target), frames=2400, sample_rate=24000, channels=1,
                        duration_seconds=.1, sha256=digest_file(target), engine_fingerprint='native-pcm-fixture')

                def measured_engine(store, *, configuration_root):
                    check('Studio render keeps its explicit library configuration root', configuration_root == library.root)
                    engine = original_director(store, speech_renderer=synthetic_speech,
                        engine_identity=lambda _: 'native-pcm-fixture')
                    actual_render = engine.render
                    def render(project, *, progress, **kwargs):
                        def observe(state):
                            progress(state)
                            value = current_progress().snapshot()
                            observations.append((value.completed, value.total, value.phase, value.percent))
                            if control['armed'] and state['speech_chunks_done'] == 1:
                                control['armed'] = False
                                entered.set()
                                if not release.wait(30):
                                    raise TimeoutError('Native Studio checkpoint barrier was not released.')
                        return actual_render(project, progress=observe, **kwargs)
                    engine.render = render
                    return engine

                def cancelled():
                    task = reopened.progress_panel.active_task
                    check('native cancellation is acknowledged after checkpoint', task.snapshot().state == 'cancelled')
                    check('cancelled render never reaches100', task.snapshot().percent < 100)
                    check('actual durable cancellation receipt', reopened.store.jobs()[0]['state'] == 'cancelled')
                    reopened.close()

                def cancel_at_checkpoint():
                    event(reopened.actions[4])
                    check('native cancel reports request before stop acknowledgement',
                          reopened.progress_panel.active_task.snapshot().state == 'cancelling')
                    release.set()
                    wait_for(reopened, lambda: not reopened.busy, cancelled)

                def completed():
                    snapshot = reopened.progress_panel.active_task.snapshot()
                    check('native shared progress reaches100 after successful render',
                          snapshot.state == 'succeeded' and snapshot.percent == 100)
                    check('real Studio result has durable master and manifest',
                          reopened.store.jobs()[0]['state'] == 'complete'
                          and Path(reopened.result['master']['path']).is_file()
                          and Path(reopened.result['manifest_path']).is_file())
                    check('known synthesis units match actual split and retained chunks',
                          snapshot.completed == snapshot.total == len(speech_calls))
                    check('finalization remained99 until durable return',
                          any(phase == 'Finalizing audio' and percent == 99
                              for _, _, phase, percent in observations))
                    project = reopened.store.load()
                    scene = project.scenes[0]
                    changed = replace(project, scenes=(replace(scene, lines=(
                        replace(scene.lines[0], text=scene.lines[0].text + ' Cancel this authored revision.'),)),))
                    reopened.editor.SetValue(json.dumps(changed.to_dict(), ensure_ascii=False))
                    entered.clear()
                    release.clear()
                    control['armed'] = True
                    event(reopened.actions[2])
                    wait_for(reopened, entered.is_set, cancel_at_checkpoint)

                def paused():
                    snapshot = reopened.progress_panel.active_task.snapshot()
                    check('native pause follows retained actual checkpoint',
                          snapshot.state == 'paused' and reopened.store.jobs()[0]['state'] == 'paused')
                    check('paused progress retains measured count without ETA',
                          snapshot.completed == 1 and snapshot.percent < 100 and snapshot.eta_seconds is None)
                    task = reopened.progress_panel.active_task
                    event(reopened.actions[2])
                    check('native resume starts a fresh measured validation pass',
                          reopened.progress_panel.active_task is not task and task.snapshot().state == 'paused')
                    wait_for(reopened, lambda: not reopened.busy, completed)

                def pause_at_checkpoint():
                    reopened.progress_panel.refresh()
                    snapshot = reopened.progress_panel.active_task.snapshot()
                    check('native gauge reflects accepted measured speech units',
                          snapshot.completed == 1 and snapshot.total > 1
                          and reopened.progress_panel.gauge.GetValue() == snapshot.percent)
                    event(reopened.actions[3])
                    check('native pause request does not claim an acknowledged pause',
                          reopened.progress_panel.active_task.snapshot().state == 'running')
                    event(reopened.progress_panel.query_button)
                    check('query progress is keyboard focusable during render', wx.Window.FindFocus() is reopened.status)
                    release.set()
                    wait_for(reopened, lambda: not reopened.busy, paused)

                def reopened_checks():
                    check('actual saved project reopened in JSON mode', reopened.mode.GetSelection() == 2)
                    check('reopened text preserves exact Unicode', reopened.project().scenes[0].lines[0].text == text)
                    check('reopened form is clean', reopened._form_state() == reopened.saved_form)
                    check('reopened editor receives focus', wx.Window.FindFocus() is reopened.editor)
                    project = reopened.store.load()
                    project = replace(project, budget=replace(project.budget, chunk_characters=32))
                    reopened.editor.SetValue(json.dumps(project.to_dict(), ensure_ascii=False))
                    event(reopened.actions[2])
                    check('native render owns a shared progress task', reopened.busy
                          and reopened.progress_panel.active_task.snapshot().state == 'running')
                    wait_for(reopened, entered.is_set, pause_at_checkpoint)

                director_module.Director = measured_engine
                try:
                    later(reopened, reopened_checks, 100)
                    watchdog = wx.CallLater(100000, lambda: (release.set(),
                        failures.append('Native Studio progress probe exceeded its time budget.'),
                        reopened.dialog.EndModal(wx.ID_CANCEL) if reopened.dialog.IsModal() else None))
                    reopened.show()
                    watchdog.Stop()
                finally:
                    release.set()
                    director_module.Director = original_director
            check('library source bytes are unchanged', originals == {
                p.name: sha256(p.read_bytes()).hexdigest() for p in library.assets_dir.iterdir()})
        finally:
            for timer in timers:
                timer.Stop()
            frame.Destroy()
            gui.ProcessPendingEvents()

    report = dict(status='FAIL' if failures else 'PASS', platform=platform.system(), python=platform.python_version(),
                  wx_version=wx.version(), native_runtime_active=native_active, checks=checks, errors=failures,
                  synthesis='SYNTHETIC_PCM_FIXTURE', listening='NOT_RUN', windows_nvda='NOT_RUN',
                  studio_module_sha256=sha256(Path(studio_actions.__file__).read_bytes()).hexdigest())
    print(json.dumps(report, ensure_ascii=False))
    return 1 if failures else 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--require-windows', action='store_true')
    args = parser.parse_args()
    if args.require_windows and sys.platform != 'win32':
        raise RuntimeError('This probe requires actual native Windows execution.')
    from carr_library.native_runtime import activate_bundled_runtime, _discover
    handle = activate_bundled_runtime()
    try:
        bundle = _discover()
        if sys.platform == 'win32' and bundle is not None:
            manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
            if manifest.get('bundled_native_runtime') is True:
                assert handle is not None, 'Private Windows runtime was not activated.'
        if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
            print(json.dumps({'status': 'NOT_RUN', 'reason': 'An actual display is required.', 'windows_nvda': 'NOT_RUN'}))
            return 2
        import wx
        return run_probe(wx, native_active=handle is not None)
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())

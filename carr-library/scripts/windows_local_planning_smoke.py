"""Installed native assistant proposal/review smoke using synthetic loopback JSON.

This exercises real wx controls, HTTP Gateway, typed service validation and
durable execution. It does not qualify an actual model, manual keyboard use or NVDA.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import ProfileStore, ProviderProfile
    from carr_library.ui import assistant_dialog as ui
    from carr_library.ui.desktop_app import DesktopFrame

    errors, checks = [], []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return False
    gui = ProbeApp(False)
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name); print('PASS:', name, flush=True)
    def until(predicate, timeout=15):
        end = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= end:
                raise TimeoutError('Native local proposal did not finish.')
            gui.Yield(); sleep(.01)
    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)

    state = {'requests': [], 'hold': None}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state['requests'].append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            hold = state['hold']
            if hold is not None:
                hold.wait(5)
            proposal = {'summary': 'Synthetic protocol response, no actual LLM', 'steps': [
                {'tool': 'library.create', 'arguments': {'name': 'Explicitly reviewed library'}}]}
            raw = json.dumps({'choices': [{'message': {'content': json.dumps(proposal)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    dialog = frame = None
    try:
        source = Path(ui.__file__).resolve()
        check('assistant UI imports from installed package',
              'site-packages' in {part.casefold() for part in source.parts})
        check('real Windows widgets', wx.Platform == '__WXMSW__')
        with TemporaryDirectory(prefix='resonance-native-local-planning-') as temporary:
            app = LibraryApp(Path(temporary) / 'library')
            profile = ProviderProfile('synthetic_local', 'Synthetic protocol fixture', 'openai-compatible',
                f'http://127.0.0.1:{server.server_port}/v1', 'synthetic-protocol-model', local=True, enabled=True)
            ProfileStore(app.root / 'state' / 'providers').save(profile)
            frame = DesktopFrame(app); frame.Show(); gui.Yield()
            dialog = ui.create_assistant_dialog(frame); dialog.Show(); gui.Yield()
            controls, handlers = dialog.assistant_controls, dialog.assistant_handlers
            buttons = controls['buttons']
            check('named native planner selector defaults to guided commands',
                  isinstance(controls['planning_source'], wx.Choice)
                  and controls['planning_source'].GetName() == 'Planning source'
                  and controls['planning_source'].GetSelection() == 0 and not state['requests'])
            controls['planning_source'].SetSelection(1)
            controls['instruction'].SetValue('Create one named research library.')
            click(buttons['preview'])
            check('request is pending with run disabled', not buttons['run'].IsEnabled())
            until(buttons['run'].IsEnabled)
            check('proposal remains unexecuted until separate review action',
                  app.workspaces() == () and dialog.assistant_service.jobs() == ())
            check('exact selected model reached the loopback gateway',
                  len(state['requests']) == 1 and state['requests'][0]['model'] == profile.model)
            check('review receives native keyboard focus', wx.Window.FindFocus() == controls['review'])

            state['hold'] = Event()
            click(buttons['preview']); until(lambda: len(state['requests']) == 2)
            controls['instruction'].SetValue('Edited goal while local response is pending.')
            state['hold'].set(); until(buttons['preview'].IsEnabled)
            check('edited instructions invalidate pending native proposal',
                  not buttons['run'].IsEnabled() and 'discarded' in controls['review'].GetValue()
                  and app.workspaces() == () and dialog.assistant_service.jobs() == ())

            state['hold'] = None
            controls['instruction'].SetValue('Create one named research library.')
            click(buttons['preview']); until(buttons['run'].IsEnabled)
            click(buttons['run'])
            until(lambda: 'complete: 1 of 1' in controls['review'].GetValue())
            check('separate run uses existing durable assistant service',
                  [row.name for row in app.workspaces()] == ['Explicitly reviewed library']
                  and len(dialog.assistant_service.jobs()) == 1 and len(state['requests']) == 3)
            check('completion returns focus to readable result', wx.Window.FindFocus() == controls['review'])
            handlers['close'](); dialog.Destroy(); dialog = None
            frame.Close(); frame = None; gui.Yield()
        if errors:
            raise RuntimeError('\n'.join(errors))
    finally:
        if state['hold'] is not None:
            state['hold'].set()
        if dialog is not None:
            dialog.assistant_handlers['close'](); dialog.Destroy()
        if frame is not None:
            frame.Close()
        server.shutdown(); server.server_close(); thread.join(2)
        if native_handle is not None:
            native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks,
                      'actual_llm_model': 'NOT_RUN — synthetic protocol fixture',
                      'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

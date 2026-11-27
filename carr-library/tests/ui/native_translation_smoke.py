"""Installed native reviewed translation; the provider boundary is synthetic.

The actual wx controls, shared command registry, gateway, budget ledger and
translation/reading-copy stores run. This never calls Google, supplies a real
credential, or claims translation quality or manual NVDA acceptance.
Run: python -I tests/ui/native_translation_smoke.py --native
"""
from argparse import ArgumentParser
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def run_native(source=None, timeout=120):
    def not_run(message):
        print(json.dumps({'status':'NOT_RUN', 'reason':message, 'google_api':'NOT_RUN',
                          'nvda':'NOT_RUN'}), flush=True)
        return 77
    if sys.platform not in ('win32','darwin') and not os.environ.get('DISPLAY'):
        return not_run('A native display is unavailable.')
    if os.environ.get('GDK_BACKEND') == 'wayland':
        return not_run('wx.UIActionSimulator requires a supported native key backend.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: '+str(exc))
    if source is not None:
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source/'src'))
    import carr_library.providers as providers
    import carr_library.assistant.translation as domain
    import carr_library.ui.translation as view
    import carr_library.ui.desktop_app as desktop
    from carr_library.application import LibraryApp
    from carr_library.reader.narration import NarrationStore

    checks, errors, opened, releases = [], [], [], []
    deadline = monotonic()+timeout
    phase = None
    held = {'entered':None, 'release':None}
    def abort(message):
        errors.append(message)
        for gate in releases:
            gate.set()
        # Dismiss deepest modal children before their parent. Production finally
        # blocks retain ownership of destroying the native dialog wrappers.
        def depth(window):
            result = 0
            while window.GetParent() is not None:
                window = window.GetParent(); result += 1
            return result
        modals = [window for window in wx.GetTopLevelWindows()
                  if isinstance(window, wx.Dialog) and window.IsModal() and window.IsShown()]
        if modals:
            sorted(modals, key=depth, reverse=True)[0].EndModal(wx.ID_CANCEL)
            wx.CallAfter(lambda: abort_children())
    def abort_children():
        modals = [window for window in wx.GetTopLevelWindows()
                  if isinstance(window, wx.Dialog) and window.IsModal() and window.IsShown()]
        if modals:
            modals[-1].EndModal(wx.ID_CANCEL)
            wx.CallAfter(abort_children)
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False
    try:
        application = App(False)
    except (Exception,SystemExit) as exc:
        return not_run('The wx application could not start: '+str(exc))
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    simulator = wx.UIActionSimulator()
    watchdog = wx.CallLater(int(timeout*1000), abort, 'Native translation deadline exceeded.')
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name); print('PASS:', name, flush=True)
    def healthy():
        if errors:
            raise AssertionError('; '.join(errors))
        if monotonic() >= deadline:
            raise TimeoutError('Native translation deadline exceeded.')
    def pump(predicate, seconds=10):
        end = min(deadline, monotonic()+seconds)
        while monotonic() < end:
            application.Yield(); healthy()
            if predicate():
                return True
            wx.MilliSleep(10)
        healthy()
        return False
    def press(key, modifiers=wx.MOD_NONE):
        if not simulator.Char(key, modifiers):
            raise RuntimeError('Native key injection is unavailable.')
        end = min(deadline, monotonic()+.15)
        while monotonic() < end:
            application.Yield(); healthy(); wx.MilliSleep(10)
    def focus(control):
        control.SetFocus()
        check('native focus reaches '+control.GetName(),
              pump(lambda:wx.Window.FindFocus() is control, 3))
    def activate(control):
        check('native button is enabled: '+control.GetName(), control.IsEnabled())
        focus(control); press(wx.WXK_SPACE)
    def answer_choice(center, title):
        expires = min(deadline, monotonic()+6)
        def answer():
            try:
                if errors:
                    return
                candidates = [window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.SingleChoiceDialog) and window.GetParent() is center.dialog
                    and window.GetTitle() == title and window.IsShown() and window.IsModal()]
                if len(candidates) == 1:
                    candidates[0].SetSelection(0)
                    if not simulator.Char(wx.WXK_RETURN):
                        raise RuntimeError('Cannot accept the actual native choice.')
                    return
                if len(candidates)>1 or monotonic() >= expires:
                    raise AssertionError('The exact owned native choice did not open: '+title)
                wx.CallLater(40, answer)
            except BaseException:
                abort(traceback.format_exc())
        wx.CallLater(40, answer)

    class Vault:
        def __init__(self): self.values = {}
        def put(self, ref, binding, secret): self.values[ref,binding] = secret
        def get(self, ref, binding): return self.values[ref,binding]
        def delete(self, ref, binding): self.values.pop((ref,binding),None)
    class Transport:
        def __init__(self): self.calls = []
        def get(self, *args, **kwargs):
            self.calls.append('GET')
            return b'{"data":{"languages":[{"language":"en","name":"English"},{"language":"uk","name":"Ukrainian"}]}}'
        def post(self, profile, route, body, headers, **kwargs):
            self.calls.append(json.loads(body))
            return json.dumps({'data':{'translations':[{'translatedText':'Synthetic derivative'}]}}).encode()

    old_show, old_factory, old_review = view.show_translation, view.TranslationService, domain.TranslationService.review
    origins = {module.__name__:str(Path(module.__file__).resolve())
               for module in (providers,domain,view,desktop)}
    checkout = Path(__file__).resolve().parents[2]
    frame = None
    temporary = TemporaryDirectory(prefix='carr-native-translation-', ignore_cleanup_errors=True)
    import socket
    socket_originals = (socket.getaddrinfo, socket.create_connection, socket.socket.connect, socket.socket.connect_ex)
    def offline(*args, **kwargs):
        raise OSError('Network access is disabled during this synthetic native probe.')
    socket.getaddrinfo = socket.create_connection = socket.socket.connect = socket.socket.connect_ex = offline
    try:
        root = Path(temporary.name)
        library = LibraryApp(root/'library')
        workspace = library.create_workspace('Synthetic translation library')
        imported = library.add_text(workspace,'Synthetic immutable source','Exact source remains unchanged.')
        original = library.assets.resolve(imported['asset']).read_bytes()
        vault, transport = Vault(), Transport()
        profile = providers.ProfileStore(library.root/'api-connections.json',vault).save(
            providers.ProviderProfile('google','Synthetic transport only','google-translate',
                providers.GOOGLE_TRANSLATE_ENDPOINT,'nmt',('translation','translation_languages')),
            secret='synthetic-probe-key-never-used-on-network')
        service = domain.TranslationService(library,vault=vault,transport=transport)
        service.refresh_languages(profile.id,approved_profile_fingerprint=profile.fingerprint)
        transport.calls.clear()
        view.TranslationService = lambda app:domain.TranslationService(app,vault=vault,transport=transport)
        def held_review(self,*args,**kwargs):
            result = old_review(self,*args,**kwargs)
            if held['entered'] is not None:
                held['entered'].set()
                if not held['release'].wait(12):
                    raise TimeoutError('Held real local review was not released.')
            return result
        domain.TranslationService.review = held_review
        def observed(parent,**kwargs):
            def ready(center):
                opened.append(center)
                try:
                    phase(center)
                except BaseException:
                    abort(traceback.format_exc())
                finally:
                    for gate in releases: gate.set()
                    if center.window_alive and center.dialog.IsShown():
                        center.close()
            return old_show(parent,on_ready=ready,**kwargs)
        view.show_translation = observed
        def fill(center):
            center.source.SetValue('Exact Unicode input — Україна.')
            center.title.SetValue('Explicit native derivative')
            center.budget.SetValue('0.000100')
            center.per_call.SetValue('0.000010')
            center.to_language.SetSelection(1)
        def success(center):
            check('native translation has named source and read-only request review',
                center.dialog.IsModal() and center.source.GetName().startswith('Exact text')
                and not center.review.IsEditable() and not center.status.IsEditable())
            check('opening a saved profile sends no request',transport.calls == [])
            fill(center)
            focus(center.source); press(wx.WXK_TAB)
            check('native Tab reaches the next control',wx.Window.FindFocus() is not center.source)
            activate(center.buttons['prepare'])
            check('real worker prepares exact text without transmission',
                pump(lambda:not center.actions.busy) and center.actions.review is not None
                and 'Exact Unicode input' in center.review.GetValue() and transport.calls == [])
            answer_choice(center,'Approve this exact translation')
            activate(center.buttons['run'])
            check('explicit native approval completes the actual gateway and checkpoint',
                pump(lambda:not center.actions.busy) and center.result_state == 'complete'
                and len(transport.calls)==1 and transport.calls[0]['q']=='Exact Unicode input — Україна.')
            activate(center.buttons['save'])
            check('native Save publishes an owned reading derivative',
                pump(lambda:not center.actions.busy) and
                center.actions.service.details(center.actions.job_id)['reading_project'] is not None)
            record = center.actions.service.details(center.actions.job_id)
            derived,_ = NarrationStore(library.root).load(record['reading_project']['id'])
            check('saved derivative retains original text separately',
                derived.original_text=='Exact Unicode input — Україна.'
                and derived.current_text=='Synthetic derivative')
            answer_choice(center,'Saved translations')
            activate(center.buttons['jobs'])
            check('reopening saved output does not rerun or retain approval',
                pump(lambda:not center.actions.busy and center.budget.GetValue()=='')
                and center.actions.review is None and len(transport.calls)==1)
            activate(center.buttons['close'])
            check('native Close hides the completed dialog',not center.dialog.IsShown())
        def cancellation(center):
            fill(center)
            entered, release = Event(), Event()
            held.update(entered=entered,release=release); releases.append(release)
            before = len(transport.calls)
            activate(center.buttons['prepare'])
            check('actual local review is held before publication',pump(entered.is_set))
            activate(center.buttons['cancel']); release.set()
            check('native Cancel discards the late review',
                pump(lambda:not center.actions.busy) and center.actions.review is None
                and not center.buttons['run'].IsEnabled() and len(transport.calls)==before)
            held.update(entered=None,release=None)
            activate(center.buttons['close'])
        def forced_destroy(center):
            fill(center)
            entered, release = Event(), Event()
            held.update(entered=entered,release=release); releases.append(release)
            activate(center.buttons['prepare'])
            check('actual worker is active before forced native destruction',pump(entered.is_set))
            center.dialog.Destroy()
            check('destroy event withdraws the native lifetime',pump(lambda:not center.window_alive))
            release.set()
            check('owned worker settles without a dead-window result',
                pump(lambda:not center.actions.busy) and center.actions.review is None)
            held.update(entered=None,release=None)
        if source is None:
            check('translation modules come from installed package',
                  all(not Path(value).is_relative_to(checkout) for value in origins.values()))
        frame = desktop.DesktopFrame(library)
        frame.Show(); frame.Raise()
        if source is not None and 'translation.open' not in frame.keyboard_commands.bindings:
            view.install_translation_actions(frame)
        check('Main exposes translation through its single shared registry',
              'translation.open' in frame.keyboard_commands.bindings)
        chosen = None
        for digit in ('8','7','6'):
            overrides = {**frame.keyboard_commands.overrides,'translation.open':'Ctrl+Alt+Shift+'+digit}
            try:
                frame.keyboard_commands.validate(overrides)
            except ValueError:
                continue
            frame.keyboard_commands.apply_overrides(overrides)
            chosen = ord(digit)
            break
        check('the temporary native shortcut has no registry conflict', chosen is not None)
        for phase in (success,cancellation,forced_destroy):
            before = len(opened)
            frame.Raise(); focus(frame.choice)
            press(chosen,wx.MOD_CONTROL|wx.MOD_ALT|wx.MOD_SHIFT)
            check('real native shared shortcut opens one translation dialog',
                  pump(lambda:len(opened)==before+1))
        check('native translation workflow keeps retained source bytes exact',
              library.assets.resolve(imported['asset']).read_bytes()==original)
        healthy()
        print(json.dumps({'status':'PASS','checks':len(checks),'wx':wx.version(),
            'platform':platform.system(),'origins':origins,
            'provider_transport':'synthetic; actual gateway and stores',
            'google_api':'NOT_RUN','translation_quality':'NOT_RUN','nvda':'NOT_RUN',
            'source_override':str(source) if source else None},sort_keys=True),flush=True)
        return 0
    except BaseException as exc:
        print(json.dumps({'status':'FAIL','checks_passed':len(checks),'error':str(exc),
                          'google_api':'NOT_RUN','nvda':'NOT_RUN'}),flush=True)
        traceback.print_exc()
        return 1
    finally:
        view.show_translation,view.TranslationService = old_show,old_factory
        domain.TranslationService.review = old_review
        watchdog.Stop()
        for gate in releases: gate.set()
        if frame is not None:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=False,cancel_futures=True)
            frame.Destroy()
            application.Yield()
        socket.getaddrinfo, socket.create_connection, socket.socket.connect, socket.socket.connect_ex = socket_originals
        temporary.cleanup()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native',action='store_true',required=True)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--timeout',type=float,default=120)
    args = parser.parse_args()
    if not 10<=args.timeout<=240:
        parser.error('--timeout must be between 10 and 240 seconds')
    return run_native(args.source.resolve() if args.source else None,args.timeout)


if __name__=='__main__':
    raise SystemExit(main())

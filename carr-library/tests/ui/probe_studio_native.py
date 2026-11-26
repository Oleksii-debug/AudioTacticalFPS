"""Actual native Audio Studio save/reopen, labels, layout and keyboard probe.

Uses synthetic voice metadata without synthesizing or playing sound. A successful
run proves native controls and durable project editing, not listening or NVDA.
Run the installed package with ``python -I <this-file> --require-windows``.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import traceback


def run_probe(wx, *, native_active):
    from carr_library.application import LibraryApp
    from carr_library.studio import ProjectStore
    from carr_library.ui import studio_actions, provider_dialog

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

                def reopened_checks():
                    check('actual saved project reopened in JSON mode', reopened.mode.GetSelection() == 2)
                    check('reopened text preserves exact Unicode', reopened.project().scenes[0].lines[0].text == text)
                    check('reopened form is clean', reopened._form_state() == reopened.saved_form)
                    check('reopened editor receives focus', wx.Window.FindFocus() is reopened.editor)
                    reopened.close()

                later(reopened, reopened_checks, 100)
                reopened.show()
            check('library source bytes are unchanged', originals == {
                p.name: sha256(p.read_bytes()).hexdigest() for p in library.assets_dir.iterdir()})
        finally:
            for timer in timers:
                timer.Stop()
            frame.Destroy()
            gui.ProcessPendingEvents()

    report = dict(status='FAIL' if failures else 'PASS', platform=platform.system(), python=platform.python_version(),
                  wx_version=wx.version(), native_runtime_active=native_active, checks=checks, errors=failures,
                  synthesis='NOT_RUN', listening='NOT_RUN', windows_nvda='NOT_RUN',
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

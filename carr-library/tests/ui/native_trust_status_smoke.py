"""Real Windows wx controls; synthetic authority, no NVDA or release claim."""
from hashlib import sha256
import argparse
import base64
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true', required=True)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows required', 'nvda': 'NOT_RUN'}))
        return 77
    if args.source is not None:
        sys.path[:0] = [str(args.source), str(args.source / 'src')]
    if args.source is None and (not sys.flags.isolated or not sys.dont_write_bytecode):
        raise RuntimeError('Use the installed interpreter with -I -B.')
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _run(args)
    finally:
        if runtime is not None:
            runtime.close()


def _run(args):
    import wx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from carr_library.application import LibraryApp
    from carr_library.delivery_contract import IntegrationContext
    from carr_library.security._files import json_bytes
    from carr_library.security.anchors import Anchor
    from carr_library.security.controller import TrustController
    from carr_library.security.entitlements import CONTEXT, EntitlementStore, PublicKey, TrustPolicy
    from carr_library.ui.desktop_app import DesktopFrame
    import carr_library.ui.trust_status as ui
    if args.source is None:
        assert not Path(ui.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    import runpy
    import traceback
    from carr_library.security import anchors, entitlements, controller as controller_module, updates
    verify_sources = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts' / 'windows_local_planning_smoke.py'))['verify_installed_sources']
    bindings = (verify_sources(ui, anchors, entitlements, controller_module, updates)
                if args.source is None else {})
    errors = []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            return False
    app = ProbeApp(False)
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)
    class SyntheticAnchor:
        def __init__(self):
            self.value = Anchor()
        def load(self):
            return self.value
        def compare_and_set(self, expected, replacement, *, guard=lambda: None):
            assert self.value == expected
            guard()
            self.value = replacement
    def exercise():
        try:
            with TemporaryDirectory(prefix='carr-trusted-native-') as temporary:
                root = Path(temporary)
                frame = DesktopFrame(LibraryApp(root / 'library'))
                frame.Show()
                app.Yield()
                try:
                    bar = frame.GetMenuBar()
                    help_menu = next(bar.GetMenu(index) for index in range(bar.GetMenuCount())
                                     if bar.GetMenuLabelText(index) == 'Help')
                    item_id = help_menu.FindItem('License and signed updates')
                    check('Actual desktop Help exposes the signed client status', item_id != wx.NOT_FOUND)
                    check('Signed status uses the existing configurable keyboard registry',
                          any(command.action == 'help/license and signed updates'
                              for command in frame.keyboard_settings.commands))
                    previous_count = help_menu.GetMenuItemCount()
                    ui.install_trust_actions(frame)
                    check('Repeated module installation does not duplicate the Help command',
                          help_menu.GetMenuItemCount() == previous_count)
                    calls = []
                    original = ui.show_trust_status
                    ui.show_trust_status = lambda host: calls.append(host)
                    try:
                        event = wx.CommandEvent(wx.EVT_MENU.typeId, item_id)
                        frame.GetEventHandler().ProcessEvent(event)
                    finally:
                        ui.show_trust_status = original
                    check('Native Help event reaches the status action', calls == [frame])
                    default = ui.TrustStatusDialog(frame, TrustController())
                    default.Show()
                    app.Yield()
                    check('Default status is a named native read-only multiline control',
                          isinstance(default.text, wx.TextCtrl)
                          and default.text.GetName() == 'License and update status'
                          and bool(default.text.GetWindowStyleFlag() & wx.TE_READONLY)
                          and bool(default.text.GetWindowStyleFlag() & wx.TE_MULTILINE))
                    check('Private default shows perpetual operation and no fake authority actions',
                          'Private perpetual' in default.text.GetValue() and not default.buttons)
                    default.Destroy()
                    for termination in ('direct', 'native_parent'):
                        owner = wx.Frame(frame, title='Synthetic signed-status owner')
                        detached = ui.TrustStatusDialog(owner, TrustController())
                        native_id, events, delivered = detached.GetId(), [], []
                        def observed(event):
                            if event.GetId() == native_id:
                                events.append(event.GetId())
                            event.Skip()
                        detached.Bind(wx.EVT_WINDOW_DESTROY, observed)
                        owner.Show()
                        detached.Show()
                        if termination == 'direct':
                            check('Direct status destruction immediately detaches callbacks', detached.Destroy() and not detached._alive)
                        else:
                            wx.Window.DestroyChildren(owner)
                        deadline = time.monotonic() + 5
                        while detached and time.monotonic() < deadline:
                            app.Yield()
                            wx.MilliSleep(10)
                        check('Native status owner deletion observed once: ' + termination,
                              not detached and events == [native_id] and not detached._alive)
                        def after_deletion():
                            for action in (detached.on_status, detached.on_import, detached.on_refresh, detached.on_update):
                                action()
                            delivered.append(True)
                        wx.CallAfter(after_deletion)
                        deadline = time.monotonic() + 5
                        while not delivered and time.monotonic() < deadline:
                            app.Yield()
                            wx.MilliSleep(10)
                        check('Queued status actions ignore the deleted owner: ' + termination,
                              delivered == [True] and not errors and detached.Destroy() is False)
                        if owner:
                            owner.Destroy()
                    key = Ed25519PrivateKey.generate()
                    terms = 'Synthetic owner terms. No production account, billing or publisher is represented.'
                    terms_digest = sha256(terms.encode()).hexdigest()
                    now = int(time.time())
                    payload = json_bytes(dict(schema=1, product='synthetic', entitlement_id='native-fixture',
                        subject='fixture', device='fixture-device', sequence=1, profile='perpetual',
                        issued_at=now, not_before=now, expires_at=None, offline_until=None,
                        refresh_interval=None, policy_version='fixture-policy', terms_sha256=terms_digest,
                        features=['render'], revoked=False, request_nonce=None))
                    raw = json_bytes(dict(version=1, kind='carr-entitlement',
                        key_id=sha256(key.public_key().public_bytes_raw()).hexdigest(),
                        payload=base64.b64encode(payload).decode(),
                        signature=base64.b64encode(key.sign(CONTEXT + payload)).decode()))
                    path = root / 'permission.json'
                    path.write_bytes(raw)
                    policy = TrustPolicy('synthetic', 'fixture', 'fixture-device',
                        (PublicKey(key.public_key().public_bytes_raw()),), frozenset({'fixture-policy'}), frozenset({'render'}))
                    store = EntitlementStore(root / 'client', policy, SyntheticAnchor())
                    controller = TrustController(entitlement=store, terms={terms_digest: terms},
                        context=IntegrationContext('synthetic', 'fixture-build', 'stable'))
                    review = controller.preview_file(path)
                    dialog = ui.GrantReviewDialog(frame, review)
                    dialog.Show()
                    app.Yield()
                    check('Terms and profile are readable before accepting',
                          terms in dialog.text.GetValue() and 'Profile: perpetual' in dialog.text.GetValue())
                    check('Import requires explicit native checkbox acceptance', not dialog.import_button.IsEnabled())
                    dialog.accepted.SetValue(True)
                    event = wx.CommandEvent(wx.EVT_CHECKBOX.typeId, dialog.accepted.GetId())
                    event.SetEventObject(dialog.accepted)
                    dialog.accepted.GetEventHandler().ProcessEvent(event)
                    check('Native acceptance event enables import', dialog.import_button.IsEnabled())
                    controller.import_review(review, accepted=dialog.accepted.GetValue())
                    check('Reviewed signed grant is durably verified', store.current().sequence == 1)
                    dialog.Destroy()
                finally:
                    frame.Close()
                    app.Yield()
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            app.ExitMainLoop()
    wx.CallAfter(exercise)
    app.MainLoop()
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'nvda': 'NOT_RUN',
                      'production_authority': 'NOT_CONFIGURED', 'production_installer_upgrade': 'NOT_RUN',
                      'ui_module': str(Path(ui.__file__).resolve()), 'installed_sources': bindings}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

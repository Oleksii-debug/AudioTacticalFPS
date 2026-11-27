"""Real Windows wx controls; synthetic authority, no NVDA or release claim."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import argparse
import base64
import faulthandler
import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import time
import traceback


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
    import wx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from carr_library.application import LibraryApp
    from carr_library.delivery_contract import IntegrationContext
    from carr_library.security._files import json_bytes
    from carr_library.security.anchors import Anchor
    from carr_library.security.controller import TrustController
    from carr_library.security.entitlements import CONTEXT, EntitlementStore, PublicKey, TrustPolicy
    from carr_library.security.updates import LocalRepositoryClient, UpdatePolicy
    from carr_library.storage.revisions import SCHEMAS
    import carr_library.security.polling as polling
    from carr_library.ui.desktop_app import DesktopFrame
    import carr_library.ui.trust_status as ui
    if args.source is None:
        assert not Path(ui.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
        assert not Path(polling.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2])
    # Keep native callback failures in the CI console, including failures raised
    # outside this generator by normal Close/Destroy event dispatch.
    faulthandler.enable(file=sys.stderr, all_threads=True)
    app = wx.App(False)
    app.SetExitOnFrameDelete(False)
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
    def signed_repository(root):
        # The same synthetic product/context as the permission fixture. Real
        # TUF signatures are generated locally; this is no publisher authority.
        from securesystemslib.signer import CryptoSigner
        from tuf.api.metadata import Metadata, MetaFile, Root, Snapshot, TargetFile, Targets, Timestamp
        repository = root / 'signed-repository'
        (repository / 'metadata').mkdir(parents=True)
        (repository / 'targets').mkdir()
        signer = CryptoSigner.generate_ed25519()
        expires = datetime.now(timezone.utc) + timedelta(days=1)
        metadata = {'root': Metadata(Root(expires=expires, consistent_snapshot=True)),
                    'targets': Metadata(Targets(expires=expires)),
                    'snapshot': Metadata(Snapshot(expires=expires)),
                    'timestamp': Metadata(Timestamp(expires=expires))}
        for role in metadata:
            metadata['root'].signed.add_key(signer.public_key, role)
        def encode(role):
            metadata[role].sign(signer)
            return metadata[role].to_bytes()
        bootstrap = encode('root')
        (repository / 'metadata' / '1.root.json').write_bytes(bootstrap)
        target = TargetFile.from_data('application.zip', b'Synthetic payload; metadata checks never download this.')
        target.unrecognized_fields['custom'] = dict(schema=1, kind='carr-application', product='synthetic',
            channel='stable', version='0.2.0', build=2, source_commit='b' * 40,
            data_schema=max(SCHEMAS), notes='Synthetic native polling fixture; not an application release.')
        metadata['targets'].signed.targets = {target.path: target}
        targets = encode('targets')
        (repository / 'metadata' / '1.targets.json').write_bytes(targets)
        metadata['snapshot'].signed.meta = {'targets.json': MetaFile.from_data(1, targets, ['sha256'])}
        snapshot = encode('snapshot')
        (repository / 'metadata' / '1.snapshot.json').write_bytes(snapshot)
        metadata['timestamp'].signed.snapshot_meta = MetaFile.from_data(1, snapshot, ['sha256'])
        (repository / 'metadata' / 'timestamp.json').write_bytes(encode('timestamp'))
        (repository / 'targets' / (target.hashes['sha256'] + '.' + target.path)).write_bytes(
            b'Synthetic payload; metadata checks never download this.')
        client = LocalRepositoryClient(root / 'update-cache', UpdatePolicy(
            product='synthetic', channel='stable', bootstrap=bootstrap,
            bootstrap_sha256=sha256(bootstrap).hexdigest(), current_build=1,
            max_package_bytes=1024**2, max_unpacked_bytes=2 * 1024**2, max_files=100))
        return repository, target.path, client

    def scenario():
        with TemporaryDirectory(prefix='carr-trusted-native-') as temporary:
            root = Path(temporary)
            frame = DesktopFrame(LibraryApp(root / 'library'))
            frame.Show()
            yield None
            try:
                bar = frame.GetMenuBar()
                help_menu = next(bar.GetMenu(index) for index in range(bar.GetMenuCount())
                                 if bar.GetMenuLabelText(index) == 'Help')
                item_id = help_menu.FindItem('License and signed updates')
                check('Actual desktop Help exposes the signed client status', item_id != wx.NOT_FOUND)
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
                yield None
                check('Default status is a named native read-only multiline control',
                      isinstance(default.text, wx.TextCtrl)
                      and default.text.GetName() == 'License and update status'
                      and bool(default.text.GetWindowStyleFlag() & wx.TE_READONLY)
                      and bool(default.text.GetWindowStyleFlag() & wx.TE_MULTILINE))
                check('Private default shows perpetual operation and no fake authority actions',
                      'Private perpetual' in default.text.GetValue() and not default.buttons)
                default.Destroy()
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
                context = IntegrationContext('synthetic', 'fixture-build', 'stable')
                controller = TrustController(entitlement=store, terms={terms_digest: terms}, context=context)
                review = controller.preview_file(path)
                dialog = ui.GrantReviewDialog(frame, review)
                dialog.Show()
                yield None
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

                repository, target, client = signed_repository(root)
                calls, original_check = [], client.check
                def checked(*args, **kwargs):
                    calls.append(threading.get_ident())
                    return original_check(*args, **kwargs)
                client.check = checked
                poll_controller = TrustController(entitlement=store, terms={terms_digest: terms}, context=context,
                    updates=client, target=target, update_repository=repository, automatic_updates=True)
                class BusyDesktopFrame(DesktopFrame):
                    def _menus(self):
                        # Simulate an existing owning operation before the real
                        # native installation sequence starts its wx.Timer.
                        self.working = True
                        super()._menus()
                poll_frame = BusyDesktopFrame(LibraryApp(root / 'poll-library'), trust_controller=poll_controller)
                poll_frame.Show()
                host, timer = poll_frame.trust_update_poll_host, poll_frame.trust_client_timer
                try:
                    check('Configured polling uses an actual running wx.Timer', isinstance(timer, wx.Timer) and timer.IsRunning())
                    start = time.monotonic()
                    yield (lambda: time.monotonic() - start >= 1.2, 4,
                           'Native busy hold did not finish.')
                    check('Native idle timer holds metadata checks while the frame is working',
                          not calls and host.status.phase == 'waiting' and not list(client.metadata.iterdir()))
                    poll_frame.working = False
                    yield (lambda: host.status.phase == 'available', 8,
                           lambda: 'Native polling did not settle: phase=' + host.status.phase
                           + ', calls=' + str(len(calls)) + ', worker_alive=' + str(host._thread.is_alive()))
                    check('Native idle transition verifies signed metadata on the background thread',
                          host.status.release.build == 2 and calls == [host._thread.ident]
                          and calls[0] != threading.get_ident())
                    check('Automatic native checks leave payloads and existing signed permission untouched',
                          not list(client.downloads.iterdir()) and store.current().sequence == 1)
                    configured_status = ui.TrustStatusDialog(poll_frame, poll_controller,
                                                            status_text=poll_controller.status_text())
                    configured_status.Show()
                    yield None
                    check('Configured update status retains the explicit native repository review action',
                          'Automatic checks do not download' in configured_status.text.GetValue()
                          and any(button.GetName() == 'Check offline update repository'
                                  for button in configured_status.buttons))
                    configured_status.Destroy()
                finally:
                    poll_frame.working = False
                    print('TRACE: Requesting native polling owner Close.', flush=True)
                    poll_frame.Close()
                    print('TRACE: Native polling owner Close returned; phase=' + host.status.phase, flush=True)
                    yield (lambda: host.status.phase == 'stopped', 8,
                           lambda: 'Native owner destruction did not stop polling: phase=' + host.status.phase)
                check('Native owner destruction stops the metadata worker', not host._thread.is_alive())
                try:
                    timer_stopped = not timer.IsRunning()
                except RuntimeError:
                    timer_stopped = not timer
                check('Native owner destruction stops or destroys the actual timer', timer_stopped)
            finally:
                frame.Close()
                yield None
    # Run every native assertion from the real application event loop. Yielding
    # a predicate schedules a bounded check; it never dispatches product timer
    # callbacks, host idle changes or Destroy handlers directly.
    steps = scenario()
    # wxPython 4.2.4 wxPyApp::MainLoop returns without dispatching CallAfter
    # when wxTopLevelWindows is empty, even with ExitOnFrameDelete(False).
    # Prime only the actual DesktopFrame creation/show before entering the loop;
    # the first yield precedes every scenario assertion and product timer action.
    initial = next(steps)
    assert initial is None and wx.GetTopLevelWindows(), 'Create the native owner before MainLoop.'
    state = {'finished': False, 'error': None}
    def complete(error=None):
        if state['finished']:
            return
        if error is not None and state['error'] is None:
            state['error'] = error
        if state['error'] is not None:
            print(json.dumps({'status': 'FAIL', 'stage': 'native event loop',
                              'reason': repr(state['error'])}), flush=True)
            traceback.print_exception(state['error'], file=sys.stderr)
        state['finished'] = True
        app.ExitMainLoop()

    def advance(error=None):
        if state['finished']:
            return
        if error is not None and state['error'] is None:
            state['error'] = error
        try:
            wait = steps.throw(error) if error is not None else next(steps)
        except StopIteration:
            complete()
            return
        except BaseException as failure:
            complete(failure)
            return
        if wait is None:
            wx.CallLater(10, advance)
            return
        predicate, maximum, description = wait
        deadline = time.monotonic() + maximum
        def poll():
            if state['finished']:
                return
            try:
                ready = predicate()
            except BaseException as failure:
                advance(failure)
                return
            if ready:
                wx.CallAfter(advance)
            elif time.monotonic() >= deadline:
                message = description() if callable(description) else description
                advance(AssertionError(message))
            else:
                wx.CallLater(10, poll)
        wx.CallAfter(poll)

    def expired():
        complete(AssertionError('Native application event loop exceeded its total 45-second budget.'))

    original_hook = sys.excepthook
    def native_uncaught(kind, error, trace):
        # SIP invokes sys.excepthook for exceptions in native event callbacks.
        # Record them as failures instead of losing them after the frame closes.
        print(json.dumps({'status': 'FAIL', 'stage': 'native callback',
                          'reason': repr(error)}), flush=True)
        traceback.print_exception(kind, error, trace, file=sys.stderr)
        complete(error)
    sys.excepthook = native_uncaught
    watchdog = wx.CallLater(45000, expired)
    wx.CallAfter(advance)
    try:
        app.MainLoop()
    finally:
        sys.excepthook = original_hook
        print(json.dumps({'stage': 'MainLoop returned', 'finished': state['finished'],
                          'error': repr(state['error']) if state['error'] is not None else None}), flush=True)
        watchdog.Stop()
    if not state['finished']:
        raise AssertionError('Native application event loop exited before the scenario finished.')
    if state['error'] is not None:
        print(json.dumps({'status': 'FAIL', 'checks': checks,
                          'reason': str(state['error']), 'nvda': 'NOT_RUN'}), flush=True)
        raise state['error']
    print(json.dumps({'status': 'PASS', 'checks': checks, 'nvda': 'NOT_RUN',
                      'production_authority': 'NOT_CONFIGURED', 'production_installer_upgrade': 'NOT_RUN',
                      'ui_module': str(Path(ui.__file__).resolve()),
                      'polling_module': str(Path(polling.__file__).resolve())}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Installed native host-lifecycle probe with synthetic providers; no crypto/NVDA claim."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    import wx
    from dataclasses import replace
    from carr_library.application import LibraryApp
    from carr_library.delivery_contract import (
        Capability, CONTRACT_VERSION, DecisionReason, DecisionState,
        EntitlementDecision, IntegrationContext, ProviderCapabilities, UpdateOffer,
    )
    from carr_library.delivery_service import (
        DeliveryError, DeliveryService, LicenseSnapshot, LicenseState, PreparedUpdate,
    )
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import delivery_dialog as ui

    context = IntegrationContext('synthetic-product', 'fixture-1', 'test')
    checks, errors = [], []

    class Provider:
        def __init__(self):
            self.value = LicenseSnapshot(context, LicenseState.VALID, 1, 0, 86400)
            self.prepared = None
            self.calls = []
            self.wait_for_cancel = False

        def describe(self):
            return ProviderCapabilities(CONTRACT_VERSION, frozenset(Capability))

        def snapshot(self, context, *, cancelled):
            return self.value

        def decide(self, request):
            return EntitlementDecision(request, DecisionState.DENIED, DecisionReason.PROVIDER_DENIED)

        def import_license(self, context, source, *, cancelled):
            if source.read_bytes() != b'synthetic fixture only':
                raise ValueError('synthetic refusal')
            self.value = replace(self.value, state=LicenseState.VALID, generation=2)
            return self.value

        def check(self, context, *, cancelled):
            self.calls.append('check')
            deadline = monotonic() + 10
            while self.wait_for_cancel:
                if cancelled():
                    raise InterruptedError('private message must not escape')
                if monotonic() > deadline:
                    raise TimeoutError('synthetic cancellation deadline')
                sleep(0.01)
            return UpdateOffer(context, 'synthetic-offer', 'fixture-2', 7, 'a' * 64)

        def stage(self, context, offer, *, cancelled, progress):
            if progress:
                progress(7, 7, 'Synthetic package verified')
            self.prepared = PreparedUpdate(offer, 'b' * 32)
            return self.prepared

        def recover(self, context, *, cancelled):
            return self.prepared

        def install(self, *args, **kwargs):
            raise AssertionError('An update must not install without fresh explicit approval.')

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    app = ProbeApp(False)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def pump(predicate):
        deadline = monotonic() + 15
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline:
                raise TimeoutError('Native delivery operation did not finish.')
            app.Yield()
            sleep(0.01)

    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        check('native button event handled: ' + control.GetName(),
            control.GetEventHandler().ProcessEvent(event))

    check('delivery controls load from installed wheel',
        'site-packages' in [part.casefold() for part in Path(ui.__file__).parts])
    check('native Windows controls active', wx.Platform == '__WXMSW__')
    with TemporaryDirectory(prefix='research-delivery-native-') as temp:
        root = Path(temp)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic preserved library')
        topic = library.create_topic(workspace, 'Preserved notes')
        library.add_text(workspace, 'Retained source', 'A railway source remains accessible.')
        hit, = library.search(workspace, 'railway')
        evidence = library.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this research note.')
        signed = root / 'synthetic-license.json'
        signed.write_bytes(b'synthetic fixture only')
        provider = Provider()
        service = DeliveryService(context, entitlement=provider, updater=provider)
        frame = DesktopFrame(library, delivery=service)
        try:
            app.SetTopWindow(frame)
            frame.Show()
            pump(lambda: not frame.working)
            commands = {row.command.identity for row in frame.keyboard_actions.bindings}
            check('license and update actions share the native command registry',
                'Help/License and updates' in commands and 'Help/Check for updates' in commands)

            def ready(dialog):
                try:
                    check('status has a readable native name and read-only style',
                        dialog.status.GetName() == 'License and update status'
                        and bool(dialog.status.GetWindowStyleFlag() & wx.TE_READONLY))
                    check('native Escape closes the dialog', dialog.GetEscapeId() == wx.ID_CANCEL)
                    dialog.status.SetFocus()
                    app.Yield()
                    check('native status accepts keyboard focus', wx.Window.FindFocus() is dialog.status)
                    dialog.status.Navigate()
                    app.Yield()
                    check('native Tab reaches a named action',
                        wx.Window.FindFocus() in dialog.controls.values())
                    click(dialog.controls['status'])
                    pump(lambda: not dialog.running and not frame.working)
                    check('local license status is presented', 'Verified license' in dialog.status.GetValue())
                    provider.value = replace(provider.value, state=LicenseState.EXPIRED)
                    click(dialog.controls['status'])
                    pump(lambda: not dialog.running and not frame.working)
                    check('expiry is visible without hiding user data',
                        'expired' in dialog.status.GetValue()
                        and service.require('data.export').state is DecisionState.ALLOWED)
                    dialog.choose_source = lambda title: signed
                    click(dialog.controls['import_license'])
                    pump(lambda: not dialog.running and not frame.working)
                    check('offline import updates verified local view', provider.value.generation == 2
                        and 'Verified license' in dialog.status.GetValue())
                    dialog.approve_network = lambda purpose: True
                    click(dialog.controls['check'])
                    pump(lambda: not dialog.running and not frame.working)
                    check('offer needs separate download and install',
                        dialog.controls['stage'].IsEnabled()
                        and not dialog.controls['install'].IsEnabled())
                    click(dialog.controls['stage'])
                    pump(lambda: not dialog.running and not frame.working)
                    check('prepared package permits explicit backup/install review',
                        dialog.prepared is not None and dialog.controls['install'].IsEnabled()
                        and 'SHA-256' in dialog.status.GetValue())
                    approval = service.approve_install(dialog.prepared)
                    click(dialog.controls['recover'])
                    pump(lambda: not dialog.running and not frame.working)
                    try:
                        service.install_update(approval, idle=lambda: True,
                            backup=lambda: (_ for _ in ()).throw(AssertionError('stale approval reached backup')))
                    except DeliveryError:
                        check('recovery invalidates prior install approval', True)
                    else:
                        raise AssertionError('stale install approval was accepted')
                    provider.wait_for_cancel = True
                    click(dialog.controls['check'])
                    pump(lambda: dialog.running and frame.working and provider.calls.count('check') == 2)
                    dialog.close()
                    check('closing a running request requests cancellation and retains dialog', dialog.IsModal())
                    pump(lambda: not dialog.running and not frame.working)
                    check('cancelled provider detail is redacted',
                        'private message' not in dialog.status.GetValue())
                    check('saved research remains unchanged after license/update operations',
                        library.evidence(workspace, topic)[0].evidence_id == evidence
                        and library.evidence(workspace, topic)[0].note == 'Keep this research note.')
                    dialog.EndModal(wx.ID_CANCEL)
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            watchdog = wx.CallLater(45000, lambda: errors.append('Delivery probe deadline expired.'))
            try:
                frame.delivery_actions.show(on_ready=ready)
            finally:
                watchdog.Stop()
            if errors:
                raise RuntimeError('\n'.join(errors))
        finally:
            frame.cancel_event.set()
            if not frame.IsBeingDeleted():
                frame.Close(force=True)
                app.Yield()
            if native is not None:
                native.close()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks),
        'provider': 'synthetic', 'cryptographic_acceptance': 'NOT_RUN',
        'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

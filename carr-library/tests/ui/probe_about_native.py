"""Actual installed About menu, native keyboard and upstream-notice readback.

The real installed catalogue/RECORD/notice reader and DesktopFrame worker run.
Only a disposable empty library is created. No NVDA or rights clearance is claimed.
"""
from argparse import ArgumentParser
from collections import Counter
from hashlib import sha256
from importlib import metadata
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback

EXPECTED_CHECKS = (
    'installed-about-module-origins',
    'one-native-help-about-command',
    'native-help-event-opens-real-modal',
    'named-read-only-about-and-status-controls',
    'named-focusable-component-and-action-controls',
    'real-installed-component-version-in-catalogue',
    'real-product-version-and-component-specific-terms',
    'catalogue-read-runs-on-owned-background-worker',
    'native-choice-keys-select-installed-component',
    'native-read-key-opens-notice-inventory',
    'native-picker-lists-actual-installed-notices',
    'native-picker-key-opens-upstream-text',
    'upstream-text-matches-verified-installed-notice',
    'upstream-reader-is-named-readonly-and-focused',
    'native-escape-keeps-parent-about-open',
    'native-refresh-rereads-installed-catalogue',
    'all-product-notice-reads-use-background-workers',
    'native-close-destroys-only-about',
    'parent-library-and-notice-bytes-preserved',
)

def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', required=True)
    parser.parse_args(argv)
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows required.', 'nvda': 'NOT_RUN'}))
        return 77
    import wx
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    import carr_library.component_notices as inventory
    import carr_library.ui.about_dialog as about
    import carr_library.ui.desktop_app as desktop

    # Independent expected readback uses the same admitted installed inventory,
    # before the native event loop; no product callback or parser is replaced.
    expected_catalogue = inventory.catalogue()
    selected = [item for item in expected_catalogue.components if item.name.casefold() == 'pypdf']
    if len(selected) != 1:
        raise AssertionError('The pinned installed pypdf component must be present.')
    component = selected[0]
    expected_notices = inventory.notices(component)
    if not expected_notices:
        raise AssertionError('The pinned installed pypdf wheel must retain an upstream notice.')
    expected_text = inventory.read_notice(expected_notices[0])
    expected_version = metadata.version('pypdf')
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            finish(RuntimeError(traceback.format_exc()))
            return False

    app = ProbeApp(False)
    app.SetExitOnFrameDelete(False)
    keys = wx.UIActionSimulator()
    checks, workers = [], []
    main_thread = threading.get_ident()
    windows = {'frame': None, 'about': None}
    state = {'finished': False, 'error': None, 'traceback': None}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def children(window):
        found = []
        pending = list(window.GetChildren())
        while pending:
            current = pending.pop(0)
            found.append(current)
            pending.extend(current.GetChildren())
            if len(found) > 256:
                raise AssertionError('Unexpected native control count.')
        return found

    def modal(title):
        candidates = [window for window in wx.GetTopLevelWindows()
                      if window and isinstance(window, wx.Dialog)
                      and window.IsShown() and window.GetTitle() == title]
        if len(candidates) > 1:
            raise AssertionError('Duplicate native modal.')
        return candidates[0] if candidates else None

    def press(control, key):
        assert control and control.IsShownOnScreen() and control.IsEnabled(), 'Native control unavailable.'
        control.SetFocus()
        assert wx.Window.FindFocus() is control, 'Native control did not receive focus.'
        assert keys.KeyDown(key) and keys.KeyUp(key), 'Native key dispatch failed.'

    origins = {}
    for module in (inventory, about, desktop):
        path = Path(module.__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix).resolve()), str(path)
        origins[module.__name__] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    check(EXPECTED_CHECKS[0], len(origins) == 3)

    def scenario():
        with TemporaryDirectory(prefix='carr-about-native-') as temporary:
            library = LibraryApp(Path(temporary) / 'library')
            frame = DesktopFrame(library)
            windows['frame'] = frame
            real_run_job = frame.run_job

            def observed_job(title, operation, *args, **kwargs):
                def execute():
                    workers.append((title, threading.get_ident()))
                    return operation()
                return real_run_job(title, execute, *args, **kwargs)
            # Observe the actual host worker; preserve its admission, executor,
            # progress, cancellation, result and cleanup semantics.
            frame.run_job = observed_job
            frame.Show()
            frame.Raise()
            yield None
            bar = frame.GetMenuBar()
            items = []
            for index in range(bar.GetMenuCount()):
                if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'help':
                    items.extend(item for item in bar.GetMenu(index).GetMenuItems()
                                 if 'About Resonance and component notices' in item.GetItemLabelText())
            check(EXPECTED_CHECKS[1], len(items) == 1)
            wx.PostEvent(frame, wx.CommandEvent(wx.EVT_MENU.typeId, items[0].GetId()))
            yield (lambda: modal('About Resonance and component notices') is not None, 10,
                   'Actual About modal did not open.')
            dialog = modal('About Resonance and component notices')
            windows['about'] = dialog
            check(EXPECTED_CHECKS[2], dialog.IsModal() and dialog.GetParent() is frame)
            check(EXPECTED_CHECKS[3],
                  dialog.about.GetName() == 'About Resonance, version and notice availability'
                  and dialog.details.GetName() == 'Component licence metadata and reading status'
                  and not dialog.about.IsEditable() and not dialog.details.IsEditable())
            controls = ((dialog.components, 'Installed and delivered component'),
                        (dialog.read_button, 'Read selected component upstream notices'),
                        (dialog.refresh_button, 'Refresh installed component list'),
                        (dialog.close_button, 'Close About Resonance'))
            yield (lambda: dialog.session.catalogue is not None and not dialog.session.busy
                   and not frame.working, 20, 'Actual installed catalogue did not finish.')
            check(EXPECTED_CHECKS[4], all(control.GetName() == name
                  and control.AcceptsFocusFromKeyboard() for control, name in controls))
            available = dialog.session.catalogue.components
            indexes = [index for index, item in enumerate(available)
                       if item.name.casefold() == 'pypdf' and item.version == expected_version]
            check(EXPECTED_CHECKS[5], len(indexes) == 1 and dialog.components.GetCount() == len(available))
            check(EXPECTED_CHECKS[6], 'Resonance\nSceneria Innovative Technologies' in dialog.about.GetValue()
                  and 'Application version: ' + dialog.session.catalogue.version in dialog.about.GetValue()
                  and 'specific to each component' in dialog.about.GetValue()
                  and 'entitlement status is a separate command' in dialog.about.GetValue())
            check(EXPECTED_CHECKS[7], workers and all(identity != main_thread for _, identity in workers))
            target = indexes[0]
            press(dialog.components, wx.WXK_HOME)
            for _ in range(target):
                press(dialog.components, wx.WXK_DOWN)
            yield (lambda: dialog.session.selected == target, 8, 'Native component selection did not settle.')
            check(EXPECTED_CHECKS[8], dialog.components.GetSelection() == target
                  and dialog.session.catalogue.components[target].name.casefold() == 'pypdf')
            press(dialog.read_button, wx.WXK_SPACE)
            picker_title = available[target].name + ' — notice files'
            yield (lambda: modal(picker_title) is not None, 15, 'Actual notice inventory did not open.')
            picker = modal(picker_title)
            check(EXPECTED_CHECKS[9], isinstance(picker, wx.SingleChoiceDialog) and picker.IsModal())
            lists = [child for child in children(picker) if isinstance(child, wx.ListBox)]
            check(EXPECTED_CHECKS[10], len(lists) == 1
                  and list(lists[0].GetStrings()) == [notice.title for notice in expected_notices])
            press(lists[0], wx.WXK_HOME)
            press(lists[0], wx.WXK_RETURN)
            reader_title = available[target].name + ' — upstream notice'
            yield (lambda: modal(reader_title) is not None, 15, 'Actual upstream notice did not open.')
            reader_dialog = modal(reader_title)
            readers = [child for child in children(reader_dialog) if isinstance(child, wx.TextCtrl)]
            check(EXPECTED_CHECKS[11], len(readers) == 1 and reader_dialog.IsModal())
            reader = readers[0]
            check(EXPECTED_CHECKS[12],
                  reader.GetValue().replace('\r\n', '\n') == expected_text.replace('\r\n', '\n'))
            check(EXPECTED_CHECKS[13], not reader.IsEditable()
                  and reader.GetName() == reader_title and wx.Window.FindFocus() is reader)
            press(reader, wx.WXK_ESCAPE)
            yield (lambda: modal(reader_title) is None and not dialog.session.busy
                   and not frame.working, 8, 'Upstream reader did not close.')
            check(EXPECTED_CHECKS[14], dialog.IsModal() and dialog.alive)
            previous_reads = len(workers)
            press(dialog.refresh_button, wx.WXK_SPACE)
            yield (lambda: len(workers) > previous_reads and not dialog.session.busy
                   and not frame.working, 20, 'Actual catalogue refresh did not finish.')
            check(EXPECTED_CHECKS[15], len(dialog.session.catalogue.components) == len(available)
                  and dialog.components.GetCount() == len(available))
            check(EXPECTED_CHECKS[16], len(workers) == 4
                  and [name for name, _ in workers] == [
                      'Read installed component inventory', 'Read selected component notice inventory',
                      'Read selected upstream notice', 'Read installed component inventory']
                  and all(identity != main_thread for _, identity in workers))
            press(dialog.close_button, wx.WXK_ESCAPE)
            yield (lambda: dialog.destroyed and modal('About Resonance and component notices') is None,
                   8, 'About did not actually destroy after native Escape.')
            check(EXPECTED_CHECKS[17], dialog.session.closed and dialog.session.cancelled.is_set()
                  and frame and frame.IsShown() and not frame._closing)
            check(EXPECTED_CHECKS[18], frame.library is library and not frame.working
                  and inventory.read_notice(expected_notices[0]) == expected_text)
            frame.Close()
            yield None

    def finish(error=None):
        if state['finished']:
            return
        state['finished'] = True
        if error is not None:
            state['error'] = str(error)
            state['traceback'] = ''.join(traceback.format_exception(error))
        # Settle only owned modal windows; no unrelated application's windows.
        for window in reversed(list(wx.GetTopLevelWindows())):
            if window and isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        app.ExitMainLoop()

    steps = scenario()
    def advance(error=None):
        if state['finished']:
            return
        try:
            wait = steps.throw(error) if error is not None else next(steps)
        except StopIteration:
            finish()
            return
        except BaseException as exc:
            finish(exc)
            return
        if wait is None:
            wx.CallLater(30, advance)
            return
        predicate, maximum, message = wait
        deadline = time.monotonic() + maximum
        def poll():
            if state['finished']:
                return
            try:
                ready = predicate()
            except BaseException as exc:
                advance(exc)
                return
            if ready:
                wx.CallAfter(advance)
            elif time.monotonic() >= deadline:
                advance(AssertionError(message))
            else:
                wx.CallLater(30, poll)
        wx.CallAfter(poll)

    watchdog = wx.CallLater(120000, lambda: finish(TimeoutError('Native About deadline.')))
    wx.CallAfter(advance)
    try:
        app.MainLoop()
    finally:
        watchdog.Stop()
        frame = windows['frame']
        if frame:
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            try:
                frame.Destroy()
            except RuntimeError:
                pass
    if state['error'] is not None or Counter(checks) != Counter(EXPECTED_CHECKS):
        print(json.dumps({'status': 'FAIL', 'checks': checks, 'error': state['error'],
                          'traceback': state['traceback'], 'nvda': 'NOT_RUN'}), flush=True)
        return 1
    print(json.dumps({'status': 'PASS', 'checks': checks, 'check_count': len(checks),
        'actual_native_keys': True, 'actual_installed_metadata': True,
        'actual_background_workers': True, 'component': 'pypdf', 'component_version': expected_version,
        'notice_sha256': sha256(expected_text.encode('utf-8')).hexdigest(),
        'module_origins': origins, 'nvda': 'NOT_RUN', 'redistribution_clearance': 'NOT_ASSESSED'}), flush=True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

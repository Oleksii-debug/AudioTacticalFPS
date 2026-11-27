"""Installed Windows Edit, recovery, Save All and close boundaries.

Only synthetic text is used. The final report is emitted after native windows,
owned worker tasks, temporary files and any optional DLL handle are released.
Screen-reader speech and physical audio are not qualified by this probe.
"""
from argparse import ArgumentParser
from contextlib import contextmanager
import faulthandler
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback

MODULES = (
    'carr_library.ui.productivity', 'carr_library.ui.save_all',
    'carr_library.ui.desktop_app', 'carr_library.ui.desktop_evidence_panel',
    'carr_library.ui.keyboard_settings', 'carr_library.evidence_drafts',
    'carr_library.ui.narration_editor',
)


def identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in path.parts}:
            raise ValueError('The native save probe imported checkout application code.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


@contextmanager
def native_directory(work):
    if work is None:
        with TemporaryDirectory(prefix='carr-save-all-native-') as temporary:
            yield Path(temporary)
    else:
        work.mkdir(mode=0o700)
        yield work


def run(work):
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.narration import NarrationStore, new_project
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.narration_editor import open_narration_editor
    from carr_library.ui import productivity, save_all as coordinator_module

    checks, failures = [], []
    before_origins = identities()
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    activation = None
    try:
        native_loop = wx.GUIEventLoop()
        activation = wx.EventLoopActivator(native_loop)
        check('real Windows native controls', wx.Platform == '__WXMSW__')
        check('save implementation loaded from installed package', all(
            'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
            for module in (productivity, coordinator_module)))

        def pump(predicate, seconds=12):
            deadline = monotonic() + seconds
            while not predicate():
                if failures:
                    raise AssertionError('\n'.join(failures))
                if monotonic() >= deadline:
                    raise TimeoutError('Native Save All boundary did not complete in time.')
                gui.Yield()
                wx.MilliSleep(10)

        def click(button):
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native button handler: ' + button.GetLabel(),
                  button.GetEventHandler().ProcessEvent(event))

        with native_directory(work) as temporary:
            library = LibraryApp(Path(temporary) / 'library')
            workspace = library.create_workspace('Save All synthetic library')
            topic = library.create_topic(workspace, 'Synthetic notes')
            imported = library.add_text(workspace, 'Immutable source', 'Exact source — Україна.')
            hit, = library.search(workspace, 'Exact')
            library.save_hit(workspace, topic, hit.ref, hit.revision, 'Baseline note')
            source_bytes = library.assets.resolve(imported['asset']).read_bytes()
            frame = DesktopFrame(library)
            gui.SetTopWindow(frame)
            frame.Show()
            releases, timers = [], []
            try:
                pump(lambda: not frame.working)
                frame._open_workspace(workspace, topic)
                pump(lambda: not frame.working and frame.evidence is not None
                     and frame.evidence.displayed is not None)
                panel, coordinator = frame.evidence, frame._save_coordinator
                real_transaction = panel.transaction
                store = NarrationStore(library.root)


                # These are the current installed main-window menus. Event dispatch
                # reaches the same registry/owner as an actual menu selection.
                print('PHASE native Edit and File recovery routes', flush=True)
                edit_index = frame.menu_bar.FindMenu('Edit')
                if edit_index < 0:
                    raise AssertionError('The native Edit menu is missing.')
                edit_items = {item.GetItemLabel().split('\t', 1)[0].replace('&', ''): item
                              for item in frame.menu_bar.GetMenu(edit_index).GetMenuItems()
                              if not item.IsSeparator()}
                check('native_edit_menu_has_six_named_actions',
                      set(edit_items) == {'Undo', 'Redo', 'Cut', 'Copy', 'Paste', 'Select all'})
                command_ids = {spec.command_id for spec in frame.keyboard_commands.registry.commands()}
                check('native_recovery_and_edit_commands_use_current_registry',
                      {'file.recover_note', 'edit.undo', 'edit.redo', 'edit.cut',
                       'edit.copy', 'edit.paste', 'edit.select_all'} <= command_ids)

                def menu_command(item):
                    event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
                    if not frame.GetEventHandler().ProcessEvent(event):
                        raise AssertionError('The installed native menu command was not handled.')

                current_note = panel.displayed.note
                panel.note.ChangeValue('Editable note — Україна')
                panel.note.SetInsertionPointEnd()
                panel.note.SetFocus()
                pump(lambda: wx.Window.FindFocus() is panel.note)
                panel.note.WriteText(' Δ')
                menu_command(edit_items['Undo'])
                check('native_edit_undo_restores_previous_draft',
                      panel.note.GetValue() == 'Editable note — Україна')
                supports_redo = panel.note.CanRedo()
                menu_command(edit_items['Redo'])
                check('native_edit_redo_obeys_current_control_capability',
                      panel.note.GetValue() == ('Editable note — Україна Δ' if supports_redo
                                                else 'Editable note — Україна'))
                if not supports_redo:
                    panel.note.SetFocus()
                    panel.note.SetInsertionPointEnd()
                    panel.note.WriteText(' Δ')
                draft = panel.note.GetValue()
                panel.note.SetSelection(0, 8)
                panel.note.SetFocus()
                menu_command(edit_items['Copy'])
                clipboard = wx.TextDataObject()
                if not wx.TheClipboard.Open():
                    raise RuntimeError('The native clipboard could not be opened for its owned synthetic value.')
                try:
                    copied = wx.TheClipboard.GetData(clipboard)
                finally:
                    wx.TheClipboard.Close()
                check('native_edit_copy_keeps_exact_draft',
                      copied and clipboard.GetText() == draft[:8] and panel.note.GetValue() == draft)
                menu_command(edit_items['Cut'])
                check('native_edit_cut_changes_only_selected_draft',
                      panel.note.GetValue() == draft[8:]
                      and library.evidence(workspace, topic)[0].note == current_note)
                menu_command(edit_items['Paste'])
                check('native_edit_paste_restores_exact_unicode', panel.note.GetValue() == draft)
                menu_command(edit_items['Select all'])
                check('native_edit_select_all_covers_current_control',
                      panel.note.GetSelection() == (0, len(draft)))
                readonly = wx.TextCtrl(frame.host, value='Read-only synthetic original — Україна',
                                       style=wx.TE_READONLY)
                readonly.SetName('Read-only Edit-route acceptance text')
                frame.layout.Add(readonly, 0, wx.EXPAND)
                frame.Layout()
                readonly.SetSelection(0, 4)
                readonly.SetFocus()
                pump(lambda: wx.Window.FindFocus() is readonly)
                menu_command(edit_items['Cut'])
                check('native_edit_refuses_readonly_mutation',
                      readonly.GetValue() == 'Read-only synthetic original — Україна')
                readonly.Destroy()

                # Exercise the real recovery chooser without committing recovered
                # text. The current producer writes its actual bound draft first.
                wanted_recovery = 'Explicit recovered research draft — Україна 🚂'
                panel.note.SetValue(wanted_recovery)
                panel._capture_recovery()
                panel._recovery_timer.Stop()
                panel.note.ChangeValue(current_note)
                file_menu = frame.menu_bar.GetMenu(frame.menu_bar.FindMenu('File'))
                recover_item = next(item for item in file_menu.GetMenuItems()
                                    if not item.IsSeparator() and
                                    item.GetItemLabel().replace('&', '') == 'Recover research-note draft...')
                recovery_choice = {'seen': False}
                recovery_deadline = monotonic() + 12

                def choose_recovery():
                    try:
                        if monotonic() >= recovery_deadline:
                            raise TimeoutError('The existing recovery chooser did not open.')
                        rows = [window for window in wx.GetTopLevelWindows()
                                if isinstance(window, wx.SingleChoiceDialog)
                                and window.GetTitle() == 'Recover research note']
                        if not rows:
                            timers.append(wx.CallLater(25, choose_recovery))
                            return
                        if len(rows) != 1 or rows[0].GetParent() is not panel:
                            raise AssertionError('Recovery did not use the current evidence owner.')
                        check('native_file_recovery_requires_explicit_choice', rows[0].GetSelection() == 2)
                        recovery_choice['seen'] = True
                        rows[0].SetSelection(0)
                        rows[0].EndModal(wx.ID_OK)
                    except Exception:
                        failures.append(traceback.format_exc())
                        for window in wx.GetTopLevelWindows():
                            if isinstance(window, wx.Dialog) and window.IsModal():
                                window.EndModal(wx.ID_CANCEL)

                timers.append(wx.CallLater(25, choose_recovery))
                menu_command(recover_item)
                check('native_file_recovery_keeps_saved_note_and_original_unchanged',
                      recovery_choice['seen'] and not failures
                      and panel.note.GetValue() == wanted_recovery and panel._dirty()
                      and library.evidence(workspace, topic)[0].note == current_note
                      and library.assets.resolve(imported['asset']).read_bytes() == source_bytes)

                for fail_commit in (False, True):
                    entered, release = Event(), Event()
                    releases.append(release)
                    previous_note = library.evidence(workspace, topic)[0].note
                    wanted_note = 'Failed note remains editable' if fail_commit else 'Committed native note — 🚂'
                    panel.note.SetValue(wanted_note)
                    reading = new_project('Immutable reading original', title='Native Save All')
                    state = {'alive': True, 'phase': 0}
                    deadline = monotonic() + 12

                    @contextmanager
                    def transaction():
                        entered.set()
                        if not release.wait(8):
                            raise TimeoutError('Synthetic write gate was not released.')
                        with real_transaction() as service:
                            yield service
                            if fail_commit:
                                raise OSError('Synthetic transaction commit failure')
                    panel.transaction = transaction

                    def ready(dialog, controller, controls, *, state=state, fail_commit=fail_commit,
                              entered=entered, release=release, previous_note=previous_note,
                              wanted_note=wanted_note, reading=reading, deadline=deadline):
                        def fail():
                            release.set()
                            if state['alive'] and dialog.IsModal():
                                dialog.EndModal(wx.ID_CANCEL)

                        def poll():
                            if not state['alive']:
                                return
                            try:
                                if monotonic() >= deadline:
                                    raise TimeoutError('Native modal save/close did not finish.')
                                if state['phase'] == 0 and entered.is_set():
                                    check('Save All waits for the real note transaction', coordinator.busy)
                                    check('note is uncommitted while the worker is held',
                                          library.evidence(workspace, topic)[0].note == previous_note)
                                    click(controls['close'])
                                    check('native Close keeps the editor open during save',
                                          dialog.IsModal() and getattr(dialog, '_save_close_pending', False))
                                    release.set()
                                    state['phase'] = 1
                                elif state['phase'] == 1 and not coordinator.busy and fail_commit:
                                    check('failed note commit cancels the pending close',
                                          dialog.IsModal() and not getattr(dialog, '_save_close_pending', False))
                                    check('failed note remains editable and baseline is preserved',
                                          panel.note.GetValue() == wanted_note and panel._dirty()
                                          and library.evidence(workspace, topic)[0].note == previous_note)
                                    check('another valid reading draft still saves after the failure',
                                          store.load(reading.id)[0].current_text == 'Native reading derivative')
                                    state['phase'] = 2
                                    dialog.Close()
                                    return
                                timers.append(wx.CallLater(25, poll))
                            except Exception:
                                failures.append(traceback.format_exc())
                                fail()

                        try:
                            check('reading editor shares the main Save All coordinator',
                                  dialog._save_coordinator is coordinator)
                            controls['editor'].SetValue('Native reading derivative')
                            click(controls['save_all'])
                            timers.append(wx.CallLater(25, poll))
                        except Exception:
                            failures.append(traceback.format_exc())
                            fail()

                    result = open_narration_editor(frame, store, reading, on_ready=ready)
                    state['alive'] = False
                    release.set()
                    pump(lambda: not frame.working)
                    if failures:
                        raise AssertionError('\n'.join(failures))
                    check('reading draft survives the complete native event flow',
                          result.current_text == 'Native reading derivative'
                          and store.load(reading.id)[0].current_text == result.current_text)
                    if not fail_commit:
                        check('successful pending close follows both durable writes',
                              library.evidence(workspace, topic)[0].note == wanted_note
                              and not coordinator.busy)
                    check('closed reading editor unregisters its save participant',
                          set(coordinator.participants) == {'research.note'})
                    panel.transaction = real_transaction

                # The main Exit/Close path must also wait, without blocking its UI thread.
                entered, release = Event(), Event()
                releases.append(release)
                @contextmanager
                def final_transaction():
                    entered.set()
                    if not release.wait(8):
                        raise TimeoutError('Synthetic main-close write was not released.')
                    with real_transaction() as service:
                        yield service
                panel.transaction = final_transaction
                panel.note.SetValue('Final note before native main-window close')
                file_menu = frame.menu_bar.GetMenu(0)
                item = next(item for item in file_menu.GetMenuItems()
                            if 'Save &all changes' in item.GetItemLabel())
                event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
                check('existing File Save All command reaches the native handler',
                      frame.GetEventHandler().ProcessEvent(event))
                pump(entered.is_set)
                frame.Close()
                check('native main close is deferred during the write',
                      not frame._closing and getattr(frame, '_save_close_pending', False))
                release.set()
                pump(lambda: frame._closing)
                check('main close occurs after the note is committed',
                      library.evidence(workspace, topic)[0].note == 'Final note before native main-window close')
                check('all save operations preserve immutable retained source bytes',
                      library.assets.resolve(imported['asset']).read_bytes() == source_bytes)
                check('no native callback failed', not failures)
                report = {'status': 'PASS', 'actual_windows': True, 'isolated': True,
                          'native_controls': True, 'checks': checks, 'total_checks': len(checks),
                          'wx': wx.version(), 'native_events': 'PASS', 'durable_stores': 'PASS',
                          'deferred_close': 'PASS', 'module_origins': before_origins,
                          'metrics': {'edit_commands': 6, 'recovery_dialogs': 1,
                                      'failed_save_scenarios': 1, 'successful_save_scenarios': 1},
                          'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'}
            finally:
                for release in releases:
                    release.set()
                for timer in timers:
                    timer.Stop()
                frame._closing = True
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                if frame:
                    frame.Destroy()
                gui.Yield()
        if failures:
            raise AssertionError('\n'.join(failures))
        if identities() != before_origins:
            raise ValueError('The installed save implementation changed during native qualification.')
        return report
    finally:
        if activation is not None:
            del activation
        gui.Destroy()
        if failures:
            raise AssertionError('\n'.join(failures))


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    if (not (args.native or args.work is not None)
            or sys.platform != 'win32' or not sys.flags.isolated):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit isolated native Windows run required.',
                          'nvda': 'NOT_RUN'}), flush=True)
        return 77
    handle = None
    faulthandler.enable()
    faulthandler.dump_traceback_later(240, exit=True)
    print('PHASE native save and Edit routes started', flush=True)
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        report = run(args.work.absolute() if args.work is not None else None)
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
                  'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native runtime cleanup: ' + str(error),
                          'nvda': 'NOT_RUN', 'physical_audio': 'NOT_RUN'}
    faulthandler.cancel_dump_traceback_later()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

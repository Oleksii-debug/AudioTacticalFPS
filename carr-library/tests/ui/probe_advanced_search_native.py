"""Exercise the real advanced-query dialog, desktop jobs and clipboard.

Source: PYTHONPATH=src:. python tests/ui/probe_advanced_search_native.py
Installed: <installed-python> -I -B -X utf8 <absolute-probe-path>

Requires an interactive native display (or Xvfb). The factory hook only schedules
input on the real QueryBuilderDialog; no control, service, search or clipboard
is replaced. Synthetic quotation text is written to the test host's clipboard.
This automated keyboard/focus probe does not claim manual NVDA acceptance.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import traceback


def _unwind_failed_probe(wx, gui):
    """Stop the outer loop after closing just the currently nested modal."""
    main_loop = gui.GetMainLoop()
    if main_loop is not None:
        main_loop.ScheduleExit(1)
    for window in reversed(wx.GetTopLevelWindows()):
        if isinstance(window, wx.Dialog) and window.IsModal():
            window.EndModal(wx.ID_CANCEL)
            return


def run_probe(wx, native_active):
    from carr_library.application import LibraryApp
    from carr_library.search import advanced
    from carr_library.ui import advanced_search, desktop_app

    failures, report, timers = [], {}, []
    frame = None

    def fail(message):
        failures.append(message)
        if frame is not None:
            frame.cancel_event.set()
        _unwind_failed_probe(wx, gui)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail(traceback.format_exc())
            return False

        def OnEventLoopExit(self, loop):
            if failures and not loop.IsMain():
                # The inner ShowModal must return before its parent can close.
                wx.CallAfter(_unwind_failed_probe, wx, self)
            super().OnEventLoopExit(loop)

    gui = ProbeApp(False)
    with TemporaryDirectory(prefix='research-advanced-native-') as temporary:
        library = LibraryApp(Path(temporary) / 'private library')
        workspace = library.create_workspace('Academic native probe')
        good_text = 'Railway transport in Україна links Lviv and Kyiv.'
        sources = (
            ('Selected academic source', good_text),
            ('Excluded fiction source', good_text + ' Fiction follows.'),
            ('Distant source', 'Railway trade in Україна links Kyiv through several old towns to Lviv.'),
            ('No alternative source', 'Railway history in Україна links Lviv and Kyiv.'),
        )
        for title, text in sources:
            imported = library.add_text(workspace, title, text, authors=('Synthetic researcher',))
            assert imported['state'] == 'complete', imported
        other = library.create_workspace('Separate private library')
        imported = library.add_text(other, 'Out of scope source', good_text)
        assert imported['state'] == 'complete', imported

        frame = desktop_app.DesktopFrame(library)
        if frame.active != (workspace, None):
            frame._open_workspace(workspace)
        assert frame.active == (workspace, None) and frame.book is not None
        panel = frame.book.GetPage(0)
        frame.SetSize((1100, 950))
        frame.Show()
        frame.Raise()
        original_factory = advanced_search.QueryBuilderDialog
        scenarios = []
        snapshot = {}
        expected_query = ('"railway" AND "Україна" AND ("transport" OR "trade") '
                          'AND ("Kyiv" NEAR/1 "Lviv") AND NOT "fiction"')
        simulator = wx.UIActionSimulator()

        def guarded(callback):
            if failures:
                return
            try:
                callback()
            except BaseException:
                fail(traceback.format_exc())

        def later(callback, milliseconds=30):
            timers.append(wx.CallLater(milliseconds, lambda: guarded(callback)))

        def click(button):
            assert button.IsShownOnScreen() and button.IsEnabled(), button.GetName()
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            assert button.GetEventHandler().ProcessEvent(event), button.GetLabel()

        def busy():
            assert frame.working and frame.busy
            assert frame.cancel_button.IsEnabled()
            assert not frame.choice.IsEnabled() and not frame.book.IsEnabled()

        def ready():
            assert not frame.working and not frame.busy
            assert not frame.cancel_button.IsEnabled()
            assert frame.choice.IsEnabled() and frame.book.IsEnabled()
            assert all(frame.menu_bar.IsEnabledTop(i) for i in range(frame.menu_bar.GetMenuCount()))

        def focus(control):
            assert wx.Window.FindFocus() is control, (
                'Unexpected focus', control.GetName(),
                wx.Window.FindFocus().GetName() if wx.Window.FindFocus() else None)

        def dialog_factory(*args, **kwargs):
            assert scenarios, 'An unexpected builder dialog was opened.'
            form = original_factory(*args, **kwargs)
            scenario = scenarios.pop(0)
            later(lambda: scenario(form), 100)
            return form

        advanced_search.QueryBuilderDialog = dialog_factory

        def begin():
            commands = frame.keyboard_commands
            assert commands.shortcut('main.advanced_query') == 'Ctrl+Alt+Q'
            assert commands.available('main.advanced_query')
            panel.query.SetValue('railway')
            panel.retrieval.SetSelection(0)
            panel.result_mode.SetSelection(0)
            click(panel.go)
            busy()
            later(baseline_done)

        def baseline_done():
            if frame.working:
                later(baseline_done)
                return
            ready()
            assert panel.model.row_count() == 4, (frame.status.GetValue(), panel.status.GetValue())
            assert {hit.title for hit in panel.model.hits} == {title for title, _ in sources}
            snapshot.update(query=panel.query.GetValue(), mode=panel.retrieval.GetSelection(),
                            hits=tuple(panel.model.hits), results=dict(panel.results),
                            epoch=panel._context_epoch, selected=panel.selected(),
                            details=panel.details.GetValue())
            scenarios.append(cancel_dialog)
            focus(panel.query)
            assert simulator.KeyDown(wx.WXK_CONTROL)
            try:
                assert simulator.KeyDown(wx.WXK_ALT)
                try:
                    assert simulator.KeyDown(ord('Q'))
                    assert simulator.KeyUp(ord('Q'))
                finally:
                    simulator.KeyUp(wx.WXK_ALT)
            finally:
                simulator.KeyUp(wx.WXK_CONTROL)
            later(cancel_completed, 80)

        def cancel_completed():
            if failures:
                return
            # OS keyboard events are asynchronous; keep pumping until the
            # scripted native modal has opened, been reviewed and cancelled.
            if scenarios or any(isinstance(window, wx.Dialog) and window.IsModal()
                                for window in wx.GetTopLevelWindows()):
                later(cancel_completed, 80)
                return
            assert panel.query.GetValue() == snapshot['query']
            assert panel.retrieval.GetSelection() == snapshot['mode']
            assert tuple(panel.model.hits) == snapshot['hits']
            assert panel.results == snapshot['results']
            assert panel._context_epoch == snapshot['epoch']
            assert panel.selected() == snapshot['selected']
            assert panel.details.GetValue() == snapshot['details']
            later(after_cancel, 80)

        def cancel_dialog(form):
            assert form.dialog.IsModal()
            assert form.current.GetValue() == 'railway'
            assert form.current.GetWindowStyleFlag() & wx.TE_READONLY
            names = {'all_terms': 'All of these', 'any_terms': 'Any of these',
                     'excluded_terms': 'Exclude', 'near_left': 'Nearby first phrase',
                     'near_right': 'Nearby second phrase'}
            for name, label in names.items():
                field = form.fields[name]
                assert isinstance(field, wx.TextCtrl) and label in field.GetName()
                assert field.AcceptsFocusFromKeyboard()
                assert field.GetValue() == '', 'The existing expression must not be guessed into fields.'
                if name in ('all_terms', 'any_terms', 'excluded_terms'):
                    assert field.GetWindowStyleFlag() & wx.TE_MULTILINE
            for control in (form.preview, form.status):
                assert control.GetWindowStyleFlag() & wx.TE_READONLY
                assert control.GetName() and control.AcceptsFocusFromKeyboard(), control.GetName()
            assert form.distance.GetMin() == 0 and form.distance.GetMax() == 100
            assert form.distance.GetValue() == 5
            for button in (form.build_button, form.use_button, form.cancel_button, form.help_button):
                assert isinstance(button, wx.Button) and button.GetName()
            assert not form.use_button.IsEnabled()
            focus(form.fields['all_terms'])
            assert simulator.KeyDown(wx.WXK_TAB)
            assert simulator.KeyUp(wx.WXK_TAB)
            later(lambda: tab_forward(form), 80)

        def tab_forward(form):
            focus(form.fields['any_terms'])
            assert simulator.KeyDown(wx.WXK_SHIFT)
            try:
                assert simulator.KeyDown(wx.WXK_TAB)
                assert simulator.KeyUp(wx.WXK_TAB)
            finally:
                simulator.KeyUp(wx.WXK_SHIFT)
            later(lambda: tab_back(form), 80)

        def tab_back(form):
            focus(form.fields['all_terms'])
            later(read_help, 80)
            click(form.help_button)
            if failures:
                return
            focus(form.help_button)
            form.fields['excluded_terms'].SetValue('fiction')
            later(lambda: invalid_build(form))

        def read_help():
            dialogs = [window for window in wx.GetTopLevelWindows()
                       if isinstance(window, wx.Dialog) and window.IsModal()
                       and window.GetTitle() == 'Advanced query help']
            assert len(dialogs) == 1
            dialog = dialogs[0]
            readers = [child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl)]
            assert len(readers) == 1
            reader = readers[0]
            assert reader.GetWindowStyleFlag() & wx.TE_READONLY
            assert reader.GetName() == 'Advanced query syntax and builder help'
            assert 'distinct, nonoverlapping occurrences' in reader.GetValue()
            assert 'page or logical text segment' in reader.GetValue()
            focus(reader)
            close_buttons = [child for child in dialog.GetChildren()
                             if isinstance(child, wx.Button) and child.GetId() == wx.ID_CANCEL]
            assert len(close_buttons) == 1, 'Help must have exactly one local Close button.'
            assert close_buttons[0].GetName() == 'Close advanced query help'
            click(close_buttons[0])

        def invalid_build(form):
            click(form.build_button)
            later(lambda: invalid_checked(form))

        def invalid_checked(form):
            assert 'positive term' in form.status.GetValue()
            assert form.fields['excluded_terms'].GetValue() == 'fiction'
            assert not form.preview.GetValue() and not form.use_button.IsEnabled()
            focus(form.status)
            form.fields['excluded_terms'].SetValue('')
            form.fields['all_terms'].SetValue('Temporary local phrase')
            later(lambda: temporary_build(form))

        def temporary_build(form):
            click(form.build_button)
            later(lambda: temporary_built(form))

        def temporary_built(form):
            assert form.preview.GetValue() == '"Temporary local phrase"'
            assert form.use_button.IsEnabled()
            focus(form.preview)
            assert simulator.KeyDown(wx.WXK_TAB)
            assert simulator.KeyUp(wx.WXK_TAB)
            later(lambda: readonly_status_reached(form), 80)

        def readonly_status_reached(form):
            focus(form.status)
            assert 'Query built.' in form.status.GetValue()
            assert simulator.KeyDown(wx.WXK_SHIFT)
            try:
                assert simulator.KeyDown(wx.WXK_TAB)
                assert simulator.KeyUp(wx.WXK_TAB)
            finally:
                simulator.KeyUp(wx.WXK_SHIFT)
            later(lambda: readonly_preview_reached(form), 80)

        def readonly_preview_reached(form):
            focus(form.preview)
            assert form.preview.GetValue() == '"Temporary local phrase"'
            form.fields['all_terms'].SetFocus()
            form.fields['all_terms'].SetValue('Changed temporary phrase')
            later(lambda: edited(form))

        def edited(form):
            assert not form.preview.GetValue() and not form.use_button.IsEnabled()
            assert form.built is None
            focus(form.fields['all_terms'])
            click(form.cancel_button)

        def after_cancel():
            focus(panel.query)
            scenarios.append(use_dialog)
            click(panel.advanced_builder)
            if failures:
                return
            assert panel.query.GetValue() == expected_query
            assert panel.MODES[panel.retrieval.GetSelection()] == 'advanced'
            assert panel.model.row_count() == 0 and not panel.results
            assert not panel.grid.selected_rows() and not panel.details.GetValue()
            assert panel._context_epoch > snapshot['epoch']
            later(run_advanced, 80)

        def use_dialog(form):
            assert form.current.GetValue() == 'railway'
            values = dict(all_terms='railway\nУкраїна', any_terms='transport\ntrade',
                          excluded_terms='fiction', near_left='Kyiv', near_right='Lviv')
            for name, value in values.items():
                form.fields[name].SetValue(value)
            form.distance.SetValue(1)
            later(lambda: final_build(form))

        def final_build(form):
            click(form.build_button)
            later(lambda: final_built(form))

        def final_built(form):
            assert form.preview.GetValue() == expected_query
            assert form.use_button.IsEnabled()
            focus(form.preview)
            click(form.use_button)

        def run_advanced():
            focus(panel.query)
            # Use the real Enter binding on the host query control.
            event = wx.CommandEvent(wx.EVT_TEXT_ENTER.typeId, panel.query.GetId())
            event.SetEventObject(panel.query)
            assert panel.query.GetEventHandler().ProcessEvent(event)
            busy()
            later(advanced_done)

        def advanced_done():
            if frame.working:
                later(advanced_done)
                return
            ready()
            assert panel.model.row_count() == 1, (frame.status.GetValue(), panel.status.GetValue())
            selected = panel.selected()
            assert selected.title == 'Selected academic source' and selected.workspace == workspace
            assert selected.quote == good_text
            assert panel.results[selected.ref].verified
            assert 'one page or text segment' in panel.status.GetValue()
            # Windows' generic data view may focus its own native child.
            focused = wx.Window.FindFocus()
            assert focused is panel.grid.control or (
                focused is not None and panel.grid.control.IsDescendant(focused))
            buttons = [child for child in panel.GetChildren() if isinstance(child, wx.Button)
                       and child.GetLabel().replace('&', '') == 'Copy quote']
            assert len(buttons) == 1
            click(buttons[0])
            busy()
            later(copy_done)

        def copy_done():
            if frame.working:
                later(copy_done)
                return
            ready()
            assert panel.status.GetValue().startswith('Copied quote'), panel.status.GetValue()
            assert wx.TheClipboard.Open(), 'Could not read the actual test clipboard.'
            try:
                copied = wx.TextDataObject()
                assert wx.TheClipboard.GetData(copied)
                assert copied.GetText() == good_text
            finally:
                wx.TheClipboard.Close()
            later(finish, 80)

        def finish():
            focus(panel.status)
            assert panel.status.GetName() == 'Search status'
            assert panel.status.AcceptsFocusFromKeyboard()
            assert not scenarios
            report.update(status='PASS', platform=platform.system(), wx_version=wx.version(),
                native_runtime_active=native_active, windows_nvda='NOT_RUN',
                checks=['real TXT imports and separate library', 'named native dialog fields',
                        'registered advanced command and native Ctrl+Alt+Q routing',
                        'keyboard Tab and Shift+Tab', 'native readable Help and focus return',
                        'negative-only validation and status focus', 'reviewable Build preview',
                        'keyboard Tab and Shift+Tab through readonly preview and status',
                        'editing invalidates Use', 'Cancel preserves query and results',
                        'All/Any/Exclude/NEAR Use updates advanced mode', 'stale results cleared',
                        'query focus returned', 'actual asynchronous advanced Enter search',
                        'unordered nearby match and excluded/distant sources rejected',
                        'workspace isolation', 'exact source quotation', 'actual clipboard copy',
                        'controls restored and readable status focus'],
                modules_sha256={name: sha256(Path(module.__file__).read_bytes()).hexdigest()
                    for name, module in (('desktop_app', desktop_app),
                                         ('query_builder', advanced_search), ('advanced', advanced))})
            frame.Close()

        timeout = wx.CallLater(40000, lambda: fail('Native advanced-search probe exceeded 40 seconds.'))
        later(begin, 100)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            for timer in timers:
                timer.Stop()
            advanced_search.QueryBuilderDialog = original_factory
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()

    if failures or report.get('status') != 'PASS':
        print(json.dumps({'status': 'FAIL', 'errors': failures or ['Probe did not finish.'],
                          'windows_nvda': 'NOT_RUN'}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main():
    # The private Windows DLL directory must be active before importing wx.
    from carr_library.native_runtime import activate_bundled_runtime, _discover

    handle = activate_bundled_runtime()
    try:
        bundle = _discover()
        if sys.platform == 'win32' and bundle is not None:
            manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
            if manifest.get('bundled_native_runtime') is True:
                assert handle is not None, 'Declared private Windows runtime was not activated.'
        if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
            print(json.dumps({'status': 'NOT_RUN', 'reason': 'A native display or Xvfb is required.',
                              'windows_nvda': 'NOT_RUN'}))
            return 2
        import wx

        return run_probe(wx, handle is not None)
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())

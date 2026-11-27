"""Additive real-wx Search Center smoke for the existing desktop event loop.

Call run_native_federated_smoke(frame, completed, failed) from an existing wx
callback. No second wx.App is created. It uses synthetic local text, real search,
source validation, clipboard and topic saving. It does not assert NVDA behavior.
"""
from hashlib import sha256
from pathlib import Path
import traceback

import wx

from carr_library.ui import federated_search


def _dismiss_modal_descendant(parent):
    """End the deepest child modal first, leaving its caller time to unwind."""
    descendants = []
    for window in wx.GetTopLevelWindows():
        try:
            if window is parent or not isinstance(window, wx.Dialog) or not window.IsModal():
                continue
            current, depth = window.GetParent(), 1
            while current is not None and current is not parent:
                current, depth = current.GetParent(), depth + 1
            if current is parent:
                descendants.append((depth, window))
        except RuntimeError:
            # wx may have deleted a dialog while removing it from the window list.
            continue
    if not descendants:
        return False
    _, deepest = max(descendants, key=lambda pair: pair[0])
    deepest.EndModal(wx.ID_CANCEL)
    return True


def run_native_federated_smoke(frame, completed, failed):
    """completed(report: dict) or failed(traceback: str), after modal dismissal."""
    app = frame.library
    active, book = frame.active, frame.book
    assert active is not None and not frame.working
    workspace = app.create_workspace('Search Center native source library')
    topic = app.create_topic(workspace, 'Native destination topic')
    app.add_text(workspace, 'Native federated source',
                 'Railway native source. Exact context beyond the selected quotation.',
                 authors=('Synthetic native author',), language='en', edition='First')
    for number in range(11):
        app.add_text(workspace, f'Native result page {number:02}',
                     f'Paging sequence document {number}.', authors=('Page fixture author',))
    host_selections = dict(app._selections)
    native = federated_search.NativeSearchCenter(frame, app.workspaces())
    controller = native.controller
    errors, checks = [], []
    state = {'context_closed': False, 'topic_chosen': False, 'note_entered': False,
             'completed': False, 'hit': None}

    def later(callback, milliseconds=20):
        wx.CallLater(milliseconds, lambda: guarded(callback))

    def guarded(callback):
        if errors:
            return
        try:
            callback()
        except BaseException:
            errors.append(traceback.format_exc())
            controller.cancel()
            wait_failure_close()

    def wait_failure_close():
        # A failed assertion can occur inside NativeDialogs.show/choose/text.
        # Dismiss that nested loop before attempting to end the parent modal.
        # The next event-loop turn lets its finally block destroy the child.
        if _dismiss_modal_descendant(native.window):
            wx.CallLater(20, wait_failure_close)
        elif controller.running or frame.working:
            wx.CallLater(20, wait_failure_close)
        elif native.window.IsModal():
            native._close()

    def click(button):
        assert button is not None and button.IsShownOnScreen() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event), 'Native button event was not handled.'

    def dialog_with_title(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.IsShown()
                     and window.GetTitle() == title), None)

    def assert_scope():
        assert frame.library is app and frame.active == active and frame.book is book
        assert app._selections == host_selections, 'Search Center replaced the desktop result cache.'

    def assert_busy():
        assert frame.working and controller.running
        assert native.cancel_button.IsEnabled()
        for control in (native.query, native.libraries_control, native.retrieval,
                        native.presentation, native.search_button, native.results,
                        native.builder_button, native.previous_button, native.next_button,
                        native.close_button, native.quote_button, native.save_button):
            assert not control.IsEnabled(), control.GetName()

    def begin():
        assert native.window.IsModal()
        checked = tuple(row.id for index, row in enumerate(native.libraries)
                        if native.libraries_control.IsChecked(index))
        assert checked == (active[0],), checked
        assert native.libraries_control.GetName() == 'Libraries to search (Space selects)'
        assert native.results.GetName() == 'Search results with library and verification status'
        assert native.status.GetName() == 'Search Center status'
        assert not native.previous_button.IsEnabled() and not native.next_button.IsEnabled()
        for index, row in enumerate(native.libraries):
            native.libraries_control.Check(index, row.id in (active[0], workspace))
        event = wx.CommandEvent(wx.EVT_CHECKLISTBOX.typeId, native.libraries_control.GetId())
        event.SetEventObject(native.libraries_control)
        native.libraries_control.GetEventHandler().ProcessEvent(event)
        native.query.SetValue('railway')
        click(native.search_button)
        assert_busy()
        later(search_ready)

    def search_ready():
        if frame.working:
            later(search_ready)
            return
        assert not controller.running and controller.rows, native.status.GetValue()
        assert {hit.workspace for hit in controller.rows} == {active[0], workspace}
        index = next(index for index, hit in enumerate(controller.rows) if hit.workspace == workspace)
        native.results.SetSelection(index)
        event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, native.results.GetId())
        event.SetEventObject(native.results)
        native.results.GetEventHandler().ProcessEvent(event)
        state['hit'] = controller.rows[index]
        assert 'Search Center native source library' in native.results.GetString(index)
        assert workspace in native.details.GetValue()
        assert native.quote_button.IsEnabled() and native.save_button.IsEnabled()
        assert_scope()
        checks.extend(['current-library-only default', 'explicit multi-library native search event',
                       'library and verification labels', 'worker controls disabled and restored'])
        later(context_dialog)
        click(native.context_button)
        assert_busy()

    def context_dialog():
        dialog = dialog_with_title('Verified original source context')
        if dialog is None:
            assert controller.running or frame.working, native.status.GetValue()
            later(context_dialog)
            return
        readers = [child for child in dialog.GetChildren() if isinstance(child, wx.TextCtrl)]
        assert len(readers) == 1
        assert workspace in readers[0].GetValue()
        assert 'Exact context beyond the selected quotation.' in readers[0].GetValue()
        click(dialog.FindWindow(wx.ID_CANCEL))
        state['context_closed'] = True
        later(copy_begin, milliseconds=40)

    def copy_begin():
        assert state['context_closed'] and wx.Window.FindFocus() is native.results
        checks.append('fresh original context and result focus restoration')
        click(native.quote_button)
        assert_busy()
        later(copy_ready)

    def copy_ready():
        if frame.working:
            later(copy_ready)
            return
        assert 'copied from Search Center native source library' in native.status.GetValue()
        assert wx.TheClipboard.Open(), 'Native clipboard was unavailable.'
        try:
            value = wx.TextDataObject()
            assert wx.TheClipboard.GetData(value)
            assert value.GetText() == state['hit'].result.quote
        finally:
            wx.TheClipboard.Close()
        assert wx.Window.FindFocus() is native.results
        checks.append('fresh verified native clipboard copy')
        later(save_dialogs)
        click(native.save_button)
        assert_busy()

    def save_dialogs():
        choose = dialog_with_title('Save quotation to a topic')
        note = dialog_with_title('Quotation note')
        if choose is not None and not state['topic_chosen']:
            assert isinstance(choose, wx.SingleChoiceDialog)
            choose.SetSelection(0)
            state['topic_chosen'] = True
            # Keep the timer alive while the next native modal opens.
            later(save_dialogs)
            click(choose.FindWindow(wx.ID_OK))
            return
        if note is not None and not state['note_entered']:
            fields = [child for child in note.GetChildren() if isinstance(child, wx.TextCtrl)]
            assert len(fields) == 1
            fields[0].SetValue('Native saved quotation note.')
            state['note_entered'] = True
            click(note.FindWindow(wx.ID_OK))
            later(save_ready)
            return
        assert frame.working or controller.running or state['topic_chosen'], native.status.GetValue()
        later(save_dialogs)

    def save_ready():
        if frame.working:
            later(save_ready)
            return
        saved, = app.evidence(workspace, topic)
        assert saved.anchor.identity.workspace == workspace
        assert saved.anchor.quote == state['hit'].result.quote
        assert saved.note == 'Native saved quotation note.'
        assert_scope()
        assert wx.Window.FindFocus() is native.results
        checks.append('native topic chooser saves only in result library')
        state['before_builder'] = (controller.rows, controller.epoch,
            native.query.GetValue(), native.retrieval.GetSelection())
        later(builder_cancel_dialog)
        click(native.builder_button)
        if errors:
            return
        assert state['before_builder'] == (controller.rows, controller.epoch,
            native.query.GetValue(), native.retrieval.GetSelection())
        later(builder_use_begin, milliseconds=60)

    def builder_controls():
        dialog = dialog_with_title('Advanced search query builder')
        assert dialog is not None and dialog.IsModal()
        return {child.GetName(): child for child in dialog.GetChildren()}

    def builder_cancel_dialog():
        controls = builder_controls()
        assert controls['Current query; shown for reference only'].GetValue() == 'railway'
        assert wx.Window.FindFocus() is controls['All of these; one word or phrase per line']
        controls['All of these; one word or phrase per line'].SetValue('a discarded replacement')
        click(controls['Cancel; keep existing query'])

    def builder_use_begin():
        assert wx.Window.FindFocus() is native.query
        checks.append('native builder Cancel preserves query mode results and source scope')
        native.filters['author'].SetValue('Synthetic native author')
        state['checked_libraries'] = tuple(row.id for index, row in enumerate(native.libraries)
            if native.libraries_control.IsChecked(index))
        later(builder_use_dialog)
        click(native.builder_button)
        if errors:
            return
        assert native.query.GetValue() == ('"railway" AND ("native" OR "transport") '
            'AND ("railway" NEAR/1 "source") AND NOT "fiction"')
        assert federated_search.MODES[native.retrieval.GetSelection()] == 'advanced'
        assert controller.rows == () and native.results.GetCount() == 0
        assert native.filters['author'].GetValue() == 'Synthetic native author'
        assert tuple(row.id for index, row in enumerate(native.libraries)
            if native.libraries_control.IsChecked(index)) == state['checked_libraries']
        assert wx.Window.FindFocus() is native.query
        checks.append('native builder Use preserves checked libraries and filters while withdrawing stale results')
        click(native.search_button)
        assert_busy()
        later(advanced_search_ready)

    def builder_use_dialog():
        controls = builder_controls()
        controls['All of these; one word or phrase per line'].SetValue('railway')
        controls['Any of these; one word or phrase per line'].SetValue('native\ntransport')
        controls['Exclude; one word or phrase per line'].SetValue('fiction')
        controls['Nearby first phrase; optional'].SetValue('railway')
        controls['Nearby second phrase; optional'].SetValue('source')
        controls['Nearby distance; maximum intervening words, 0 to 100'].SetValue(1)
        click(controls['Build and preview query'])
        assert wx.Window.FindFocus() is controls['Generated query preview']
        assert controls['Use generated query in Search'].IsEnabled()
        click(controls['Use generated query in Search'])

    def advanced_search_ready():
        if frame.working:
            later(advanced_search_ready)
            return
        hit, = controller.rows
        assert hit.workspace == workspace and hit.result.quote == 'Railway native source.'
        assert wx.Window.FindFocus() is native.results
        state['advanced_hit'] = hit
        assert_scope()
        checks.append('actual multi-library Advanced AND OR NOT and NEAR search with metadata filter')
        click(native.quote_button)
        assert_busy()
        later(advanced_copy_ready)

    def advanced_copy_ready():
        if frame.working:
            later(advanced_copy_ready)
            return
        assert wx.TheClipboard.Open()
        try:
            value = wx.TextDataObject()
            assert wx.TheClipboard.GetData(value)
            assert value.GetText() == state['advanced_hit'].result.quote
        finally:
            wx.TheClipboard.Close()
        checks.append('advanced result authentic quotation copied to native clipboard')
        native.filters['author'].SetValue('')
        native.limit.SetSelection(0)  # Ten results; the eleventh is on the next page.
        native.query.SetValue('"paging" AND "sequence"')
        click(native.search_button)
        assert_busy()
        later(first_page_ready)

    def hit_identity(hit):
        return hit.workspace, hit.result.title, hit.result.quote

    def first_page_ready():
        if frame.working:
            later(first_page_ready)
            return
        assert len(controller.rows) == 10, native.status.GetValue()
        assert native.next_button.IsEnabled() and not native.previous_button.IsEnabled()
        state['first_page'] = tuple(map(hit_identity, controller.rows))
        click(native.next_button)
        assert_busy()
        later(second_page_ready)

    def second_page_ready():
        if frame.working:
            later(second_page_ready)
            return
        hit, = controller.rows
        assert hit_identity(hit) not in state['first_page']
        assert native.previous_button.IsEnabled() and not native.next_button.IsEnabled()
        assert 'Page 2.' in native.status.GetValue()
        assert 'No further matching page' in native.status.GetValue()
        click(native.previous_button)
        assert_busy()
        later(previous_page_ready)

    def previous_page_ready():
        if frame.working:
            later(previous_page_ready)
            return
        assert tuple(map(hit_identity, controller.rows)) == state['first_page']
        assert native.next_button.IsEnabled() and not native.previous_button.IsEnabled()
        assert_scope()
        checks.extend(['native Next and Previous pages preserve unchanged source order',
                       'last page stops navigation without duplicate source results',
                       'page navigation reuses the selected libraries and query'])
        # A subsequent attempt cannot close the modal while completion is pending.
        native.query.SetValue('source')
        assert controller.rows == () and not native.quote_button.IsEnabled()
        assert not native.previous_button.IsEnabled() and not native.next_button.IsEnabled()
        click(native.search_button)
        assert_busy()
        native.window.Close()
        assert controller.running and native.window.IsModal()
        assert frame.cancel_event.is_set()
        later(cancel_ready)

    def cancel_ready():
        if frame.working:
            later(cancel_ready)
            return
        assert not controller.running and controller.rows == ()
        assert native.results.GetCount() == 0 and not native.quote_button.IsEnabled()
        assert 'cancelled' in native.status.GetValue().casefold()
        assert_scope()
        checks.extend(['option changes invalidate previous results', 'cancelled search exposes no rows',
                       'close veto while worker active', 'desktop scope and selection cache preserved'])
        state['completed'] = True
        click(native.close_button)

    def timed_out():
        if not state['completed'] and not errors:
            errors.append('Research Search Center native smoke exceeded 30 seconds.')
            controller.cancel()
            wait_failure_close()

    timeout = wx.CallLater(30000, timed_out)
    later(begin)
    try:
        native.show()
    finally:
        timeout.Stop()
    if errors:
        failed('\n'.join(errors))
    elif not state['completed']:
        failed('Research Search Center closed before the native workflow completed.')
    else:
        completed({'status': 'PASS', 'checks': checks, 'windows_nvda': 'NOT_RUN',
                   'federated_ui_sha256': sha256(Path(federated_search.__file__).read_bytes()).hexdigest()})

"""Real wx academic workflow probe using only disposable synthetic research.

Windows CI runs this script with ``python -I ... --require-windows`` against the
installed package. A local wx/Xvfb run is also useful but is reported as Linux,
never Windows or NVDA acceptance. No model, network lookup or user corpus is used.
The RIS review dialog is native; the operating-system file picker is not tested.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback

import wx

from carr_library.application import LibraryApp
from carr_library.catalog.ris_apply import apply_ris_metadata, read_ris_file
from carr_library.ui import academic_tools, bibliography_import, catalog_quality, literature_matrix
from carr_library.ui.desktop_app import DesktopFrame


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--require-windows', action='store_true')
    args = parser.parse_args()
    if args.require_windows and sys.platform != 'win32':
        raise RuntimeError('This workflow requires actual native Windows execution.')
    failures, checks, timers = [], [], []
    state = {}

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail(traceback.format_exc())
            return False

    gui = ProbeApp(False)

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def fail(message):
        failures.append(message)
        for window in wx.GetTopLevelWindows():
            if isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    def guarded(callback):
        if failures:
            return
        try:
            callback()
        except BaseException:
            fail(traceback.format_exc())

    def later(callback, milliseconds=25):
        timers.append(wx.CallLater(milliseconds, lambda: guarded(callback)))

    def wait_for(predicate, action, description, *, seconds=12):
        deadline = monotonic() + seconds

        def poll():
            value = predicate()
            if value:
                action(value)
            elif monotonic() >= deadline:
                raise TimeoutError('Native academic probe timed out waiting for ' + description)
            else:
                later(poll)
        later(poll)

    def event(control, kind, *, value=None, required=True):
        native = wx.CommandEvent(kind.typeId, control.GetId())
        native.SetEventObject(control)
        if value is not None:
            native.SetInt(value)
        handled = control.GetEventHandler().ProcessEvent(native)
        if required and not handled:
            raise AssertionError('Native event was not handled: ' + control.GetName())

    def choose(control, index, kind=wx.EVT_CHOICE):
        control.SetSelection(index)
        event(control, kind, value=index)

    def tick(control, value):
        control.SetValue(value)
        # Native checkbox state is read by Apply; it has no additional handler.
        event(control, wx.EVT_CHECKBOX, value=int(value), required=False)

    def click(button):
        check('native button enabled: ' + button.GetLabel().replace('&', ''), button.IsEnabled())
        event(button, wx.EVT_BUTTON)

    def dialog_named(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.IsShown()
                     and window.GetTitle() == title), None)

    def await_dialog(title, action):
        wait_for(lambda: dialog_named(title), action, title)

    with TemporaryDirectory(prefix='carr-native-academic-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'private library')
        workspace = library.create_workspace('Synthetic academic research')
        with library.topic_transaction(workspace) as tree:
            tree.create('root', 'Research review')
            tree.create('child', 'Archive section', parent='root')
        for identity, raw, edition, volume in (
                ('edition-first', 'Railway archive alpha. Український точний текст.', 'First', 'I'),
                ('edition-revised', 'Railway archive beta. Slovenský presný text.', 'Revised', 'II')):
            result = library.add_text(workspace, 'Repeated title', raw, authors=('Олена',),
                language='uk', edition=edition, volume=volume, edition_id=identity,
                work_id='work-' + identity)
            check('actual TXT import completed: ' + identity, result['state'] == 'complete')
        with library.catalog_transaction(workspace) as catalog:
            catalog.create_collection('filed', 'Filed sources')
            catalog.assign('filed', 'edition-first')
        hits = {hit.edition_id: hit for hit in library.search(workspace, 'Railway archive')}
        first, revised = hits['edition-first'], hits['edition-revised']
        evidence_id = library.save_hit(workspace, 'root', first.ref, first.revision, 'Saved root note')
        child_id = library.save_hit(workspace, 'child', revised.ref, revised.revision, 'Saved child note')
        library.set_evidence_tags(workspace, 'root', evidence_id, ('Method',))
        library.set_evidence_tags(workspace, 'child', child_id, ('Archive',))
        initial_saved = library.evidence(workspace, 'root')[0]
        original_assets = {path.name: sha256(path.read_bytes()).hexdigest()
                           for path in library.assets_dir.iterdir()}
        frame = DesktopFrame(library)
        frame.SetSize((1180, 1000))
        gui.SetTopWindow(frame)
        frame.Show()
        frame.Raise()

        def ready():
            return not frame.working and not frame.busy

        def invoke_academic(label):
            check('academic menu is available for ' + label, ready())
            index = frame.menu_bar.FindMenu('Academic')
            check('native Academic menu exists', index >= 0)
            menu = frame.menu_bar.GetMenu(index)
            item = next((item for item in menu.GetMenuItems()
                         if item.GetItemLabelText() == label), None)
            check('native menu item exists: ' + label, item is not None)
            command = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
            command.SetEventObject(frame)
            check('native menu event handled: ' + label, frame.GetEventHandler().ProcessEvent(command))

        def evidence_ready():
            return (ready() and frame.evidence is not None and frame.evidence.displayed is not None
                    and frame.evidence.displayed.evidence_id == evidence_id)

        def begin():
            menus = [frame.menu_bar.GetMenu(i) for i in range(frame.menu_bar.GetMenuCount())]
            shortcuts = [item.GetItemLabel().partition('\t')[2].casefold()
                         for menu in menus for item in menu.GetMenuItems()
                         if '\t' in item.GetItemLabel()]
            check('main window shortcuts have no conflicting actions',
                  len(shortcuts) == len(set(shortcuts)))
            labels = [frame.menu_bar.GetMenuLabel(i) for i in range(frame.menu_bar.GetMenuCount())]
            mnemonics = [label.split('&', 1)[1][0].casefold() for label in labels if '&' in label]
            check('main menu access keys are distinct', len(mnemonics) == len(set(mnemonics)))
            index = frame.menu_bar.FindMenu('Academic')
            check('Academic menu contains all four integrated actions', index >= 0 and
                  frame.menu_bar.GetMenu(index).GetMenuItemCount() == 6)
            frame.choice.SetSelection(frame.scopes.index((workspace, 'root')))
            button = next(child for child in frame.host.GetChildren()
                          if isinstance(child, wx.Button) and child.GetLabel() == 'Open selected scope')
            click(button)
            wait_for(evidence_ready, lambda _: compare(), 'initial saved evidence')

        def compare(*, draft=False):
            await_dialog('Compare sources in this topic',
                         lambda dialog: drive_matrix(dialog, draft=draft))
            invoke_academic('Compare sources in active topic')

        def drive_matrix(dialog, *, draft):
            check('comparison uses actual native source and evidence lists',
                  isinstance(dialog.sources, wx.ListBox) and isinstance(dialog.evidence, wx.ListBox))
            check('comparison names its keyboard controls', all(control.GetName() for control in
                (dialog.sources, dialog.evidence, dialog.tags, dialog.descendants, dialog.details)))
            if not draft:
                check('root and descendant source versions both appear', len(dialog.source_rows) == 2)
                choose(dialog.tags, dialog.tag_names.index('Archive'))
                check('native tag filter selects only descendant evidence', len(dialog.source_rows) == 1
                      and dialog.evidence_rows[0].topic_id == 'child')
                tick(dialog.descendants, False)
                check('native descendant checkbox removes out-of-scope evidence',
                      not dialog.source_rows and not dialog.open_button.IsEnabled())
                choose(dialog.tags, 0)
                check('root-only native filter keeps root evidence', len(dialog.source_rows) == 1
                      and dialog.evidence_rows[0].topic_id == 'root')
                tick(dialog.descendants, True)
                check('native descendant checkbox restores both versions', len(dialog.source_rows) == 2)
            index = next(index for index, (source, _) in enumerate(dialog.source_rows)
                         if source.edition_id == 'edition-first')
            choose(dialog.sources, index, wx.EVT_LISTBOX)
            chosen = next(index for index, item in enumerate(dialog.evidence_rows)
                          if item.evidence_id == evidence_id and item.topic_id == 'root')
            choose(dialog.evidence, chosen, wx.EVT_LISTBOX)
            check('comparison separates exact quotation from research note',
                  initial_saved.anchor.quote in dialog.details.GetValue()
                  and 'Research note, separate from the source:' in dialog.details.GetValue()
                  and 'Saved root note' in dialog.details.GetValue())
            click(dialog.open_button)
            if draft:
                wait_for(lambda: ready() and wx.Window.FindFocus() is frame.evidence.note,
                         check_draft, 'retained draft focus')
            else:
                wait_for(lambda: evidence_ready() and wx.Window.FindFocus() is frame.evidence.items,
                         opened_evidence, 'selected native evidence focus')

        def opened_evidence(_):
            panel = frame.evidence
            check('comparison selects the existing native evidence notebook page',
                  frame.book.GetPage(frame.book.GetSelection()) is panel)
            check('comparison opens the exact evidence identity and source quotation',
                  panel.displayed.evidence_id == evidence_id and panel.quote.GetValue() == initial_saved.anchor.quote)
            check('actual keyboard focus reaches the saved evidence list', wx.Window.FindFocus() is panel.items)
            state['draft'] = 'Unsaved independent note — зберегти дослівно.'
            panel.note.SetValue(state['draft'])
            compare(draft=True)

        def check_draft(_):
            panel = frame.evidence
            check('comparison drill-down preserves an unsaved note', panel.note.GetValue() == state['draft'])
            check('draft preservation returns actual keyboard focus to note', wx.Window.FindFocus() is panel.note)
            check('opening comparison did not publish the draft',
                  library.evidence(workspace, 'root')[0].note == 'Saved root note')
            button = next(child for child in panel.GetChildren() if isinstance(child, wx.Button)
                          and 'Save note' in child.GetLabel().replace('&', ''))
            click(button)
            wait_for(lambda: ready() and panel.displayed.note == state['draft'],
                     lambda _: begin_quality(), 'saving the retained synthetic draft')

        def begin_quality():
            state['saved_before_metadata'] = library.evidence(workspace, 'root')
            state['child_before_metadata'] = library.evidence(workspace, 'child')
            await_dialog('Catalog quality', drive_quality)
            invoke_academic('Check catalog quality')

        def drive_quality(dialog):
            check('catalog quality displays scoped summary on a native text control',
                  isinstance(dialog.summary, wx.TextCtrl) and '2 source editions' in dialog.summary.GetValue())
            choose(dialog.filter, dialog.category_ids.index('possible_duplicate'))
            check('tentative metadata filter retains both different editions', len(dialog.visible) == 2)
            index = next(index for index, (row, _) in enumerate(dialog.visible)
                         if row.edition_id == 'edition-revised')
            choose(dialog.results, index, wx.EVT_LISTBOX)
            text = dialog.details.GetValue()
            check('repeated titles remain distinguishable by edition IDs and labels',
                  all(value in text for value in ('edition-first', 'edition-revised', 'First', 'Revised', 'tentative')))
            choose(dialog.filter, dialog.category_ids.index('missing_title'))
            check('empty native quality filter disables metadata editing', not dialog.visible and not dialog.edit.IsEnabled())
            choose(dialog.filter, dialog.category_ids.index('possible_duplicate'))
            index = next(index for index, (row, _) in enumerate(dialog.visible)
                         if row.edition_id == 'edition-revised')
            choose(dialog.results, index, wx.EVT_LISTBOX)
            await_dialog('Edit book metadata', drive_metadata)
            click(dialog.edit)

        def drive_metadata(dialog):
            check('quality action opens the existing metadata editor for the exact edition',
                  dialog.fields['edition'].GetValue() == 'Revised' and dialog.fields['volume'].GetValue() == 'II')
            dialog.fields['language'].SetValue('sk')
            click(dialog.save_button)
            wait_for(lambda: ready() and dialog_named('Edit book metadata') is None,
                     metadata_saved, 'native metadata save')

        def metadata_saved(_):
            first = library.edition_metadata(workspace, 'edition-first')
            revised = library.edition_metadata(workspace, 'edition-revised')
            check('quality metadata callback updates only the chosen edition language',
                  first.language == 'uk' and revised.language == 'sk')
            check('quality metadata update preserves historical saved research',
                  library.evidence(workspace, 'root') == state['saved_before_metadata'] and
                  library.evidence(workspace, 'child') == state['child_before_metadata'])
            begin_ris()

        def begin_ris():
            path = root / 'synthetic bibliography.ris'
            path.write_text('TY  - BOOK\nTI  - First unverified record\nAU  - First author\nLA  - en\nER  -\n'
                'TY  - BOOK\nTI  - Reviewed RIS title\nAU  - Žan Novak\nAU  - Олена Коваль\n'
                'LA  - en\nDO  - 10.1234/unverified\nL1  - not-a-real-attachment.pdf\n'
                'N1  - Unsupported RIS note\nER  -\n', encoding='utf-8')
            records = read_ris_file(path)
            editions = library.browse(workspace)
            dialog = bibliography_import.BibliographyReviewDialog(frame, library, workspace,
                records, editions, edition_id='edition-first')
            later(lambda: drive_ris(dialog))
            try:
                result = dialog.ShowModal()
                reviewed = dialog.result
            finally:
                dialog.Destroy()
            if failures:
                return
            check('native RIS confirmation returned a reviewed selection', result == wx.ID_OK and reviewed is not None)
            review, selected = reviewed
            check('RIS native selection carries exact edition and only checked fields',
                  review.metadata.edition_id == 'edition-revised' and set(selected) == {'title', 'authors'})
            frame.run_job('Applying synthetic reviewed RIS metadata',
                lambda: apply_ris_metadata(library, workspace, review, selected,
                                           cancelled=frame.cancel_event.is_set), ris_saved)

        def drive_ris(dialog):
            check('RIS review uses actual native confirmation checkboxes',
                  all(isinstance(control, wx.CheckBox) for control in dialog.checks.values()))
            check('all RIS import checkboxes start unchecked', not any(c.GetValue() for c in dialog.checks.values()))
            button = next(child for child in dialog.GetChildren()
                          if isinstance(child, wx.Button) and child.GetId() == wx.ID_OK)
            click(button)
            check('unchecked RIS apply stays open and reports explicit selection requirement',
                  dialog.IsModal() and dialog.result is None and 'Select at least one' in dialog.status.GetValue())
            tick(dialog.checks['title'], True)
            target = next(i for i, row in enumerate(dialog.editions) if row.edition_id == 'edition-revised')
            choose(dialog.target, target)
            check('changing native RIS target resets every confirmation', not any(c.GetValue() for c in dialog.checks.values()))
            tick(dialog.checks['authors'], True)
            choose(dialog.record, 1)
            check('changing native RIS record resets every confirmation', not any(c.GetValue() for c in dialog.checks.values()))
            check('missing RIS edition and volume cannot overwrite metadata',
                  not dialog.checks['edition'].IsEnabled() and not dialog.checks['volume'].IsEnabled())
            check('RIS native review labels identifiers and unsupported attachments unverified',
                  'unverified' in dialog.details.GetValue().lower() and 'No file is attached or downloaded' in dialog.details.GetValue())
            tick(dialog.checks['title'], True)
            tick(dialog.checks['authors'], True)
            click(button)

        def ris_saved(_):
            first = library.edition_metadata(workspace, 'edition-first')
            revised = library.edition_metadata(workspace, 'edition-revised')
            check('confirmed RIS title and ordered authors are published to the chosen work',
                  revised.title == 'Reviewed RIS title' and revised.authors == ('Žan Novak', 'Олена Коваль'))
            check('unchecked RIS fields and the other edition remain unchanged',
                  (revised.language, revised.edition, revised.volume) == ('sk', 'Revised', 'II')
                  and first.title == 'Repeated title' and first.authors == ('Олена',))
            check('RIS metadata preserves exact historical quotations, citations and notes',
                  library.evidence(workspace, 'root') == state['saved_before_metadata'] and
                  library.evidence(workspace, 'child') == state['child_before_metadata'])
            check('academic workflows preserve every retained original byte', original_assets ==
                  {path.name: sha256(path.read_bytes()).hexdigest() for path in library.assets_dir.iterdir()})
            check('academic worker restores scope controls and menus', ready() and frame.choice.IsEnabled()
                  and frame.book.IsEnabled() and all(frame.menu_bar.IsEnabledTop(i)
                                                   for i in range(frame.menu_bar.GetMenuCount())))
            state['complete'] = True
            frame.Close()

        timeout = wx.CallLater(60000, lambda: fail('Native academic probe exceeded 60 seconds.'))
        later(begin, 100)
        try:
            gui.MainLoop()
        finally:
            timeout.Stop()
            for timer in timers:
                timer.Stop()
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            if frame:
                frame.Destroy()
                gui.ProcessPendingEvents()

    if not failures and not state.get('complete'):
        failures.append('Native academic workflow exited before all checks completed.')
    report = dict(status='FAIL' if failures else 'PASS', platform=platform.system(),
        wx_version=wx.version(), python=sys.version, isolated_imports=bool(sys.flags.isolated),
        check_count=len(checks), checks=checks, errors=failures,
        windows_native='PASS' if not failures and sys.platform == 'win32' else 'NOT_RUN',
        nvda='NOT_RUN', os_file_picker='NOT_EXERCISED', user_corpus='NOT_USED',
        module_sha256={module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
                       for module in (academic_tools, literature_matrix, bibliography_import, catalog_quality)})
    print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps(dict(status='FAIL', error=traceback.format_exc(), nvda='NOT_RUN'),
                         ensure_ascii=False), flush=True)
        raise

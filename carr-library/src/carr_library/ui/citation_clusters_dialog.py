"""Native ordered citation editor using the existing offline CSL workbench."""
from concurrent.futures import ThreadPoolExecutor
import threading

from ..citations.cluster_workbench import CitationClusterWorkbench, CitationDraft
from ..research.export_file import capture_export_destination


def create_citation_clusters_dialog(parent, controller, *, identity=None, style='apa',
                                     locale='en-US', name_mode='literal', executor=None):
    import wx
    from wx.lib.scrolledpanel import ScrolledPanel
    from ..citations.csl import ATTRIBUTION, _LABELS

    class CitationClustersDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Citations in document order', size=(900, 700),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.workbench = CitationClusterWorkbench(controller)
            self.selection = None
            self.draft = None
            self.preview = None
            self.references = ()
            self.pending = None
            self.cancelled = threading.Event()
            self.alive = True
            self.fields_dirty = False
            self.owns_executor = executor is None
            self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='citation-order')
            self.editor_controls = []
            outer = wx.BoxSizer(wx.VERTICAL)
            self.tabs = wx.Notebook(self)
            self.edit_page = ScrolledPanel(self.tabs)
            self.tabs.AddPage(self.edit_page, 'Citation order and locators')
            form = wx.BoxSizer(wx.VERTICAL)
            tip = wx.StaticText(self.edit_page, label=(
                'Add references in document order. A group can contain several references. '
                'Apply field edits before formatting. Locators are entered manually and do not '
                'approve a quotation. Ctrl+Tab changes sections; F6 formats the current order.'))
            tip.Wrap(800)
            form.Add(tip, 0, wx.EXPAND | wx.ALL, 8)

            def labelled(label, control, *, expand=0):
                control.SetName(label.replace('&', ''))
                form.Add(wx.StaticText(self.edit_page, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
                form.Add(control, expand, wx.EXPAND | wx.ALL, 6)
                self.editor_controls.append(control)
                return control

            self.groups = labelled('Citations in document &order', wx.ListBox(self.edit_page))
            self.groups.SetMinSize((-1, 105))
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.up = wx.Button(self.edit_page, label='Move &up')
            self.down = wx.Button(self.edit_page, label='Move &down')
            self.remove_group = wx.Button(self.edit_page, label='Remove citation &group')
            for button in (self.up, self.down, self.remove_group):
                self.editor_controls.append(button)
                row.Add(button, 0, wx.ALL, 4)
            form.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
            self.items = labelled('References in the selected citation', wx.ListBox(self.edit_page))
            self.items.SetMinSize((-1, 80))
            self.reference = labelled('Reference for the citation &item', wx.Choice(self.edit_page))
            grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=8)
            grid.AddGrowableCol(1)
            self.locator_labels = tuple(sorted(_LABELS))
            self.locator_kind = wx.Choice(self.edit_page, choices=[value.replace('-', ' ').title() for value in self.locator_labels])
            self.locator_kind.SetSelection(self.locator_labels.index('page'))
            self.locator = wx.TextCtrl(self.edit_page)
            self.prefix = wx.TextCtrl(self.edit_page)
            self.suffix = wx.TextCtrl(self.edit_page)
            self.note = wx.TextCtrl(self.edit_page)
            for label, control in (
                ('Locator type', self.locator_kind), ('Locator; blank for a general reference', self.locator),
                ('Prefix; optional', self.prefix), ('Suffix; optional', self.suffix),
                ('Note number; blank uses document order', self.note),
            ):
                control.SetName(label)
                grid.Add(wx.StaticText(self.edit_page, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(control, 1, wx.EXPAND)
                self.editor_controls.append(control)
            form.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
            self.suppress = wx.CheckBox(self.edit_page, label='&Suppress author in this citation item')
            self.suppress.SetName('Suppress author in this citation item')
            self.editor_controls.append(self.suppress)
            form.Add(self.suppress, 0, wx.ALL, 8)
            actions = wx.WrapSizer(wx.HORIZONTAL)
            self.new_group = wx.Button(self.edit_page, label='Add &new citation')
            self.add_item = wx.Button(self.edit_page, label='Add to selected &citation')
            self.apply_item = wx.Button(self.edit_page, label='&Apply item and note edits')
            self.remove_item = wx.Button(self.edit_page, label='Remove selected &item')
            self.reload_item = wx.Button(self.edit_page, label='Reload selected item')
            for button in (self.new_group, self.add_item, self.apply_item, self.remove_item, self.reload_item):
                self.editor_controls.append(button)
                actions.Add(button, 0, wx.ALL, 4)
            form.Add(actions, 0, wx.EXPAND | wx.ALL, 4)
            self.load_order = wx.Button(self.edit_page, label='Load citation order from a saved Word document...')
            self.editor_controls.append(self.load_order)
            form.Add(self.load_order, 0, wx.EXPAND | wx.ALL, 8)
            self.edit_page.SetSizer(form)
            self.edit_page.SetupScrolling(scroll_x=False)
            preview_page = wx.Panel(self.tabs)
            self.tabs.AddPage(preview_page, 'Preview and export')
            preview_layout = wx.BoxSizer(wx.VERTICAL)
            self.output = wx.TextCtrl(preview_page, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.output.SetName('Ordered citations, bibliography and review notes')
            preview_layout.Add(self.output, 1, wx.EXPAND | wx.ALL, 8)
            attribution = wx.StaticText(preview_page, label=ATTRIBUTION)
            attribution.Wrap(800)
            preview_layout.Add(attribution, 0, wx.EXPAND | wx.ALL, 8)
            preview_page.SetSizer(preview_layout)
            outer.Add(self.tabs, 1, wx.EXPAND | wx.ALL, 6)
            footer = wx.WrapSizer(wx.HORIZONTAL)
            self.generate = wx.Button(self, label='&Format citation order (F6)')
            self.copy = wx.Button(self, label='&Copy ordered citations')
            self.save = wx.Button(self, label='Save &Word citation document...')
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            for button in (self.generate, self.copy, self.save, self.close_button):
                footer.Add(button, 0, wx.ALL, 4)
            outer.Add(footer, 0, wx.EXPAND | wx.ALL, 4)
            self.status = wx.TextCtrl(self, value='Loading the selected local references.',
                                      style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Citation order status')
            self.status.SetMinSize((-1, 55))
            outer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            self.SetSizer(outer)
            self.SetMinSize((650, 480))
            self.SetEscapeId(wx.ID_CANCEL)
            self.groups.Bind(wx.EVT_LISTBOX, self.on_group)
            self.items.Bind(wx.EVT_LISTBOX, self.on_item)
            self.new_group.Bind(wx.EVT_BUTTON, lambda event: self.on_add(False))
            self.add_item.Bind(wx.EVT_BUTTON, lambda event: self.on_add(True))
            self.apply_item.Bind(wx.EVT_BUTTON, self.on_apply)
            self.remove_group.Bind(wx.EVT_BUTTON, lambda event: self.on_remove(False))
            self.remove_item.Bind(wx.EVT_BUTTON, lambda event: self.on_remove(True))
            self.reload_item.Bind(wx.EVT_BUTTON, self.on_item)
            self.up.Bind(wx.EVT_BUTTON, lambda event: self.on_move(-1))
            self.down.Bind(wx.EVT_BUTTON, lambda event: self.on_move(1))
            self.generate.Bind(wx.EVT_BUTTON, self.on_preview)
            self.copy.Bind(wx.EVT_BUTTON, self.on_copy)
            self.save.Bind(wx.EVT_BUTTON, self.on_save)
            self.load_order.Bind(wx.EVT_BUTTON, self.on_load)
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            for control in (self.reference, self.locator_kind):
                control.Bind(wx.EVT_CHOICE, self.on_field)
            for control in (self.locator, self.prefix, self.suffix, self.note):
                control.Bind(wx.EVT_TEXT, self.on_field)
            self.suppress.Bind(wx.EVT_CHECKBOX, self.on_field)
            command = int(wx.NewIdRef())
            self.Bind(wx.EVT_MENU, self.on_preview, id=command)
            self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_NORMAL, wx.WXK_F6, command)]))
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.controls()
            wx.CallAfter(self.start_loading)

        def report(self, message, *, focus=False):
            self.status.ChangeValue(message)
            self.status.SetInsertionPoint(0)
            if focus:
                self.status.SetFocus()

        def controls(self):
            ready = self.selection is not None and self.pending is None
            for control in self.editor_controls:
                control.Enable(ready)
            groups = self.draft.clusters if self.draft is not None else []
            group = self.groups.GetSelection()
            selected = ready and 0 <= group < len(groups)
            for control in (self.add_item, self.apply_item, self.remove_item, self.remove_group, self.reload_item):
                control.Enable(selected)
            self.up.Enable(selected and group > 0)
            self.down.Enable(selected and group + 1 < len(groups))
            self.generate.Enable(ready and bool(groups) and not self.fields_dirty)
            self.copy.Enable(ready and self.preview is not None and not self.fields_dirty)
            self.save.Enable(ready and self.preview is not None and not self.fields_dirty)

        def run(self, operation, receive, message):
            if self.pending is not None or not self.alive:
                return
            token = threading.Event()
            self.cancelled = token
            self.report(message)
            try:
                future = self.executor.submit(operation, token.is_set)
                self.pending = future
                self.controls()
                future.add_done_callback(lambda completed: wx.CallAfter(self.ready, completed, receive, token))
            except Exception as exc:
                self.pending = None
                self.controls()
                self.report('Operation not completed: ' + str(exc), focus=True)

        def ready(self, future, receive, token):
            if not self.alive or token.is_set() or self.pending is not future:
                return
            self.pending = None
            try:
                value = future.result()
                receive(value)
            except Exception as exc:
                self.report('Operation not completed: ' + str(exc), focus=True)
            self.controls()

        def start_loading(self):
            self.run(lambda cancelled: self.workbench.selection(identity=identity,
                name_mode=name_mode, cancelled=cancelled), self.loaded, 'Loading the selected local references.')

        def loaded(self, selection):
            self.selection = selection
            self.references = tuple(selection.records)
            self.reference.Set([f'{number}. {row.get("title", "Untitled")[:180]} [{row["id"][:8]}]'
                                for number, row in enumerate(self.references, 1)])
            self.reference.SetSelection(0)
            self.draft = CitationDraft(selection)
            self.fields_dirty = False
            self.report('Choose a reference and add citations in document order. '
                        'Locators remain manual, unverified presentation data.')
            self.reference.SetFocus()

        def invalidate(self):
            self.preview = None
            self.output.ChangeValue('')
            self.controls()

        def on_field(self, event=None):
            if self.selection is not None:
                self.fields_dirty = True
                self.invalidate()
                self.report('Apply the item edits or add a citation before formatting.')
            if event is not None:
                event.Skip()

        def group_index(self):
            index = self.groups.GetSelection()
            if self.draft is None or not 0 <= index < len(self.draft.clusters):
                raise ValueError('Choose an existing citation group.')
            return index

        def item_value(self):
            index = self.reference.GetSelection()
            if not 0 <= index < len(self.references):
                raise ValueError('Choose a reference.')
            item = {'id': self.references[index]['id']}
            locator = self.locator.GetValue().strip()
            if locator:
                kind = self.locator_kind.GetSelection()
                if not 0 <= kind < len(self.locator_labels):
                    raise ValueError('Choose a locator type.')
                item.update(locator=locator, label=self.locator_labels[kind])
            for name, control in (('prefix', self.prefix), ('suffix', self.suffix)):
                if control.GetValue():
                    item[name] = control.GetValue()
            if self.suppress.GetValue():
                item['suppress-author'] = True
            raw_note = self.note.GetValue().strip()
            if raw_note and (not raw_note.isascii() or not raw_note.isdecimal()):
                raise ValueError('Enter a positive note number or leave it blank for document order.')
            note = int(raw_note) if raw_note else None
            return item, note

        def refresh(self, selected=0, item=0):
            groups = self.draft.clusters
            titles = {row['id']: row.get('title', 'Untitled') for row in self.references}
            labels = []
            for number, group in enumerate(groups, 1):
                names = '; '.join(titles[value['id']][:100] for value in group['items'])
                note = f'; note {group["note_index"]}' if group.get('note_index') else '; automatic note order'
                labels.append(f'{number}. {names}{note}')
            self.groups.Set(labels)
            self.items.Set([])
            if groups:
                self.groups.SetSelection(min(max(0, selected), len(groups) - 1))
                self.on_group(item_index=item)
            else:
                self.fields_dirty = False
            self.controls()

        def on_group(self, event=None, *, item_index=0):
            if self.pending is not None or self.draft is None:
                return
            groups = self.draft.clusters
            index = self.groups.GetSelection()
            if not 0 <= index < len(groups):
                return
            titles = {row['id']: row.get('title', 'Untitled') for row in self.references}
            items = groups[index]['items']
            self.items.Set([titles[value['id']][:150] +
                (f'; {value.get("label", "page")}: {value["locator"]}' if value.get('locator') else '; general reference')
                for value in items])
            self.items.SetSelection(min(max(0, item_index), len(items) - 1))
            self.on_item()

        def on_item(self, event=None):
            if self.pending is not None or self.draft is None:
                return
            try:
                group = self.draft.clusters[self.group_index()]
                index = self.items.GetSelection()
                if not 0 <= index < len(group['items']):
                    return
                item = group['items'][index]
                self.reference.SetSelection(next(i for i, row in enumerate(self.references) if row['id'] == item['id']))
                self.locator.ChangeValue(item.get('locator', ''))
                self.locator_kind.SetSelection(self.locator_labels.index(item.get('label', 'page')))
                self.prefix.ChangeValue(item.get('prefix', ''))
                self.suffix.ChangeValue(item.get('suffix', ''))
                self.suppress.SetValue(item.get('suppress-author', False))
                self.note.ChangeValue(str(group['note_index']) if group.get('note_index') else '')
                self.fields_dirty = False
                self.controls()
            except (ValueError, IndexError) as exc:
                self.report(str(exc), focus=True)

        def edit(self, operation):
            if self.pending is not None or self.draft is None:
                return
            try:
                group, item = operation()
                self.fields_dirty = False
                self.invalidate()
                self.refresh(group, item)
                self.report('Citation order updated. Format again before copying or saving.')
                self.groups.SetFocus()
            except (ValueError, TypeError, IndexError) as exc:
                self.report('Citation not changed: ' + str(exc), focus=True)

        def on_add(self, append):
            def operation():
                item, note = self.item_value()
                group = self.group_index() if append else None
                chosen = self.draft.add(item, group=group, note_index=note)
                return chosen, len(self.draft.clusters[chosen]['items']) - 1
            self.edit(operation)

        def on_apply(self, event=None):
            def operation():
                item, note = self.item_value()
                group, index = self.group_index(), self.items.GetSelection()
                self.draft.replace_item(group, index, item, note_index=note)
                return group, index
            self.edit(operation)

        def on_remove(self, item_only):
            def operation():
                group, item = self.group_index(), self.items.GetSelection()
                if item_only:
                    self.draft.remove_item(group, item)
                else:
                    self.draft.remove_group(group)
                return group, max(0, item - 1)
            self.edit(operation)

        def on_move(self, step):
            self.edit(lambda: (self.draft.move_group(self.group_index(), step), 0))

        def on_preview(self, event=None):
            if self.pending is not None or self.draft is None:
                return
            if self.fields_dirty:
                self.report('Apply the item edits or add the citation before formatting.', focus=True)
                return
            captured = self.draft.captured
            self.preview = None
            self.output.ChangeValue('')
            def complete(preview):
                self.workbench.check(preview)
                self.preview = preview
                self.output.ChangeValue(preview.explanation)
                self.output.SetInsertionPoint(0)
                self.tabs.SetSelection(1)
                self.report(f'Formatted {len(preview.result.citations)} ordered citations. '
                            'Review manual locators and formatting before use.')
                self.output.SetFocus()
            self.run(lambda cancelled: self.workbench.preview(self.selection, captured,
                style=style, locale=locale, cancelled=cancelled), complete,
                'Formatting the full citation order with the existing offline processor.')

        def on_copy(self, event=None):
            if self.pending is not None or self.fields_dirty:
                return
            try:
                content = self.workbench.copy_text(self.preview)
                if not wx.TheClipboard.Open():
                    raise ValueError('The clipboard is busy.')
                try:
                    if not wx.TheClipboard.SetData(wx.TextDataObject(content)):
                        raise ValueError('The formatted citations could not be copied.')
                finally:
                    wx.TheClipboard.Close()
                self.report('Ordered citations copied.', focus=True)
            except Exception as exc:
                self.report('Citations not copied: ' + str(exc), focus=True)

        def on_save(self, event=None):
            if self.pending is not None or self.fields_dirty:
                return
            try:
                self.workbench.check(self.preview)
                chooser = wx.FileDialog(self, 'Save a new Word citation document',
                    defaultFile='ordered-citations.docx', wildcard='Word document (*.docx)|*.docx',
                    style=wx.FD_SAVE)
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    path = chooser.GetPath()
                finally:
                    chooser.Destroy()
                approval = capture_export_destination(path, private_root=controller.library.root)
                if not approval.require_absent:
                    raise FileExistsError('Choose a new filename; the existing file is kept.')
                preview = self.preview
                self.run(lambda cancelled: self.workbench.export_document(preview, path,
                    cancelled=cancelled, approval=approval),
                    lambda _value: self.report('Citation document saved: ' + path, focus=True),
                    'Saving the ordered citation snapshot to a new Word document.')
            except Exception as exc:
                self.report('Document not saved: ' + str(exc), focus=True)

        def on_load(self, event=None):
            if self.pending is not None or self.selection is None:
                return
            try:
                chooser = wx.FileDialog(self, 'Load citation order from a saved Word document',
                    wildcard='Word document (*.docx)|*.docx', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    path = chooser.GetPath()
                finally:
                    chooser.Destroy()
                def loaded(draft):
                    self.draft = draft
                    self.fields_dirty = False
                    self.invalidate()
                    self.refresh()
                    self.report('Saved citation order loaded. Visible Word edits are not imported. '
                                'Review the locators and format with the selected style.')
                    self.groups.SetFocus()
                self.run(lambda cancelled: self.workbench.load_document_order(self.selection, path,
                    cancelled=cancelled), loaded, 'Checking the saved reference snapshot and citation order.')
            except Exception as exc:
                self.report('Citation order not loaded: ' + str(exc), focus=True)

        def on_close(self, event=None):
            self.cancelled.set()
            if self.pending is not None:
                self.pending.cancel()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.SetReturnCode(wx.ID_CANCEL)
                self.Hide()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
                self.cancelled.set()
                if self.pending is not None:
                    self.pending.cancel()
                if self.owns_executor:
                    self.executor.shutdown(wait=False, cancel_futures=True)
            event.Skip()

    return CitationClustersDialog()

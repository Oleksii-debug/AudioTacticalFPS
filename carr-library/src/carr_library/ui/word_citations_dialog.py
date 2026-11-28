"""Explicit finite Word review on the CSL workbench's existing serial worker.

Native Word is optional. This window never creates its own executor or opens,
saves or closes an Office document.
"""
from __future__ import annotations

import threading
from pathlib import Path

from ..durable_state import local_path, regular
from ..academic_integration.word_bridge import WordBridge, WordDecision
from ..academic_integration.word_records import canonical
from ..citations.bibliography_workbench import records_for_style


def create_word_citation_dialog(parent, workbench, preview, *, executor, guard):
    import wx

    library = workbench.controller.library
    workspace = workbench.controller.workspace
    root = local_path(library.root, exists=True)
    root_info = regular(root, directory=True)
    root_identity = root_info.st_dev, root_info.st_ino
    guard()
    snapshot = workbench.check(preview)
    references, _ = records_for_style(snapshot, name_mode=preview.name_mode, preserve_literal_text=True)
    reference_ids = [row['id'] for row in references]
    labels = [str(row.get('title') or row['id'])[:180] + ' [' + row['id'] + ']' for row in references]

    class WordCitationDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Already-open Word document', size=(900, 830),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive = True
            self.pending = None
            self.decision = None
            self.view = None
            self.bridge = None
            self.generation = 0
            self.cancelled = threading.Event()
            self.target_ids = []
            self.capture = None
            layout = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'Keep the intended Word document active. Inspect is read-only. Preview holds one finite '
                'Word session for at most 60 seconds of approval. Apply needs your explicit confirmation. '
                'Only Resonance-managed citation ranges change; foreign fields and legacy metadata remain. '
                'The bridge never opens, saves or closes a Word document.'))
            notice.Wrap(840)
            layout.Add(notice, 0, wx.EXPAND | wx.ALL, 8)
            self.inspect = wx.Button(self, label='I&nspect active Word document')
            self.inspect.SetName('Inspect already-open Word document')
            self.inspect.Bind(wx.EVT_BUTTON, self.on_inspect)
            layout.Add(self.inspect, 0, wx.ALL, 8)
            grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=8)
            grid.AddGrowableCol(1)
            self.action = wx.Choice(self, choices=[
                'Insert a citation at the current collapsed main-text position',
                'Edit an existing Resonance citation',
                'Insert or regenerate the Resonance bibliography'])
            self.action.SetSelection(0)
            self.targets = wx.Choice(self, choices=[])
            self.references = wx.CheckListBox(self, choices=labels)
            self.references.SetMinSize((-1, 100))
            self.locator = wx.TextCtrl(self)
            self.locator_label = wx.Choice(self, choices=['page', 'chapter', 'section', 'paragraph', 'volume'])
            self.locator_label.SetSelection(0)
            self.verified = wx.CheckBox(self, label='I checked this pinpoint against the source')
            self.prefix = wx.TextCtrl(self)
            self.suffix = wx.TextCtrl(self)
            self.suppress_author = wx.CheckBox(self, label='Suppress author for the selected citation items')
            for label, control in (
                ('Word operation', self.action), ('Existing managed citation', self.targets),
                ('References to cite', self.references), ('Verified pinpoint; one reference only', self.locator),
                ('Pinpoint kind', self.locator_label), ('Pinpoint evidence', self.verified),
                ('Citation prefix; one reference only', self.prefix), ('Citation suffix; one reference only', self.suffix),
                ('Citation author presentation', self.suppress_author),
            ):
                control.SetName(label)
                grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(control, 1, wx.EXPAND)
            layout.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.preview_button = wx.Button(self, label='&Preview Word change')
            self.apply_button = wx.Button(self, label='&Apply reviewed change')
            self.stop_button = wx.Button(self, label='&Stop')
            for control in (self.preview_button, self.apply_button, self.stop_button):
                buttons.Add(control, 0, wx.ALL, 4)
            self.preview_button.SetName('Preview finite Word citation change')
            self.apply_button.SetName('Apply current Word preview')
            self.stop_button.SetName('Stop owned Word operation')
            self.preview_button.Bind(wx.EVT_BUTTON, self.on_preview)
            self.apply_button.Bind(wx.EVT_BUTTON, self.on_apply)
            self.stop_button.Bind(wx.EVT_BUTTON, self.on_stop)
            layout.Add(buttons, 0, wx.ALL, 4)
            self.output = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.output.SetName('Read-only Word inspection and change preview')
            layout.Add(self.output, 1, wx.EXPAND | wx.ALL, 8)
            self.references_output = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.references_output.SetName('Read-only managed Word document references')
            self.references_output.SetMinSize((-1, 75))
            layout.Add(self.references_output, 0, wx.EXPAND | wx.ALL, 8)
            self.copy_button = wx.Button(self, label='Copy &document references as CSL JSON')
            self.copy_button.Bind(wx.EVT_BUTTON, self.on_copy_references)
            layout.Add(self.copy_button, 0, wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Word citation operation status')
            self.status.SetMinSize((-1, 65))
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetMinSize((740, 680))
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.options = (self.action, self.targets, self.references, self.locator, self.locator_label,
                            self.verified, self.prefix, self.suffix, self.suppress_author)
            self.controls(False)
            self.report('Inspect the intended open Word document, or choose an explicit change to preview.', focus=False)
            self.inspect.SetFocus()

        def check_current(self):
            if not self.alive:
                raise ValueError('The Word review window is closed.')
            guard()
            if (workbench.controller.library is not library or workbench.controller.workspace != workspace
                    or Path(library.root).absolute() != root):
                raise ValueError('The source library changed; reopen the Word review.')
            info = regular(local_path(root, exists=True), directory=True)
            if (info.st_dev, info.st_ino) != root_identity:
                raise ValueError('The selected source directory was replaced; reopen the Word review.')

        def usable(self):
            try:
                self.check_current()
                return True
            except Exception:
                if self.decision is not None:
                    self.decision.close()
                self.cancelled.set()
                return False

        def report(self, message, *, focus=True):
            if not self.alive:
                return
            self.status.SetValue(message)
            self.status.SetInsertionPoint(0)
            if focus:
                self.status.SetFocus()

        def controls(self, busy):
            for control in (*self.options, self.inspect, self.preview_button):
                control.Enable(not busy)
            self.apply_button.Enable(busy and self.view is not None and not self.cancelled.is_set())
            self.stop_button.Enable(busy)
            self.copy_button.Enable(not busy and bool(self.references_output.GetValue()))

        def intent(self):
            index = self.action.GetSelection()
            if index not in (0, 1, 2):
                raise ValueError('Choose a supported Word operation.')
            action = ('insert_citation', 'edit_citation', 'regenerate_bibliography')[index]
            target = None
            if index == 1:
                choice = self.targets.GetSelection()
                if not 0 <= choice < len(self.target_ids):
                    raise ValueError('Inspect Word and select the managed citation to edit.')
                target = self.target_ids[choice]
            items = []
            if index != 2:
                chosen = tuple(self.references.GetCheckedItems())
                if not chosen or any(type(i) is not int or not 0 <= i < len(reference_ids) for i in chosen):
                    raise ValueError('Select at least one current reference.')
                locator = self.locator.GetValue().strip() or None
                prefix, suffix = self.prefix.GetValue(), self.suffix.GetValue()
                if len(chosen) != 1 and (locator is not None or prefix or suffix):
                    raise ValueError('Apply a pinpoint, prefix or suffix to one selected reference at a time.')
                label_index = self.locator_label.GetSelection()
                labels_by_index = ('page', 'chapter', 'section', 'paragraph', 'volume')
                if label_index not in range(len(labels_by_index)):
                    raise ValueError('Choose a pinpoint kind.')
                for i in chosen:
                    items.append({'reference_id': reference_ids[i], 'locator': locator,
                        'label': labels_by_index[label_index], 'locator_verified': bool(self.verified.GetValue()),
                        'prefix': prefix, 'suffix': suffix, 'suppress_author': bool(self.suppress_author.GetValue())})
            return {'action': action, 'citation_id': target, 'items': items}

        def begin(self, intent):
            if self.pending is not None:
                return
            try:
                self.check_current()
                self.cancelled = threading.Event()
                self.decision = WordDecision() if intent is not None else None
                self.view = None
                self.generation += 1
                generation, decision, cancelled = self.generation, self.decision, self.cancelled
                bridge = WordBridge(workbench, preview, guard=self.check_current)
                self.bridge = bridge
                self.controls(True)
                self.report('Inspecting the held active Word document on the existing worker.', focus=False)

                def offer(view):
                    try:
                        wx.CallAfter(self.on_preview_ready, generation, decision, view)
                    except BaseException:
                        decision.close()
                        raise

                def run():
                    self.check_current()
                    return bridge.run(intent, decision=decision,
                        on_preview=offer if intent is not None else None, cancelled=cancelled.is_set)

                future = executor.submit(run)
                self.pending = future
                future.add_done_callback(lambda done: wx.CallAfter(self.on_ready, generation, decision, done))
            except Exception as error:
                if self.decision is not None:
                    self.decision.close()
                self.pending = None
                if self.usable():
                    self.controls(False)
                    self.report('Word operation did not start: ' + str(error))

        def on_inspect(self, event=None):
            self.begin(None)

        def on_preview(self, event=None):
            if self.pending is not None:
                return
            try:
                self.check_current()
                self.begin(self.intent())
            except Exception as error:
                if self.usable():
                    self.report('Word change was not queued: ' + str(error))

        def on_preview_ready(self, generation, decision, view):
            if (not self.usable() or self.pending is None or generation != self.generation
                    or self.decision is not decision or decision.closed):
                decision.close()
                return
            self.view = view
            self.output.SetValue(view.explanation)
            self.output.SetInsertionPoint(0)
            self.references_output.SetValue(view.references_json)
            self.controls(True)
            self.report('Review this exact preview. Apply expires after 60 seconds; there is no automatic approval.', focus=False)
            self.output.SetFocus()

        def on_apply(self, event=None):
            decision, view, generation = self.decision, self.view, self.generation
            if self.pending is None or decision is None or view is None or self.cancelled.is_set():
                return
            try:
                self.check_current()
                question = wx.MessageDialog(self,
                    'Apply the displayed change to the already-open Word document?\n'
                    + view.document_name + '\nThe bridge does not request Save or Close; Word\'s own AutoSave may still apply. '
                    'No automatic retry follows an uncertain outcome.',
                    'Apply reviewed Word change', style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                try:
                    answer = question.ShowModal()
                finally:
                    question.Destroy()
                self.check_current()
                if (self.pending is None or self.decision is not decision or self.view is not view
                        or self.generation != generation or decision.closed or self.cancelled.is_set()):
                    raise ValueError('The Word preview expired or changed; inspect again.')
                if answer != wx.ID_YES:
                    return
                decision.approve(view.token)
                self.apply_button.Enable(False)
                self.report('Approved. The worker will recheck source and Word before the first change.', focus=False)
            except Exception as error:
                if self.usable():
                    self.report('Word approval was not granted: ' + str(error))

        def on_ready(self, generation, decision, future):
            if self.pending is not future or generation != self.generation:
                return
            self.pending = None
            if decision is not None:
                decision.close()
            self.view = None
            if not self.usable():
                return
            try:
                outcome = future.result()
                if outcome.status == 'INSPECTED':
                    self.capture = outcome.capture
                    metadata = outcome.capture['metadata']
                    clusters = metadata['interchange']['clusters'] if metadata is not None else []
                    rendered = metadata['interchange']['citations'] if metadata is not None else []
                    texts = {row['cluster_id']: row['text'] for row in rendered}
                    self.target_ids = [row['id'] for row in clusters]
                    self.targets.Clear()
                    for identifier in self.target_ids:
                        self.targets.Append(identifier + ': ' + texts[identifier][:120])
                    if self.target_ids:
                        self.targets.SetSelection(0)
                    refs = metadata['interchange']['references'] if metadata is not None else []
                    self.references_output.SetValue(canonical(refs).decode('ascii'))
                    self.output.SetValue('Document: ' + outcome.document_name + '\n'
                        + str(len(clusters)) + ' managed citations. '
                        + str(len(outcome.capture['legacy_parts'])) + ' legacy metadata parts preserved.\n'
                        'Inspection grants no future edit authority. Each change needs a fresh held-session preview.')
                if outcome.status in ('COMPLETED', 'UNCERTAIN'):
                    self.capture = None
                    self.target_ids = []
                    self.targets.Clear()
                self.report(outcome.message + ('\n' + '\n'.join(outcome.warnings) if outcome.warnings else ''))
            except Exception as error:
                self.report('No confirmed Word change was returned: ' + str(error)
                    + '\nIf Word was interrupted externally, inspect its document before retrying.')
            self.controls(False)

        def on_copy_references(self, event=None):
            if self.pending is not None:
                return
            try:
                self.check_current()
                content = self.references_output.GetValue()
                if not content:
                    raise ValueError('Inspect the managed Word references first.')
                if not wx.TheClipboard.Open():
                    raise ValueError('The clipboard is busy.')
                try:
                    self.check_current()
                    if not wx.TheClipboard.SetData(wx.TextDataObject(content)):
                        raise ValueError('The document references could not be copied.')
                finally:
                    wx.TheClipboard.Close()
                self.report('Managed document references copied as inert CSL JSON; no source or edit authority is transferred.')
            except Exception as error:
                if self.usable():
                    self.report('References were not copied: ' + str(error))

        def on_stop(self, event=None):
            if not self.alive:
                return
            self.cancelled.set()
            if self.decision is not None:
                self.decision.close()
            if self.pending is not None:
                self.pending.cancel()
            self.apply_button.Enable(False)
            self.report('Stop requested. Waiting for owned-process cleanup and the actual Word outcome.', focus=False)

        def on_close(self, event=None):
            if not self.alive:
                return
            if self.pending is not None:
                self.on_stop()
                return
            self.cancelled.set()
            if self.decision is not None:
                self.decision.close()
            self.alive = False
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.SetReturnCode(wx.ID_CANCEL)
                self.Hide()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
                self.cancelled.set()
                if self.decision is not None:
                    self.decision.close()
                if self.pending is not None:
                    self.pending.cancel()
            event.Skip()

    return WordCitationDialog()

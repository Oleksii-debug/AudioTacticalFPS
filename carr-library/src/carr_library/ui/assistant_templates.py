"""Native workflow settings using the assistant's existing worker and executor."""
import json
from pathlib import Path
import threading

from ..assistant.templates import combine_instructions
from ..research.export_file import write_export, export_completion
from .export_destination import approve_export_destination


def create_templates_dialog(parent, service, *, executor, is_current,
                            current_plan=None, current_job_id=None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    store = service.workflow_templates()
    library, root = service.app, service.app.root
    dialog = wx.Dialog(parent, title='Saved workflow templates', size=(850, 680),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    notice = wx.StaticText(dialog, label=(
        'Select templates in the order shown to load one finite batch. '
        'Loading opens an unapproved draft. Choose current permissions, Preview, then Run. '
        'Templates can contain your literal paths and text; review an export before sharing it.'))
    notice.Wrap(800)
    layout.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
    rows_control = wx.ListBox(dialog, style=wx.LB_EXTENDED)
    rows_control.SetName('Saved workflow templates; selected rows form a batch in list order')
    layout.Add(rows_control, 1, wx.ALL | wx.EXPAND, 6)
    status = wx.TextCtrl(dialog, value='Reading saved workflow settings.',
                        style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Workflow template status and review')
    layout.Add(status, 1, wx.ALL | wx.EXPAND, 6)
    buttons, rows = {}, ()
    alive, busy = True, False
    stop = threading.Event()
    dialog.template_result = None
    labels = [('load', '&Load selected as draft'), ('save', '&Save plan as new'),
              ('update', '&Update selected from plan'), ('rename', '&Rename selected'),
              ('remove', 'Remove selected'), ('import', '&Import template'),
              ('export', 'E&xport selected'), ('refresh', 'Re&fresh'), ('close', '&Close')]
    for start in (0, 3, 6):
        line = wx.BoxSizer(wx.HORIZONTAL)
        for identity, label in labels[start:start + 3]:
            button = wx.Button(dialog, label=label)
            button.SetName(label.replace('&', ''))
            buttons[identity] = button
            line.Add(button, 0, wx.ALL, 4)
        layout.Add(line)
    dialog.SetName('Assistant workflow templates')
    dialog.SetSizer(layout)

    def current():
        return (alive and not stop.is_set() and service.app is library
                and service.app.root == root and is_current())

    def guard():
        if not current():
            raise InterruptedError('The assistant or its library changed. Reopen the workflow templates.')

    def cancelled():
        return not current()

    def report(message):
        if current():
            status.ChangeValue(str(message))

    def enabled():
        if not current():
            return
        rows_control.Enable(not busy)
        for identity, button in buttons.items():
            button.Enable(identity == 'close' or not busy)
        if current_plan is None and current_job_id is None:
            buttons['save'].Disable()
            buttons['update'].Disable()

    def selected(*, single=False):
        guard()
        indexes = tuple(sorted(rows_control.GetSelections()))
        if (not indexes or any(not 0 <= index < len(rows) for index in indexes)
                or single and len(indexes) != 1):
            raise ValueError('Select exactly one saved workflow.' if single else
                             'Select one or more saved workflows.')
        return tuple(rows[index] for index in indexes)

    def start(work, apply, message):
        nonlocal busy
        guard()
        if busy:
            return
        busy = True
        enabled()
        report(message)
        admission = threading.Event()
        admitted = [False]
        def owned_work():
            # ThreadPoolExecutor can enqueue before Thread.start raises.
            # A later worker must never run a mutation whose submission failed.
            admission.wait()
            if not admitted[0]:
                raise InterruptedError('Workflow worker admission failed.')
            guard()
            return work()
        try:
            future = executor.submit(owned_work)
            admitted[0] = True
        except Exception:
            busy = False
            enabled()
            raise
        finally:
            admission.set()
        def finish(done):
            try:
                value, error = done.result(), None
            except Exception as exc:
                value, error = None, exc
            def deliver():
                nonlocal busy
                busy = False
                if not current():
                    return
                enabled()
                if error is not None:
                    report(error)
                    return
                try:
                    apply(value)
                except Exception as exc:
                    report(exc)
            wx.CallAfter(deliver)
        future.add_done_callback(finish)

    def refresh(event=None, *, message=''):
        def apply(values):
            nonlocal rows
            rows = tuple(values)
            rows_control.Set([f'{value.name}; revision {value.revision}; '
                              f'{len(value.steps)} steps' for value in rows])
            report((message + '\n' if message else '') +
                   f'{len(rows)} saved workflows. Loading does not execute or approve a plan.')
        start(lambda: store.list(cancelled=cancelled), apply,
              (message + '\n' if message else '') + 'Reading saved workflows.')

    def close(event=None, *, result=None):
        nonlocal alive
        if not alive:
            return
        dialog.template_result = result
        alive = False
        stop.set()
        if dialog.IsModal():
            dialog.EndModal(wx.ID_OK if result is not None else wx.ID_CLOSE)
        else:
            dialog.Hide()
        # This dialog borrows the parent's executor. It never shuts it down.

    def load(event=None):
        chosen = selected()
        def work():
            values = []
            for previous in chosen:
                guard()
                value = store.get(previous.id)
                if value.fingerprint != previous.fingerprint:
                    raise ValueError('A selected workflow changed. Refresh and review it again.')
                values.append(value)
            return combine_instructions(values)
        start(work, lambda draft: close(result=draft),
              'Preparing an unapproved batch from the selected workflow settings.')

    def name_dialog(value, title):
        picker = wx.TextEntryDialog(dialog, 'Workflow name (1 to 120 characters)', title, value)
        try:
            result = picker.ShowModal()
            name = picker.GetValue() if result == wx.ID_OK else None
        finally:
            picker.Destroy()
        guard()
        return name

    def save(event=None, *, updating=False):
        guard()
        if current_plan is None and current_job_id is None:
            raise ValueError('Preview or select a saved plan before saving it as a workflow.')
        previous = selected(single=True)[0] if updating else None
        name = name_dialog(previous.name if previous else '', 'Save workflow template')
        if name is None:
            return
        def work():
            draft = current_plan if current_plan is not None else json.loads(
                service.review_instruction(current_job_id))
            guard()
            return store.save(name, draft, previous=previous, cancelled=cancelled)
        start(work,
              lambda value: refresh(message=f'Saved {value.name}, revision {value.revision}.'),
              'Saving the reviewed steps without their scope or execution approval.')

    def rename(event=None):
        previous = selected(single=True)[0]
        name = name_dialog(previous.name, 'Rename workflow template')
        if name is None:
            return
        start(lambda: store.save(name, previous.draft, previous=previous, cancelled=cancelled),
              lambda value: refresh(message=f'Renamed workflow to {value.name}.'),
              'Saving the workflow name.')

    def remove(event=None):
        previous = selected(single=True)[0]
        accepted = wx.MessageBox(
            f'Remove the saved workflow "{previous.name}"? Existing jobs and their outputs remain.',
            'Remove workflow template', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, dialog)
        guard()
        if accepted != wx.YES:
            return
        start(lambda: store.remove(previous, cancelled=cancelled),
              lambda value: refresh(message=f'Removed saved workflow {previous.name}.'),
              'Removing the selected saved workflow.')

    def exchange(event=None, *, importing=False):
        guard()
        previous = None if importing else selected(single=True)[0]
        picker = wx.FileDialog(dialog,
            'Import a workflow as a new template' if importing else 'Export workflow template',
            defaultFile='workflow-template.json', wildcard='JSON (*.json)|*.json',
            style=(wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) if importing else wx.FD_SAVE)
        try:
            accepted = picker.ShowModal()
            path = Path(picker.GetPath()) if accepted == wx.ID_OK else None
        finally:
            picker.Destroy()
        guard()
        if path is None:
            return
        if importing:
            def choose_import_name(value):
                name = name_dialog(value.name, 'Name imported workflow template')
                if name is None:
                    return
                start(lambda: store.save(name, value.draft, cancelled=cancelled),
                      lambda saved: refresh(message=f'Imported {saved.name} as a new, unapproved template.'),
                      'Saving the selected template under its new local identity.')
            start(lambda: store.read_import(path), choose_import_name,
                  'Validating the selected template; no saved plan is executed.')
            return
        def confirm(parent, target):
            return wx.MessageBox(f'Replace the existing workflow export?\n{target}',
                'Replace workflow template export', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                parent) == wx.YES
        approval = approve_export_destination(dialog, path, private_root=root, confirm=confirm)
        guard()
        if approval is None:
            return
        def work():
            payload = store.export_json(previous)
            guard()
            return write_export(path, payload, private_root=root, approval=approval,
                                cancelled=cancelled)
        start(work, lambda receipt: report(export_completion(
            f'Exported {previous.name}. Literal text and paths are included; scope and approval are excluded.',
            receipt)), 'Writing the reviewed template export.')

    def action(callback):
        def invoke(event=None):
            if busy:
                return
            try:
                guard()
                callback(event)
            except Exception as exc:
                report(exc)
        return invoke

    handlers = {'load': load, 'save': save, 'update': lambda event: save(event, updating=True),
        'rename': rename, 'remove': remove, 'import': lambda event: exchange(event, importing=True),
        'export': exchange, 'refresh': refresh, 'close': close}
    for identity, handler in handlers.items():
        buttons[identity].Bind(wx.EVT_BUTTON, close if identity == 'close' else action(handler))
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.template_controls = {'rows': rows_control, 'status': status, 'buttons': buttons}
    dialog.template_handlers = {identity: action(handler) for identity, handler in handlers.items()
                                if identity != 'close'}
    dialog.template_handlers['close'] = close
    enabled()
    try:
        refresh()
    except Exception as exc:
        report(exc)
    return dialog

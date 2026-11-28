"""Native authoring and review bindings for the existing assistant catalogue.

Parameter/project helpers adapt donor 949fe358 through the current Plan, CAS
catalogue and semantic keyboard manager. No workflow executor or second store.
"""
import json

from ..assistant.templates import (KINDS, MAX_BYTES, WorkflowTemplate, TemplateStore,
                                    review_request)
from ..durable_state import _unique, read_json_snapshot


def _json(raw):
    if type(raw) is not str or len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Template JSON exceeds its text budget.')
    return json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Template numbers must be finite.')))


def edit_template(parent, initial, *, guard=lambda: None, wx_module=None):
    """Return validated passive data only; the caller owns confirmation and CAS."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    dialog = wx.Dialog(parent, title='Typed workflow template settings', size=(900, 830),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    fields, result = {}, []
    def field(key, label, value, *, multiline=False):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = wx.TextCtrl(dialog, value=value, style=wx.TE_MULTILINE if multiline else 0)
        control.SetName(label.replace('&', ''))
        layout.Add(control, 1 if multiline else 0, wx.ALL | wx.EXPAND, 4)
        fields[key] = control
        return control
    field('name', 'Template &name', initial.name)
    field('summary', 'Template summary', initial.summary)
    field('steps', 'Fixed typed &steps (JSON; parameters replace whole values)',
          json.dumps([step.to_dict() for step in initial.steps], ensure_ascii=False, indent=2), multiline=True)
    field('parameters', 'Parameter &declarations (kind, label, example)',
          json.dumps(initial.parameters, ensure_ascii=False, indent=2), multiline=True)
    status = field('status', 'Template validation status',
        'Passive settings only. Every use needs a fresh scope review and separate Run.')
    status.SetEditable(False)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    project_button = wx.Button(dialog, label='Use Studio &project JSON...')
    accept = wx.Button(dialog, wx.ID_OK, '&Use these template settings')
    cancel = wx.Button(dialog, wx.ID_CANCEL, '&Cancel')
    for control in (project_button, accept, cancel): buttons.Add(control, 0, wx.ALL, 4)
    layout.Add(buttons, 0, wx.ALL, 4)
    def use_project(event=None):
        try:
            guard()
            picker = wx.FileDialog(dialog, 'Select a saved Studio project to copy',
                wildcard='Studio project JSON (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if picker.ShowModal() != wx.ID_OK: return
                path = picker.GetPath()
            finally:
                picker.Destroy()
            guard()
            from ..studio.project import Project
            project = Project.from_dict(read_json_snapshot(path, max_bytes=MAX_BYTES)[0])
            candidate = WorkflowTemplate.from_project(fields['name'].GetValue(), project,
                                                      kind=initial.kind, identity=initial.id)
            guard()
            fields['steps'].ChangeValue(json.dumps([step.to_dict() for step in candidate.steps],
                                                   ensure_ascii=False, indent=2))
            fields['parameters'].ChangeValue('{}')
            status.ChangeValue('Project copied with old source references removed. Each use creates a new project identity.')
        except Exception as exc:
            status.ChangeValue(str(exc)); status.SetFocus()
    def use(event=None):
        try:
            guard()
            candidate = WorkflowTemplate.define(fields['name'].GetValue(), _json(fields['steps'].GetValue()),
                _json(fields['parameters'].GetValue()), kind=initial.kind,
                summary=fields['summary'].GetValue(), identity=initial.id)
            guard()
            result[:] = [candidate]
            dialog.EndModal(wx.ID_OK)
        except Exception as exc:
            status.ChangeValue(str(exc)); status.SetFocus()
    project_button.Bind(wx.EVT_BUTTON, use_project)
    accept.Bind(wx.EVT_BUTTON, use)
    dialog.SetSizer(layout); dialog.SetEscapeId(wx.ID_CANCEL)
    dialog.workflow_fields = fields
    dialog.workflow_handlers = dict(use=use, project=use_project)
    fields['name'].SetFocus()
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
        guard()
        return result[0] if accepted and result else None
    finally:
        dialog.Destroy()


def choose_values(parent, record, *, guard=lambda: None, wx_module=None):
    """Return scope-free review data, with no example values accepted implicitly."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    dialog = wx.Dialog(parent, title='Review workflow values and batch items', size=(850, 700),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    fields, result = {}, []
    for key, label, value in (
            ('parameters', 'Declared parameters (read only)', json.dumps(record.parameters, ensure_ascii=False, indent=2)),
            ('values', 'Explicit parameter &values (JSON)', '{}'),
            ('items', 'Per-item &overrides (JSON array; [] means one use)', '[]'),
            ('status', 'Workflow review status', 'No job has been submitted. Choose current scope after loading.')):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = wx.TextCtrl(dialog, value=value, style=wx.TE_MULTILINE)
        control.SetName(label.replace('&', ''))
        if key in ('parameters', 'status'): control.SetEditable(False)
        fields[key] = control
        layout.Add(control, 1, wx.ALL | wx.EXPAND, 4)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    example = wx.Button(dialog, label='Copy &example values for review')
    accept = wx.Button(dialog, wx.ID_OK, '&Load new review')
    cancel = wx.Button(dialog, wx.ID_CANCEL, '&Cancel')
    for button in (example, accept, cancel): buttons.Add(button, 0, wx.ALL, 4)
    layout.Add(buttons, 0, wx.ALL, 4)
    def examples(event=None):
        try:
            guard()
            fields['values'].ChangeValue(json.dumps(
                {name: spec['example'] for name, spec in record.parameters.items()}, ensure_ascii=False, indent=2))
            fields['status'].ChangeValue('Example data copied. Review every value; no permissions or job were loaded.')
        except Exception as exc:
            fields['status'].ChangeValue(str(exc))
    def use(event=None):
        try:
            guard()
            values, items = _json(fields['values'].GetValue()), _json(fields['items'].GetValue())
            requested = review_request(record, values, items=None if items == [] else items)
            guard()
            result[:] = [requested]
            dialog.EndModal(wx.ID_OK)
        except Exception as exc:
            fields['status'].ChangeValue(str(exc)); fields['status'].SetFocus()
    example.Bind(wx.EVT_BUTTON, examples); accept.Bind(wx.EVT_BUTTON, use)
    dialog.SetSizer(layout); dialog.SetEscapeId(wx.ID_CANCEL)
    dialog.workflow_fields = fields
    dialog.workflow_handlers = dict(use=use, examples=examples)
    fields['values'].SetFocus()
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
        guard()
        return result[0] if accepted and result else None
    finally:
        dialog.Destroy()


def refresh_template_macros(frame, *, wx_module=None):
    state = getattr(frame, '_assistant_template_macros', None)
    if state is None: return
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    registered, library, root = state['registered'], state['library'], state['root']
    if (getattr(frame, '_closing', False) or frame.library is not library
            or frame.library.root != root):
        for item in registered.values(): item.Enable(False)
        return
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    records, _ = TemplateStore(root).list()
    if records and state['menu'] is None:
        state['menu'] = wx.Menu()
        state['tools_menu'].AppendSubMenu(state['menu'], 'Saved workflow &review commands')
    menu = state['menu']
    present = {record.command_id for record in records}
    for identity, item in registered.items(): item.Enable(identity in present)
    for record in records:
        if record.command_id in registered: continue
        label = 'Review ' + record.name.replace('&', '&&') + ' [' + record.id[:8] + ']...'
        item = menu.Append(wx.ID_ANY, label)
        try:
            register_menu_command(frame, CommandSpec(record.command_id, label), item)
        except Exception:
            menu.Delete(item)
            raise
        def review(event, identity=record.id):
            if getattr(frame, '_closing', False): return
            if frame.library is not library or frame.library.root != root:
                frame.report('This template belongs to a different library. Reopen its template review.'); return
            try:
                TemplateStore(root).get(identity)
                from .assistant_dialog import show_assistant
                show_assistant(frame, template_id=identity)
            except (ValueError, OSError, RuntimeError) as exc:
                frame.report('Template review: ' + str(exc))
        frame.Bind(wx.EVT_MENU, review, item)
        registered[record.command_id] = item


def install_template_macros(frame, tools_menu, *, wx_module=None):
    if getattr(frame, '_assistant_template_macros', None) is not None: return
    if getattr(frame, 'library', None) is None: return
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    frame._assistant_template_macros = dict(tools_menu=tools_menu, menu=None, registered={},
        library=frame.library, root=frame.library.root)
    try:
        refresh_template_macros(frame, wx_module=wx)
    except (ValueError, OSError, RuntimeError) as exc:
        message = 'Template shortcut inventory needs review: ' + str(exc)
        wx.CallAfter(lambda: frame.report(message) if not getattr(frame, '_closing', False) else None)

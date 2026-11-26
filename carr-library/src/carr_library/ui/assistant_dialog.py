"""Native scoped local assistant and non-secret provider settings."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..durable_state import atomic_bytes


def show_assistant(frame):
    import wx
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..studio.render import ProductionService
    production = ProductionService(frame.library.root / 'state' / 'studio')
    service = AssistantService(frame.library, production=production.assistant_callback)
    dialog = wx.Dialog(frame, title='Local assistant — review and run a bounded plan', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-assistant')
    cancel = threading.Event()
    alive, busy, plan, job_id = True, False, None, None
    workspaces = frame.library.workspaces()

    def field(label, cls=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP, 5)
        control = cls(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.ALL | wx.EXPAND, 5)
        return control

    scope_choice = field('&Library scope', wx.Choice,
        choices=['Create a new library / list libraries only'] + [row.name for row in workspaces])
    current = frame.active[0] if frame.active else None
    scope_choice.SetSelection(next((i + 1 for i, row in enumerate(workspaces) if row.id == current), 0))
    input_folder = field('Allowed &input folder (optional; no other folders are readable)')
    output_folder = field('Chosen &output folder (required for audio, extraction or backup)')
    choose_row = wx.BoxSizer(wx.HORIZONTAL)
    for label, target in [('Choose input folder', input_folder), ('Choose output folder', output_folder)]:
        button = wx.Button(dialog, label=label)
        def choose(event, target=target):
            picker = wx.DirDialog(dialog, 'Choose a local folder')
            try:
                if picker.ShowModal() == wx.ID_OK:
                    target.SetValue(picker.GetPath())
            finally:
                picker.Destroy()
        button.Bind(wx.EVT_BUTTON, choose)
        choose_row.Add(button, 0, wx.ALL, 4)
    layout.Add(choose_row)
    full_backup = wx.CheckBox(dialog, label='Allow a complete backup of all my libraries for this plan')
    layout.Add(full_backup, 0, wx.ALL, 5)
    instruction = field('&Goal — guided English/Ukrainian commands or typed JSON plan', proportion=1,
                        style=wx.TE_MULTILINE, value='Покажи бібліотеки')
    review = field('Plan &preview and actual results', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='LOCAL ONLY. Nothing executes until you review the plan and choose Run.')
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for identity, label in [('preview', '&Preview plan'), ('run', '&Run / resume'), ('pause', '&Pause'),
                             ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        buttons[identity] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    dialog.SetSizer(layout)
    buttons['run'].Disable()
    buttons['pause'].Disable()

    def report(value):
        if alive:
            review.ChangeValue(str(value))

    def invalidate(event=None):
        nonlocal plan, job_id
        plan, job_id = None, None
        buttons['run'].Disable()
        if event is not None:
            event.Skip()

    def preview(event=None):
        nonlocal plan, job_id
        try:
            index = scope_choice.GetSelection()
            scope = Scope(workspaces[index - 1].id if index > 0 else None,
                          (Path(input_folder.GetValue()),) if input_folder.GetValue().strip() else (),
                          Path(output_folder.GetValue()) if output_folder.GetValue().strip() else None,
                          allow_create=index == 0, allow_full_backup=full_backup.GetValue())
            candidate = service.preview(instruction.GetValue(), scope)
            if any(step.tool == 'audio.produce' and not step.arguments.get('voice_ids') and 'script' not in step.arguments
                   for step in candidate.steps):
                voices = production.voices.voices()
                choice = wx.SingleChoiceDialog(dialog, 'Choose the default local voice for this production.',
                                                'Production voice', [v.name + '; ' + v.language for v in voices])
                try:
                    if choice.ShowModal() != wx.ID_OK:
                        return
                    selected = choice.GetSelection()
                finally:
                    choice.Destroy()
                if not 0 <= selected < len(voices):
                    raise ValueError('Install and choose a local voice before narration.')
                steps = tuple(Step(s.tool, {**s.arguments, 'voice_ids': [voices[selected].id]})
                              if s.tool == 'audio.produce' and 'script' not in s.arguments else s for s in candidate.steps)
                candidate = Plan(scope, steps, candidate.summary)
                service.validate(candidate)
            plan, job_id = candidate, None
            report('LOCAL ONLY — no remote services or charges.\n\n' +
                   json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) +
                   '\n\nChoose Run to authorize these operations inside these directories.')
            buttons['run'].Enable()
            review.SetFocus()
        except Exception as exc:
            invalidate()
            report(str(exc))
            review.SetFocus()

    def run(event=None):
        nonlocal busy, job_id
        if busy or (plan is None and job_id is None):
            return
        try:
            if job_id is None:
                job_id = service.submit(plan)
            identity = job_id
            busy = True
            cancel.clear()
            for key in ('preview', 'run', 'jobs'):
                buttons[key].Disable()
            buttons['pause'].Enable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup):
                control.Disable()
            future = executor.submit(service.resume, identity, cancelled=cancel.is_set,
                                      progress=lambda completed, total, message: wx.CallAfter(report,
                                          f'{completed}/{total}: {message}'))
        except Exception as exc:
            busy = False
            report(str(exc))
            return
        def finish(done):
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = False
                for key in ('preview', 'run', 'jobs'):
                    buttons[key].Enable()
                buttons['pause'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup):
                    control.Enable()
                try:
                    result = done.result()
                    report(f'{result.status}: {result.completed} of {result.total} verified operations.\n' +
                           json.dumps(result.results, ensure_ascii=False, indent=2) + '\n' + result.message)
                except Exception as exc:
                    report(str(exc))
                review.SetFocus()
            wx.CallAfter(apply)
        future.add_done_callback(finish)

    def jobs(event=None):
        nonlocal job_id, plan
        rows = service.jobs()
        choice = wx.SingleChoiceDialog(dialog, 'Choose a saved local plan.', 'Assistant jobs',
            [f'{row.id}; {row.status}; {row.completed}/{row.total}' for row in rows])
        try:
            if choice.ShowModal() == wx.ID_OK and 0 <= choice.GetSelection() < len(rows):
                row = rows[choice.GetSelection()]
                job_id, plan = row.id, None
                report(f'{row.status}: {row.completed}/{row.total}. {row.message}\nResume uses the original reviewed scope.')
                buttons['run'].Enable()
        finally:
            choice.Destroy()

    def close(event=None):
        nonlocal alive
        cancel.set()
        if busy and job_id:
            service.pause(job_id)
        alive = False
        executor.shutdown(wait=False, cancel_futures=True)
        dialog.EndModal(wx.ID_CLOSE)

    def help_text(event):
        from .desktop_app import show_text
        show_text(dialog, 'Local assistant commands', HELP)

    for identity, callback in {'preview': preview, 'run': run, 'pause': lambda e: cancel.set(),
                               'jobs': jobs, 'help': help_text, 'close': close}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction):
        control.Bind(wx.EVT_TEXT, invalidate)
    scope_choice.Bind(wx.EVT_CHOICE, invalidate)
    full_backup.Bind(wx.EVT_CHECKBOX, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        dialog.ShowModal()
    finally:
        if alive:
            close()
        dialog.Destroy()


def show_provider_settings(frame):
    import wx
    from ..assistant import ProfileStore, ProviderProfile, WindowsCredentialVault
    store = ProfileStore(frame.library.root / 'state' / 'providers')
    vault = WindowsCredentialVault()
    dialog = wx.Dialog(frame, title='AI and API connections — optional', size=(760, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Local operation is the default. Saving a profile makes no network request.\n'
        'Remote use requires a separate scoped job authorization and budget.'), 0, wx.ALL, 6)
    listing = wx.ListBox(dialog)
    listing.SetName('Provider accounts')
    layout.Add(listing, 1, wx.EXPAND | wx.ALL, 6)
    status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Connection configuration status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    rows = ()

    def refresh():
        nonlocal rows
        rows = store.list()
        listing.SetItems([f'{r.name}; {r.adapter}; {"local" if r.local else "remote"}; '
                          f'{"enabled" if r.enabled else "disabled"}' for r in rows])

    def edit(event=None, *, new=False):
        index = listing.GetSelection()
        current = rows[index] if not new and 0 <= index < len(rows) else None
        form = wx.Dialog(dialog, title='Add connection' if current is None else 'Edit connection', size=(650, 700))
        box = wx.BoxSizer(wx.VERTICAL)
        def field(label, value='', password=False):
            box.Add(wx.StaticText(form, label=label), 0, wx.ALL, 4)
            control = wx.TextCtrl(form, value=value, style=wx.TE_PASSWORD if password else 0)
            control.SetName(label)
            box.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            return control
        name = field('Account name', current.name if current else '')
        box.Add(wx.StaticText(form, label='Adapter'), 0, wx.ALL, 4)
        adapter = wx.Choice(form, choices=['openai-compatible', 'elevenlabs'])
        adapter.SetSelection(1 if current and current.adapter == 'elevenlabs' else 0)
        box.Add(adapter, 0, wx.EXPAND | wx.ALL, 4)
        endpoint = field('Official endpoint or explicitly chosen loopback service', current.endpoint if current else 'http://127.0.0.1:8080/v1')
        model = field('Model ID', current.model if current else 'local-model')
        voice = field('Voice ID; blank for language models', current.voice or '' if current else '')
        capability = field('Capabilities, comma separated: llm.chat, speech.synthesize, music.compose, sfx.generate',
                           ','.join(current.capabilities) if current else 'llm.chat')
        secret = field('New API key; leave empty to keep existing key', password=True)
        local = wx.CheckBox(form, label='This is a local loopback service')
        local.SetValue(current.local if current else True)
        enabled = wx.CheckBox(form, label='Enable this profile for explicitly authorized jobs')
        enabled.SetValue(current.enabled if current else False)
        box.Add(local, 0, wx.ALL, 4)
        box.Add(enabled, 0, wx.ALL, 4)
        box.Add(form.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 5)
        form.SetSizer(box)
        try:
            if form.ShowModal() != wx.ID_OK:
                return
            identity = current.id if current else uuid4().hex
            profile = ProviderProfile(identity, name.GetValue(), adapter.GetStringSelection(), endpoint.GetValue(),
                model.GetValue(), tuple(x.strip() for x in capability.GetValue().split(',') if x.strip()),
                current.credential_ref if current else None, local.GetValue(), enabled.GetValue(), voice.GetValue() or None)
            authority_changed = current and (current.endpoint, current.adapter, current.local) != (profile.endpoint, profile.adapter, profile.local)
            if authority_changed:
                profile = replace(profile, credential_ref=None, enabled=False)
            new_reference = None
            if secret.GetValue():
                new_reference = vault.put(profile.id, secret.GetValue())
                profile = replace(profile, credential_ref=new_reference)
            try:
                store.save(profile)
            except Exception:
                if new_reference:
                    vault.delete(profile.id, new_reference)
                raise
            if current and new_reference and current.credential_ref:
                vault.delete(current.id, current.credential_ref)
            secret.ChangeValue('')
            refresh()
            status.ChangeValue('Saved. Credentials are held by Windows Credential Manager. No provider was contacted.')
        except Exception as exc:
            status.ChangeValue(str(exc))
        finally:
            secret.ChangeValue('')
            form.Destroy()
        status.SetFocus()

    def remove(event):
        index = listing.GetSelection()
        if not 0 <= index < len(rows):
            return
        profile = rows[index]
        if wx.MessageBox('Remove this connection and its stored credential?', 'Remove connection',
                          wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        try:
            store.remove(profile.id, vault=vault)
            refresh()
            status.ChangeValue('Connection removed.')
        except Exception as exc:
            status.ChangeValue(str(exc))

    def check(event):
        index = listing.GetSelection()
        if not 0 <= index < len(rows):
            return
        try:
            profile = store.get(rows[index].id)
            if profile.credential_ref:
                vault.get(profile.id, profile.credential_ref)
            status.ChangeValue('Local profile and credential configuration is valid. API connectivity, pricing and access were not tested.')
        except Exception as exc:
            status.ChangeValue(str(exc))
        status.SetFocus()

    def exchange(event, *, importing=False):
        picker = wx.FileDialog(dialog, 'Import non-secret profiles' if importing else 'Export non-secret profiles',
            defaultFile='provider-profiles.json', wildcard='JSON (*.json)|*.json',
            style=(wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) if importing else (wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT))
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            path = Path(picker.GetPath())
            if importing:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError('Profile file exceeds the import budget.')
                store.import_json(path.read_text(encoding='utf-8'))
                refresh()
                status.ChangeValue('Profiles imported disabled. Reconnect each account to its credential.')
            else:
                atomic_bytes(path, store.export_json().encode('utf-8'), replace=path.exists())
                status.ChangeValue('Exported non-secret profiles. No API keys or vault references are included.')
        except Exception as exc:
            status.ChangeValue(str(exc))
        finally:
            picker.Destroy()

    row = wx.BoxSizer(wx.HORIZONTAL)
    for label, handler in [('&Add', lambda e: edit(e, new=True)), ('&Edit', edit), ('&Remove', remove),
        ('&Check local configuration', check), ('&Import', lambda e: exchange(e, importing=True)), ('E&xport', exchange)]:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 3)
    layout.Add(row)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 5)
    dialog.SetEscapeId(wx.ID_CLOSE)
    dialog.Bind(wx.EVT_BUTTON, lambda event: dialog.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
    dialog.SetSizer(layout)
    refresh()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def install_assistant_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    if getattr(frame, '_assistant_actions_installed', False):
        return
    menu = wx.Menu()
    commands = []
    for identity, title, shortcut, handler in [
            ('assistant.open', 'Local assistant...', 'Ctrl+Alt+J', lambda e: show_assistant(frame)),
            ('assistant.providers', 'AI and API connections...', '', lambda e: show_provider_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title + ('\t' + shortcut if shortcut else ''))
        frame.Bind(wx.EVT_MENU, handler, item)
        commands.append((CommandSpec(identity, title, default_shortcut=shortcut), item))
    frame.menu_bar.Append(menu, '&Tools')
    for spec, item in commands:
        register_menu_command(frame, spec, item)
    frame._assistant_actions_installed = True

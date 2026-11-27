"""One native local assistant, delegating audio to the existing Studio service."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading


def show_assistant(frame, *, production=None, on_ready=None):
    import wx
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..assistant.production import LocalProduction
    production = production or LocalProduction(frame.library.root)
    service = AssistantService(frame.library, production=production)
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
    full_backup.SetName('Allow a complete backup of all libraries for this plan')
    layout.Add(full_backup, 0, wx.ALL, 5)
    budget = field('Per-operation output and audio working-disk budget (MiB)', value='1024')
    file_budget = field('Maximum files per operation', value='5000')
    deadline = field('Time budget for each run or resume (seconds)', value='3600')
    instruction = field('&Goal — guided English/Ukrainian commands or typed JSON plan', proportion=1,
                        style=wx.TE_MULTILINE, value='Покажи бібліотеки')
    review = field('Plan &preview and actual results', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='LOCAL ONLY. Nothing executes until you review the plan and choose Run.')
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for identity, label in [('preview', '&Preview plan'), ('model', 'Propose with local &model'),
                             ('run', '&Run / resume'), ('pause', '&Pause'),
                             ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
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

    def selected_scope():
        index = scope_choice.GetSelection()
        return Scope(workspaces[index - 1].id if index > 0 else None,
                     (Path(input_folder.GetValue()),) if input_folder.GetValue().strip() else (),
                     Path(output_folder.GetValue()) if output_folder.GetValue().strip() else None,
                     allow_create=index == 0, allow_full_backup=full_backup.GetValue(),
                     max_bytes=int(budget.GetValue()) * 1024 ** 2,
                     max_files=int(file_budget.GetValue()),
                     max_seconds=float(deadline.GetValue()))

    def preview(event=None):
        nonlocal plan, job_id
        try:
            scope = selected_scope()
            candidate = service.preview(instruction.GetValue(), scope)
            if any(step.tool == 'audio.produce' and not step.arguments.get('voice_ids') and 'script' not in step.arguments
                   for step in candidate.steps):
                voices = tuple(item for item in production.voices() if item.available)
                choice = wx.SingleChoiceDialog(dialog, 'Choose the default local voice for this production.',
                                                'Production voice', [v.name + '; ' + ', '.join(v.languages) for v in voices])
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

    def propose(event=None):
        nonlocal busy, plan, job_id
        if busy:
            return
        from uuid import uuid4
        from ..assistant.llm import local_plan_request, propose_local_plan
        from ..providers import BudgetLedger, JobApproval, ProfileStore, ProviderGateway
        try:
            scope = selected_scope()
            goal = instruction.GetValue()
            request = local_plan_request(goal)
            store = ProfileStore(frame.library.root / 'api-connections.json')
            profiles = tuple(profile for profile in store.profiles()
                             if profile.local and profile.provider == 'openai-compatible'
                             and profile.capabilities == ('llm',))
            if not profiles:
                raise ValueError('Add an explicit loopback language-model profile in AI and API connections first. '
                                 'No model or service is started automatically.')
            picker = wx.SingleChoiceDialog(dialog,
                'Choose the local service that may receive this goal. Only a proposal is requested.',
                'Local model proposal', [profile.name + '; ' + profile.endpoint + '; ' + profile.model
                                         for profile in profiles])
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                index = picker.GetSelection()
            finally:
                picker.Destroy()
            if not 0 <= index < len(profiles):
                raise ValueError('Choose an available local model profile.')
            profile = profiles[index]
            confirmation = wx.MessageDialog(dialog,
                'Send this goal to ' + profile.endpoint + ' using ' + profile.model + '?\n\n'
                'Only the goal and fixed planning instructions are sent. No library or source document is attached. '
                'Returned steps remain unapproved until you preview and run them.',
                'Request a local proposal', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
            try:
                if confirmation.ShowModal() != wx.ID_YES:
                    return
            finally:
                confirmation.Destroy()
            approval = JobApproval(uuid4().hex, profile.fingerprint,
                (request.fingerprint(profile),), allow_loopback=True)
            gateway = ProviderGateway(BudgetLedger(frame.library.root / 'api-operations.sqlite'))
            invalidate()
            busy = True
            cancel.clear()
            for key in ('preview', 'model', 'run', 'jobs'):
                buttons[key].Disable()
            buttons['pause'].Enable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, budget, file_budget, deadline):
                control.Disable()
            report('Waiting for the selected local model. No assistant operation has been approved.')
            future = executor.submit(propose_local_plan, service, gateway, profile, goal, scope,
                approval=approval, operation_id=uuid4().hex, cancelled=cancel.is_set)
        except Exception as exc:
            busy = False
            for key in ('preview', 'model', 'jobs'):
                buttons[key].Enable()
            buttons['pause'].Disable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, budget, file_budget, deadline):
                control.Enable()
            invalidate()
            report(str(exc))
            return

        def finish(done):
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = False
                for key in ('preview', 'model', 'jobs'):
                    buttons[key].Enable()
                buttons['pause'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup, budget, file_budget, deadline):
                    control.Enable()
                invalidate()
                try:
                    candidate = done.result()
                    draft = {key: value for key, value in candidate.to_dict().items() if key != 'scope'}
                    instruction.ChangeValue(json.dumps(draft, ensure_ascii=False, indent=2))
                    report('Local model proposal received and structurally checked. Nothing has run.\n\n'
                           + json.dumps(draft, ensure_ascii=False, indent=2)
                           + '\n\nReview or edit the goal, then choose Preview plan to bind your current permissions. '
                             'Run remains disabled until that separate preview succeeds.')
                except Exception as exc:
                    report(str(exc))
                review.SetFocus()
            wx.CallAfter(apply)
        future.add_done_callback(finish)

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
            for key in ('preview', 'model', 'run', 'jobs'):
                buttons[key].Disable()
            buttons['pause'].Enable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, budget, file_budget, deadline):
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
                for key in ('preview', 'model', 'run', 'jobs'):
                    buttons[key].Enable()
                buttons['pause'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup, budget, file_budget, deadline):
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
        try:
            rows = service.jobs()
        except (ValueError, OSError) as exc:
            job_id, plan = None, None
            buttons['run'].Disable()
            report(str(exc)[:1000])
            review.SetFocus()
            return
        choice = wx.SingleChoiceDialog(dialog, 'Choose a saved local plan.', 'Assistant jobs',
            [f'{row.id}; {row.status}; {row.completed}/{row.total}' for row in rows])
        try:
            if choice.ShowModal() == wx.ID_OK and 0 <= choice.GetSelection() < len(rows):
                row = rows[choice.GetSelection()]
                try:
                    record = service.store.load(row.id)
                except (ValueError, OSError) as exc:
                    job_id, plan = None, None
                    buttons['run'].Disable()
                    report(row.message + '\n' + str(exc)[:1000])
                    review.SetFocus()
                    return
                if record.get('requires_reapproval') or record.get('data_dir') != str(frame.library.root):
                    job_id, plan = None, None
                    instruction.ChangeValue(service.review_instruction(row.id))
                    report(row.message + '\nOriginal approved scope and steps:\n' +
                           json.dumps(record['plan'], ensure_ascii=False, indent=2) +
                           '\n\nThe goal field contains an unapproved copy of the original steps. '
                           'Choose the current library and input/output folders, edit path arguments '
                           'if necessary, then choose Preview and Run to approve a new job. '
                           'The saved job and its original permissions remain unchanged.')
                    buttons['run'].Disable()
                    review.SetFocus()
                    return
                job_id, plan = row.id, None
                report(f'{row.status}: {row.completed}/{row.total}. {row.message}\nResume uses the original reviewed scope.')
                buttons['run'].Enable()
        finally:
            choice.Destroy()

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        alive = False
        cancel.set()
        # The worker persists its paused checkpoint after releasing snapshot
        # locks. A concurrent control write here can be refused by that same
        # snapshot and must not prevent the native dialog from closing.
        executor.shutdown(wait=False, cancel_futures=True)
        dialog.EndModal(wx.ID_CLOSE)

    def help_text(event):
        from .desktop_app import show_text
        show_text(dialog, 'Local assistant commands', HELP)

    for identity, callback in {'preview': preview, 'model': propose, 'run': run, 'pause': lambda e: cancel.set(),
                               'jobs': jobs, 'help': help_text, 'close': close}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction, budget, file_budget, deadline):
        control.Bind(wx.EVT_TEXT, invalidate)
    scope_choice.Bind(wx.EVT_CHOICE, invalidate)
    full_backup.Bind(wx.EVT_CHECKBOX, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog, {'instruction': instruction, 'review': review,
                'budget': budget, 'deadline': deadline, 'preview': buttons['preview'],
                'run': buttons['run'], 'model': buttons['model'], 'close': buttons['close'], 'output': output_folder})
        dialog.ShowModal()
    finally:
        if alive:
            close()
        dialog.Destroy()


def install_assistant_actions(frame):
    """Add one action; existing API settings remain in their current host."""
    import wx
    menu_bar = frame.GetMenuBar()
    index = menu_bar.FindMenu('Tools')
    if index == wx.NOT_FOUND:
        menu = wx.Menu()
        menu_bar.Append(menu, '&Tools')
    else:
        menu = menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Local assistant...\tCtrl+Alt+J')
    frame.Bind(wx.EVT_MENU, lambda event: show_assistant(frame), item)

"""Native scoped local assistant and non-secret provider settings."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading



def show_assistant(frame):
    import wx
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..assistant.audio_bridge import LocalAudioProduction
    from ..assistant.planner import preview as plan_preview
    production = LocalAudioProduction(frame.library.root)
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
    choose_buttons = []
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
        choose_buttons.append(button)
        choose_row.Add(button, 0, wx.ALL, 4)
    layout.Add(choose_row)
    full_backup = wx.CheckBox(dialog, label='Allow a complete backup of all my libraries for this plan')
    layout.Add(full_backup, 0, wx.ALL, 5)
    byte_budget = field('Maximum output and imported data (MiB)', wx.SpinCtrl, min=1, max=2147483647, initial=1024)
    file_budget = field('Maximum files for this job', wx.SpinCtrl, min=1, max=1000000, initial=5000)
    time_budget = field('Maximum job time (minutes)', wx.SpinCtrl, min=1, max=525600, initial=60)
    instruction = field('&Goal — guided English/Ukrainian commands or typed JSON plan', proportion=1,
                        style=wx.TE_MULTILINE, value='List libraries')
    review = field('Plan &preview and actual results', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='LOCAL ONLY. Nothing executes until you review the plan and choose Run.')
    progress_gauge = wx.Gauge(dialog, range=100)
    progress_gauge.SetName('Verified assistant steps completed')
    layout.Add(progress_gauge, 0, wx.ALL | wx.EXPAND, 5)
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for identity, label in [('preview', '&Preview plan'), ('model', 'Propose with local &model'),
                             ('run', '&Run / resume'), ('pause', '&Pause'),
                             ('cancel', 'Cancel &job'), ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        buttons[identity] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(buttons['close'].GetId())
    buttons['run'].Disable()
    buttons['pause'].Disable()
    buttons['cancel'].Disable()

    def report(value):
        if alive:
            review.ChangeValue(str(value))

    def refresh_workspaces():
        nonlocal workspaces
        index = scope_choice.GetSelection()
        selected = workspaces[index - 1].id if 0 < index <= len(workspaces) else None
        workspaces = frame.library.workspaces()
        scope_choice.SetItems(['Create a new library / list libraries only'] +
                              [row.name for row in workspaces])
        scope_choice.SetSelection(next((i + 1 for i, row in enumerate(workspaces)
                                       if row.id == selected), 0))

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
                     max_bytes=byte_budget.GetValue() * 1024 * 1024,
                     max_files=file_budget.GetValue(), max_seconds=time_budget.GetValue() * 60)

    def preview(event=None):
        nonlocal plan, job_id
        try:
            scope = selected_scope()
            candidate = plan_preview(instruction.GetValue(), scope)
            voices = None
            steps = []
            for step in candidate.steps:
                if step.tool != 'audio.produce' or 'script' in step.arguments:
                    steps.append(step)
                    continue
                if voices is None:
                    voices = [voice for voice in production.voices.voices()
                              if voice.available and voice.languages]
                arguments = dict(step.arguments)
                selected = arguments.get('voice_ids')
                if selected is None:
                    if not voices:
                        raise ValueError('Install and choose a local voice before narration.')
                    choice = wx.SingleChoiceDialog(dialog, 'Choose the local voice for this narration.',
                        'Narration voice', [v.name + '; ' + ', '.join(v.languages) for v in voices])
                    try:
                        if choice.ShowModal() != wx.ID_OK:
                            return
                        index = choice.GetSelection()
                    finally:
                        choice.Destroy()
                    if not 0 <= index < len(voices):
                        raise ValueError('Choose an available local voice.')
                    voice = voices[index]
                    arguments['voice_ids'] = [voice.id]
                else:
                    if not isinstance(selected, (list, tuple)) or len(selected) != 1:
                        raise ValueError('Choose one voice for literal narration.')
                    voice = next((v for v in voices if v.id == selected[0]), None)
                    if voice is None:
                        raise ValueError('The selected local voice is unavailable.')
                if 'language' not in arguments:
                    if len(voice.languages) == 1:
                        arguments['language'] = voice.languages[0]
                    else:
                        choice = wx.SingleChoiceDialog(dialog, 'Choose the exact narration language.',
                                                       'Narration language', list(voice.languages))
                        try:
                            if choice.ShowModal() != wx.ID_OK:
                                return
                            index = choice.GetSelection()
                        finally:
                            choice.Destroy()
                        if not 0 <= index < len(voice.languages):
                            raise ValueError('Choose a declared language for this voice.')
                        arguments['language'] = voice.languages[index]
                steps.append(Step(step.tool, arguments))
            candidate = Plan(scope, tuple(steps), candidate.summary)
            service.validate(candidate)
            for step in candidate.steps:
                if step.tool == 'audio.produce':
                    production.validate_settings(step.arguments)
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

    def update_progress(completed, total, message):
        if alive:
            progress_gauge.SetValue(min(100, int(100 * completed / total)) if total else 0)
            report(f'{completed}/{total}: {message}')

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
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, byte_budget, file_budget, time_budget, *choose_buttons):
                control.Disable()
            report('Waiting for the selected local model. No assistant operation has been approved.')
            future = executor.submit(propose_local_plan, service, gateway, profile, goal, scope,
                approval=approval, operation_id=uuid4().hex, cancelled=cancel.is_set)
        except Exception as exc:
            busy = False
            for key in ('preview', 'model', 'jobs'):
                buttons[key].Enable()
            buttons['pause'].Disable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, byte_budget, file_budget, time_budget, *choose_buttons):
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
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup, byte_budget, file_budget, time_budget, *choose_buttons):
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
            buttons['cancel'].Enable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, byte_budget, file_budget, time_budget, *choose_buttons):
                control.Disable()
            future = executor.submit(service.resume, identity, cancelled=cancel.is_set,
                                      progress=lambda completed, total, message: wx.CallAfter(update_progress, completed, total, message))
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
                buttons['cancel'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup, byte_budget, file_budget, time_budget, *choose_buttons):
                    control.Enable()
                try:
                    result = done.result()
                    report(f'{result.status}: {result.completed} of {result.total} verified operations.\n' +
                           json.dumps(result.results, ensure_ascii=False, indent=2) + '\n' + result.message)
                except Exception as exc:
                    report(str(exc))
                refresh_workspaces()
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
                if row.review_required or row.status == 'attention':
                    if row.review_required and row.completed == row.total:
                        job_id, plan = None, None
                        buttons['run'].Disable()
                        report(f'{row.status}: {row.completed}/{row.total}. {row.message}\n' +
                               json.dumps(row.results, ensure_ascii=False, indent=2))
                        review.SetFocus()
                        return
                    reviewed = service.saved_plan_for_review(row.id)
                    job_id, plan = None, None
                    instruction.SetValue(json.dumps(reviewed, ensure_ascii=False, indent=2))
                    buttons['run'].Disable()
                    report('Choose the current library and input/output folders, review the unfinished steps, '
                           'then select Preview plan. Previous permissions have not been transferred.')
                    instruction.SetFocus()
                    return
                job_id, plan = row.id, None
                report(f'{row.status}: {row.completed}/{row.total}. {row.message}\nResume uses the original reviewed scope.')
                buttons['run'].Enable()
        except (OSError, ValueError, PermissionError) as exc:
            report(str(exc))
            review.SetFocus()
        finally:
            choice.Destroy()

    def close(event=None):
        nonlocal alive
        cancel.set()
        if busy:
            if job_id:
                try:
                    service.pause(job_id)
                except (OSError, ValueError, PermissionError) as exc:
                    report(str(exc))
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            report('Stopping the running task. Close this window after its saved checkpoint is reported.')
            review.SetFocus()
            return
        alive = False
        executor.shutdown(wait=False, cancel_futures=True)
        dialog.EndModal(wx.ID_CLOSE)

    def help_text(event):
        from .desktop_app import show_text
        show_text(dialog, 'Local assistant commands', HELP)

    def cancel_job(event=None):
        if job_id:
            service.cancel(job_id)
            cancel.set()

    for identity, callback in {'preview': preview, 'model': propose, 'run': run, 'pause': lambda e: cancel.set(),
                               'cancel': cancel_job, 'jobs': jobs, 'help': help_text, 'close': close}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction):
        control.Bind(wx.EVT_TEXT, invalidate)
    for control in (byte_budget, file_budget, time_budget):
        control.Bind(wx.EVT_SPINCTRL, invalidate)
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
        frame.refresh_scopes()



def install_assistant_actions(frame):
    import wx
    menu = next((frame.menu_bar.GetMenu(i) for i in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(i).replace('&', '') == 'Tools'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Tools')
    item = menu.Append(wx.ID_ANY, 'Local &assistant...\tCtrl+Alt+J')
    frame.Bind(wx.EVT_MENU, lambda event: show_assistant(frame), item)
    # The existing productivity collector reads this menu after all actions are installed.

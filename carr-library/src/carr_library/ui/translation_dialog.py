"""Native selected-text and library-source translation on the shared gateway."""
from copy import deepcopy
import json
from pathlib import Path
import re
import threading
from time import time
from uuid import uuid4

from ..assistant.profiles import ProfileStore
from ..assistant.providers import Authorization, Gateway, money
from ..assistant.translation import TranslationService, translation_approval
from ..assistant.library_translation import (
    LibrarySelection, capture_library_sources, source_summary, authorize_library_binding,
)


def _matches_saved_scope(job, account, value, source, target, budget, *, offline=False,
                         source_binding=None, glossary=None):
    """PR162: bind execution/export to the exact displayed saved selection."""
    return ((account is None and offline or account is not None and account.to_dict() == job['profile'])
            and value == job['source_text'] and source == job['source_language']
            and target == job['target_language'] and money(budget) == money(job['authorization']['budget'])
            and source_binding == job.get('source_binding') and glossary == job.get('glossary'))


def create_translation_dialog(frame, *, wx_module=None, service=None):
    """Use the frame's existing cancellable worker; native controls stay on UI thread."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    library, selected_scope = frame.library, getattr(frame, 'active', None)
    root = library.root / 'state' / 'translation'
    store = ProfileStore(library.root / 'state' / 'providers')
    profiles = tuple(p for p in store.list() if p.adapter in ('google-translation-v2', 'google-translation-v3') and p.enabled)
    dialog = wx.Dialog(frame, title='Translation — selected text and linked library sources', size=(900, 940),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Translation workspace')
    layout = wx.BoxSizer(wx.VERTICAL)
    alive, busy, current_job, reviewed, binding = True, False, None, None, None
    original_display_text, displayed_text = None, None
    generation = 0
    cancel = threading.Event()

    def guard():
        if (not alive or getattr(frame, '_closing', False) or frame.library is not library
                or getattr(frame, 'active', None) != selected_scope):
            raise PermissionError('The selected library or topic changed. Reopen translation in its intended scope.')

    service = service or TranslationService(root, library=library,
        workspace=selected_scope[0] if selected_scope else None, scope_guard=guard)
    service.scope_guard = guard

    def field(label, factory=None, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = (factory or wx.TextCtrl)(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Google API &account and edition', wx.Choice,
                     choices=[f'{p.name}; {p.adapter}; {p.id}' for p in profiles])
    if profiles:
        provider.SetSelection(0)
    input_scope = field('Translation input scope', wx.Choice, choices=[
        'Selected text', 'Library chapter or reading-position range',
        'Whole library document', 'Explicit library batch (at most 16 documents)'])
    input_scope.SetSelection(0)
    title_filter = field('Source title filter for library selection', value='')
    source = field('&Source language code; auto detects', value='auto')
    target = field('Target language co&de', value='uk')
    glossary = field('Advanced v3 &glossary resource; blank for none', value='')
    selected = field('Selected te&xt; originals remain unchanged', proportion=1, style=wx.TE_MULTILINE)
    rate = field('Optional price per million characters in USD; your current contract rate', value='')
    budget = field('Maximum approved &budget in USD; no default price', value='')
    reading_view = field('Reading and narration view', wx.Choice,
                         choices=['Original', 'Translation', 'Bilingual'])
    reading_view.SetSelection(2)
    status = field('Preview, supported languages and job status', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='LOCAL ONLY until explicit approval. Choose text, an exact source range, a document or an explicit batch. '
              'Library text is read-only here; corrections belong to a separate reading copy. '
              'Configure Basic v2 API-key or Advanced v3 NMT OAuth accounts under Tools / Translation and assistant service profiles. '
              'Advanced requires the exact numeric project and region and an unexpired IAM/ADC access token in Windows Credential Manager; '
              'tokens are never automatically refreshed. An existing glossary requires the same non-global region and an explicit source language. '
              'Fetch directions for this exact account before preview. Document formatting and local translation models are unavailable.')
    buttons = {}
    for row_items in (
        [('sources', 'Choose library &sources'), ('languages', 'Fetch &languages'),
         ('preview', '&Preview'), ('run', '&Translate / resume'), ('pause', 'Pa&use')],
        [('jobs', 'Saved &jobs'), ('export', '&Export'), ('read', 'Read / &narrate derivative'), ('close', '&Close')]):
        row = wx.BoxSizer(wx.HORIZONTAL)
        for name, label in row_items:
            button = wx.Button(dialog, label=label)
            button.SetName(label.replace('&', ''))
            buttons[name] = button
            row.Add(button, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)

    def report(value, focus=False):
        if alive:
            status.ChangeValue(str(value))
            if focus:
                status.SetFocus()

    def profile(*, optional=False):
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            if optional:
                return None
            raise ValueError('Choose an enabled Google Basic v2 or Advanced v3 account in Tools / Translation and assistant service profiles.')
        return profiles[index]

    def visible(*, optional=False):
        p = profile(optional=optional)
        current = selected.GetValue()
        # wx TextCtrl can normalize CRLF on Windows. An unchanged display must
        # not rewrite the exact readonly source or a restored saved selection.
        exact = original_display_text if original_display_text is not None and current == displayed_text else current
        return (p.to_dict() if p is not None else None, exact,
                source.GetValue().strip(), target.GetValue().strip(),
                str(money(budget.GetValue())), deepcopy(binding), glossary.GetValue().strip() or None,
                rate.GetValue().strip() or None)

    def show_original(value):
        nonlocal original_display_text, displayed_text
        selected.ChangeValue(value)
        original_display_text, displayed_text = value, selected.GetValue()

    def check_visible(snapshot, *, optional=False):
        guard()
        if snapshot != visible(optional=optional):
            raise ValueError('The displayed translation selection changed. Preview or select the saved job again.')

    def controls():
        if not alive:
            return
        for control in (provider, input_scope, title_filter, source, target, glossary, selected, rate, budget, reading_view):
            control.Enable(not busy)
        selected.SetEditable(not busy and input_scope.GetSelection() == 0 and binding is None)
        for name, button in buttons.items():
            if name == 'close':
                continue
            button.Enable(busy if name == 'pause' else not busy and (
                current_job is not None if name in ('export', 'read') else
                input_scope.GetSelection() != 0 if name == 'sources' else True))

    def begin(title, operation, complete, *, allow_cancel_result=False):
        nonlocal busy
        try:
            guard()
            if busy or getattr(frame, 'working', False):
                raise RuntimeError('Another operation is active. Finish or cancel it first.')
        except Exception as exc:
            report(str(exc), True)
            return False
        busy = True
        cancel.clear()
        controls()
        def finish(result):
            nonlocal busy
            busy = False
            controls()
            if not alive:
                return
            try:
                guard()
                if cancel.is_set() and not allow_cancel_result:
                    raise InterruptedError('Operation stopped; the prior selection was retained.')
                complete(result)
            except Exception as exc:
                report(str(exc), True)
        def failed(error):
            nonlocal busy
            busy = False
            controls()
            report(str(error), True)
        try:
            if frame.run_job(title, operation, done=finish, failed=failed) is False:
                busy = False
                controls()
                return False
        except Exception as exc:
            failed(exc)
            return False
        return True

    def stopped():
        return cancel.is_set() or frame.cancel_event.is_set() or not alive

    def edited(event=None):
        nonlocal reviewed, generation
        generation += 1
        reviewed = None
        if event is not None and hasattr(event, 'Skip'):
            event.Skip()

    def scope_changed(event=None):
        nonlocal binding, current_job
        if busy:
            return
        binding, current_job = None, None
        edited(event)
        controls()
        report('Selected text mode permits a new independent text copy.' if input_scope.GetSelection() == 0 else
               'Choose library sources before previewing this input scope. No text has been sent.')

    def choose_sources(event=None):
        if busy:
            return
        try:
            guard()
            if selected_scope is None:
                raise ValueError('Open an explicit library or research topic before selecting original sources.')
            mode, query = input_scope.GetSelection(), title_filter.GetValue().strip()
            if mode not in (1, 2, 3):
                raise ValueError('Choose chapter, document or explicit batch input scope.')
            workspace, topic = selected_scope
            def unchanged():
                guard()
                if input_scope.GetSelection() != mode or title_filter.GetValue().strip() != query:
                    raise ValueError('The source chooser changed; choose the intended sources again.')
            def captured(result):
                nonlocal binding, reviewed, current_job
                unchanged()
                binding, reviewed, current_job = result.binding, None, None
                show_original(result.text)
                controls()
                report(json.dumps(source_summary(binding), ensure_ascii=False, indent=2) +
                       '\nOriginal text and positions captured. No external request was made. Preview the exact transfer next.')
            def capture(choices):
                unchanged()
                begin('Verifying selected original sources',
                    lambda: capture_library_sources(library, workspace, choices, topic=topic,
                        cancelled=stopped, guard=guard), captured)
            def ask_range(edition, total):
                unchanged()
                entry = wx.TextEntryDialog(dialog,
                    f'Choose positions from 1 to {total}, such as 2 or 2-4. These are source reading positions, '
                    'not inferred printed page labels.', 'Choose chapter or source range', f'1-{total}')
                try:
                    if entry.ShowModal() != wx.ID_OK:
                        return
                    raw = entry.GetValue().strip()
                finally:
                    entry.Destroy()
                unchanged()
                matched = re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?', raw)
                if not matched:
                    raise ValueError('Enter one source position or a contiguous range such as 2-4.')
                capture((LibrarySelection(edition, int(matched[1]), int(matched[2] or matched[1])),))
            def choose(rows):
                unchanged()
                if not rows:
                    raise ValueError('No matching source editions. Adjust the literal title filter.')
                labels = [f'{row.title or "Unknown title"}; edition {row.edition_id}' for row in rows]
                factory = wx.MultiChoiceDialog if mode == 3 else wx.SingleChoiceDialog
                picker = factory(dialog,
                    'Explicitly choose source editions. The first 100 matching editions are listed; refine the title filter if needed.',
                    'Choose translation originals', labels)
                try:
                    if picker.ShowModal() != wx.ID_OK:
                        return
                    indexes = tuple(picker.GetSelections()) if mode == 3 else (picker.GetSelection(),)
                finally:
                    picker.Destroy()
                unchanged()
                if not indexes or len(indexes) > 16 or any(not 0 <= index < len(rows) for index in indexes):
                    raise ValueError('Choose between 1 and 16 source editions.')
                editions = tuple(rows[index].edition_id for index in indexes)
                if mode == 1:
                    from ..reader.reading_desk import ReadingDesk
                    begin('Checking source reading positions',
                          lambda: len(ReadingDesk(library, workspace, editions[0], topic=topic,
                                                 cancelled=stopped).pages),
                          lambda total: ask_range(editions[0], total))
                else:
                    capture(tuple(LibrarySelection(edition) for edition in editions))
            begin('Listing authorized translation sources',
                  lambda: library.browse(workspace, title=query or None, limit=100), choose)
        except Exception as exc:
            report(str(exc), True)

    def languages(event=None):
        if busy:
            return
        try:
            guard()
            p = profile()
            if wx.MessageBox('Contact the official Google Translation API to fetch its supported language list? '
                'No selected text is sent. Your configured account will be used.\n' + p.endpoint + '\nModel: ' + p.model, 'Approve language discovery',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            guard()
            if p != profile():
                raise ValueError('The selected provider changed; review the language request again.')
            identity = uuid4().hex
            approval = Authorization(identity, identity, ((p.id, p.endpoint),), ('translation.languages',),
                                      ('user-instruction',), '0', 1, time() + 600)
            service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
            begin('Fetching supported translation languages',
                  lambda: service.refresh_languages(p, authorization=approval, cancelled=stopped),
                  lambda rows: report('Provider-declared NMT languages; translation quality is not independently qualified:\n' +
                                      '\n'.join(row['language'] + ' — ' + row['name'] +
                                          ('; source: ' + str(row['source']) + '; target: ' + str(row['target'])
                                           if 'source' in row else '') for row in rows), True))
        except Exception as exc:
            report(str(exc), True)

    def preview(event=None):
        nonlocal reviewed
        if busy:
            return
        try:
            guard()
            p, snapshot, price = profile(), visible(), rate.GetValue().strip() or None
            if input_scope.GetSelection() != 0 and binding is None:
                raise ValueError('Capture the selected library sources before preview.')
            captured_generation = generation
            def complete(result):
                nonlocal reviewed, current_job
                check_visible(snapshot)
                if generation != captured_generation or rate.GetValue().strip() != (price or ''):
                    raise ValueError('The preview options changed; preview the exact current selection again.')
                if result['estimated_cost'] is not None and money(result['estimated_cost']) > money(snapshot[4]):
                    raise ValueError('The owner-entered price estimate exceeds this total budget.')
                reviewed, current_job = snapshot, None
                controls()
                report(json.dumps(result, ensure_ascii=False, indent=2) +
                       '\nNo request sent. Translate asks consent for these exact characters, source identities, '
                       'provider, languages and total budget. The reservation is not a provider-enforced charge guarantee.')
            begin('Previewing exact translation transfer',
                  lambda: service.preview(p, snapshot[1], snapshot[2], snapshot[3],
                      rate_per_million=price, source_binding=snapshot[5], glossary=snapshot[6], cancelled=stopped), complete)
        except Exception as exc:
            reviewed = None
            report(str(exc), True)

    def saved_job(snapshot, identity, *, offline=False):
        job = service.get(identity, cancelled=stopped)
        account = None if snapshot[0] is None else next(
            (p for p in profiles if p.to_dict() == snapshot[0]), None)
        if not _matches_saved_scope(job, account, snapshot[1], snapshot[2], snapshot[3], snapshot[4],
                                    offline=offline, source_binding=snapshot[5], glossary=snapshot[6]):
            raise ValueError('Visible settings differ from saved job ' + identity +
                '. Select Saved jobs to restore its exact scope, or Preview for a new approval. No operation was started.')
        return job

    def run(event=None):
        nonlocal current_job
        if busy:
            return
        try:
            guard()
            p, snapshot = profile(), visible()
            identity = current_job
            if identity is None:
                if reviewed != snapshot:
                    raise ValueError('Preview this exact text, source scope, provider, languages and budget first.')
                details = (f"{len(snapshot[5]['sources'])} library sources; " if snapshot[5] else 'selected text; ')
                if wx.MessageBox(f'Send exactly {len(snapshot[1])} characters ({details}) to Google Translation? '
                    f'Approved total reservation: {snapshot[4]} USD. The provider may charge under your terms. '
                    'A linked derivative is created; originals and quotations remain unchanged.\n' + p.endpoint +
                    '\nModel: ' + p.model + '\nGlossary: ' + (snapshot[6] or 'none'),
                    'Approve exact translation transfer', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                check_visible(snapshot)
                if stopped():
                    raise InterruptedError('Translation approval was cancelled.')
            selected_generation = generation
            def translate():
                guard()
                if generation != selected_generation:
                    raise ValueError('The displayed translation changed before execution.')
                if store.get(p.id).to_dict() != p.to_dict():
                    raise ValueError('The selected provider account changed. Reopen and approve the current account.')
                service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
                job_id = identity
                if job_id is None:
                    approval, cost = translation_approval(p, snapshot[1], snapshot[2], snapshot[3],
                                                         snapshot[4], source_binding=snapshot[5], glossary=snapshot[6])
                    job_id = service.submit(p, snapshot[1], snapshot[2], snapshot[3],
                        authorization=approval, per_call_cost=cost, source_binding=snapshot[5],
                        glossary=snapshot[6], cancelled=stopped)
                else:
                    saved_job(snapshot, job_id)
                progress = getattr(frame, 'operation_progress', None)
                token = progress.event.token if progress is not None else None
                def advanced(done, total, phase):
                    if progress is not None:
                        progress.update(done, total, phase, token=token)
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
                return service.resume(job_id, cancelled=stopped, progress=advanced)
            def completed(result):
                nonlocal current_job
                current_job = result['id']
                controls()
                report(json.dumps(result, indent=2) +
                       '\nAccepted chunks are retained. Export or open the original/translation/bilingual reading view.', True)
            begin('Translating the explicitly approved selection', translate, completed, allow_cancel_result=True)
        except Exception as exc:
            report(str(exc), True)

    def jobs(event=None):
        if busy:
            return
        def restore(job):
            nonlocal current_job, reviewed, binding
            index = next((i for i, p in enumerate(profiles) if p.to_dict() == job['profile']), -1)
            provider.SetSelection(index)
            show_original(job['source_text'])
            source.ChangeValue(job['source_language'])
            target.ChangeValue(job['target_language'])
            glossary.ChangeValue(job.get('glossary') or '')
            budget.ChangeValue(str(job['authorization']['budget']))
            rate.ChangeValue('')
            binding = deepcopy(job.get('source_binding'))
            input_scope.SetSelection(0 if binding is None else
                3 if len(binding['sources']) > 1 else 2 if binding['sources'][0]['selection'] == 'document' else 1)
            current_job, reviewed = job['id'], None
            controls()
            report(f"Saved job {job['id']}; {job['status']}; account {job['profile']['name']} ({job['profile']['id']}); "
                   f"{job['source_language']} to {job['target_language']}; "
                   f"{len(job['source_text'])} characters; budget {job['authorization']['budget']} USD. " +
                   ('Account unavailable or changed: remote resume is blocked; completed output remains readable offline. '
                    if index < 0 else 'Resume uses the exact original approval and accepted chunks. ') +
                   'An uncertain charge is never retried automatically. Model: ' + job['profile']['model'] +
                   '; glossary: ' + (job.get('glossary') or 'none') + '. '
                   'Expired Advanced access tokens require explicit re-entry for the unchanged account.', True)
        def choose(rows):
            picker = wx.SingleChoiceDialog(dialog, 'Choose a saved exact translation scope.',
                'Translation jobs', [
                    f"{j['title']}; {j['status']}; {len(j['receipts'])}/{len(j['chunks'])}; "
                    f"{j['source_language']} to {j['target_language']}; account {j['profile']['name']} "
                    f"({j['profile']['id']}); budget {j['authorization']['budget']} USD; job {j['id']}" for j in rows])
            try:
                if picker.ShowModal() != wx.ID_OK or not 0 <= picker.GetSelection() < len(rows):
                    return
                identity = rows[picker.GetSelection()]['id']
            finally:
                picker.Destroy()
            guard()
            begin('Validating saved translation selection', lambda: service.get(identity, cancelled=stopped), restore)
        begin('Loading saved translation jobs', service.list, choose)

    def export(event=None):
        if busy or current_job is None:
            return
        try:
            snapshot, identity = visible(optional=True), current_job
            def choose(job):
                check_visible(snapshot, optional=True)
                picker = wx.DirDialog(dialog, 'Choose parent folder for a NEW export of translation job ' + identity)
                try:
                    if picker.ShowModal() != wx.ID_OK:
                        return
                    destination = Path(picker.GetPath()) / ('Translation-' + identity[:8] + '-' + uuid4().hex[:8])
                finally:
                    picker.Destroy()
                check_visible(snapshot, optional=True)
                if stopped():
                    raise InterruptedError('Translation export cancelled.')
                begin('Exporting verified translation derivative',
                    lambda: service.export(identity, destination, cancelled=stopped),
                    lambda result: report('Saved new files in: ' + result['path']))
            begin('Validating exact translation export scope',
                  lambda: saved_job(snapshot, identity, offline=True), choose)
        except Exception as exc:
            report(str(exc), True)

    def read(event=None):
        if busy or current_job is None:
            return
        try:
            snapshot, identity = visible(optional=True), current_job
            view = ('original', 'translation', 'bilingual')[reading_view.GetSelection()]
            def open_reader(job):
                check_visible(snapshot, optional=True)
                from .speech_dialog import show_speech_reader
                def reading_guard():
                    guard()
                    if job.get('source_binding') is not None:
                        authorize_library_binding(library, job['source_binding'])
                show_speech_reader(frame, title=job['title'], source_guard=reading_guard,
                    recovery_slot='translation/' + identity + '/' + view,
                    source_loader=lambda cancelled: service.reading_source(identity, view=view, cancelled=cancelled))
            begin('Validating linked translation reading',
                  lambda: saved_job(snapshot, identity, offline=True), open_reader)
        except Exception as exc:
            report(str(exc), True)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        alive = False
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)
        else:
            dialog.Hide()

    handlers = {'sources': choose_sources, 'languages': languages, 'preview': preview, 'run': run,
                'pause': lambda event=None: cancel.set(), 'jobs': jobs, 'export': export, 'read': read, 'close': close}
    for name, handler in handlers.items():
        buttons[name].Bind(wx.EVT_BUTTON, handler)
        setattr(dialog, 'on_' + name, handler)
    for control in (selected, source, target, glossary, budget, rate):
        control.Bind(wx.EVT_TEXT, edited)
    provider.Bind(wx.EVT_CHOICE, edited)
    input_scope.Bind(wx.EVT_CHOICE, scope_changed)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.translation_service = service
    dialog.translation_controls = dict(provider=provider, input_scope=input_scope, title_filter=title_filter,
        source=source, target=target, glossary=glossary, selected=selected, rate=rate, budget=budget,
        reading_view=reading_view, status=status, **buttons)
    dialog.translation_cancel = cancel
    dialog.on_scope_changed = scope_changed
    controls()
    return dialog


def show_translation(frame):
    dialog = create_translation_dialog(frame)
    try:
        dialog.ShowModal()
    finally:
        dialog.on_close()
        dialog.Destroy()


def install_translation_actions(frame):
    import wx
    bar = frame.menu_bar
    menu = next((bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise RuntimeError('Install the shared Tools menu before translation actions.')
    identity = wx.NewIdRef()
    menu.Append(identity, 'Translate text, chapters and documents...')
    frame.Bind(wx.EVT_MENU, lambda event: show_translation(frame), id=int(identity))

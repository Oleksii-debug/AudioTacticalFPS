"""Native explicit plain-text translation workspace on the shared provider gateway."""

import json
from pathlib import Path
import threading
from time import time
from uuid import uuid4

from ..assistant.profiles import ProfileStore
from ..assistant.providers import Authorization, Gateway, money
from ..assistant.translation import TranslationService, translation_approval


def _matches_saved_scope(job, account, value, source, target, budget, glossary=None, *, offline=False):
    """Compare displayed values to a validated durable job, not a prior preview."""
    return ((account is None and offline or account is not None and account.to_dict() == job['profile'])
            and value == job['source_text'] and source == job['source_language']
            and target == job['target_language'] and money(budget) == money(job['authorization']['budget'])
            and glossary == job.get('glossary'))


def source_range(value, total):
    """Reading positions are explicit; no inferred chapter or printed labels."""
    import re
    match = re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?', value.strip())
    if match is None:
        raise ValueError('Enter a source position or range such as 1-3.')
    first, last = int(match[1]), int(match[2] or match[1])
    if not 1 <= first <= last <= total:
        raise ValueError('The selected source range is outside this book.')
    return first, last


def _origin(document):
    if document is None:
        return ('Pasted selected text; no retained-library locator. Plain text only; '
                'source and translated reading copies each support up to 2,000,000 characters.')
    rows = document.source_anchor['positions']
    return (f'{document.title}; source reading positions {rows[0]["ordinal"]}-{rows[-1]["ordinal"]} '
            f'({len(rows)} positions); {len(document.source_text)} characters. '
            'Original bytes and source positions remain unchanged. Translation is a derived reading copy, '
            'not quotation evidence. Plain text only; up to 2,000,000 source and translated characters.')


def choose_narration_voices(parent, choices, *, guard=lambda: None):
    """Review two exact installed-voice declarations without an automatic default."""
    import wx
    if not choices['original'] or not choices['translation']:
        raise ValueError('Bilingual narration needs installed voices declaring exactly '
                         + choices['source_language'] + ' and ' + choices['target_language']
                         + '. No regional, script or language fallback is inferred.')
    dialog = wx.Dialog(parent, title='Bilingual narration — choose both installed voices')
    owner_id, alive = dialog.GetId(), True

    def destroyed(event):
        nonlocal alive
        if event.GetId() == owner_id:
            alive = False
        event.Skip()

    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    layout = wx.BoxSizer(wx.VERTICAL)
    description = ('This creates a new Studio project only. Original text and translation are paired by '
                   'request chunk, not aligned sentences. Voice languages are adapter declarations; '
                   'pronunciation quality is not qualified. Open the exported JSON in Audio Studio, '
                   'review roles, then explicitly Render. No speech or provider request starts here.')
    layout.Add(wx.StaticText(dialog, label=description), 0, wx.EXPAND | wx.ALL, 6)
    controls = []
    for key, label, language in (('original', 'Original narration voice', choices['source_language']),
                                 ('translation', 'Translation narration voice', choices['target_language'])):
        name = label + ' (' + language + ')'
        layout.Add(wx.StaticText(dialog, label=name), 0, wx.ALL, 6)
        records = choices[key]
        control = wx.Choice(dialog, choices=[record['voice']['name'] + '; '
            + record['voice']['language'] + '; ' + record['voice']['engine'] + '; '
            + record['voice']['license'] for record in records])
        control.SetName(name)
        control.SetSelection(wx.NOT_FOUND)
        controls.append(control)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 6)
    layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 6)
    dialog.SetSizerAndFit(layout)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        guard()
        if not alive:
            raise InterruptedError('The bilingual voice selection was closed.')
        selected = tuple(control.GetSelection() for control in controls)
        if any(not 0 <= index < len(choices[key])
               for index, key in zip(selected, ('original', 'translation'))):
            raise ValueError('Explicitly choose both voices; no default voice has been selected.')
        return tuple(choices[key][index] for index, key in zip(selected, ('original', 'translation')))
    finally:
        if alive:
            dialog.Destroy()


def show_translation(frame, *, source_loader=None, source_guard=lambda: None, source_topic=None):
    import wx
    from ..speech.reading import ReadingDocument

    library = frame.library
    active = getattr(frame, 'active', None)
    root = library.root / 'state' / 'translation'
    service = TranslationService(root, library=library)
    accounts = ProfileStore(library.root / 'state' / 'providers')
    profiles = tuple(p for p in accounts.list()
                     if p.adapter in ('google-translation-v2', 'google-translation-v3') and p.enabled)
    dialog = wx.Dialog(frame, title='Translation — selected text derivatives', size=(850, 900),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Translation workspace')
    owner_id = dialog.GetId()
    layout = wx.BoxSizer(wx.VERTICAL)
    alive, busy, current_job, reviewed = True, False, None, None
    native_destroyed = False
    source_ready = source_loader is None
    original_document = document = None
    cancel = threading.Event()

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Google API &account and edition', wx.Choice,
                     choices=[f'{p.name} ({p.id}); {p.adapter}' for p in profiles])
    if profiles:
        provider.SetSelection(0)
    source = field('&Source language code; auto detects', value='auto')
    target = field('Target language co&de', value='uk')
    glossary = field('Advanced v3 &glossary resource; blank for none', value='')
    origin = field('Source selection and limits', style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value=_origin(None) if source_ready else 'Loading the selected authorized source. No text is sent.')
    selected = field('Selected te&xt; originals remain unchanged', proportion=1, style=wx.TE_MULTILINE)
    selected.SetEditable(source_loader is None)
    rate = field('Optional price per million characters in USD; your current contract rate', value='')
    budget = field('Maximum approved &budget in USD; no default price', value='')
    status = field('Preview, supported languages and job status', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='LOCAL ONLY until explicit approval. Configure a Basic v2 API-key account or Advanced v3 NMT OAuth account '
              'in AI and API connections. Advanced requires your numeric project and region, and an unexpired IAM/ADC '
              'access token in Windows Credential Manager; tokens are never automatically refreshed. '
              'An existing glossary must share the project and non-global region; choose an explicit source language. '
              'Fetch languages for the exact account before preview. Document formatting and offline translation remain unavailable.')
    buttons = {}
    row = wx.BoxSizer(wx.HORIZONTAL)
    for name, label in [('languages', 'Fetch &languages'), ('preview', '&Preview'), ('run', '&Translate / resume'),
                        ('pause', 'Pa&use'), ('jobs', 'Saved &jobs'), ('new', '&New translation'),
                        ('export', '&Export'), ('narration', 'Bilingual &audio project'), ('close', '&Close')]:
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

    def guard():
        if not alive:
            raise InterruptedError('The translation window was closed.')
        if (frame.library is not library or getattr(frame, '_closing', False)
                or (active is not None and frame.active != active)):
            raise ValueError('The active library or research topic changed. Reopen translation.')
        source_guard()

    def stopped():
        return not alive or cancel.is_set() or frame.cancel_event.is_set()

    def profile():
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Create and enable a Google Basic v2 or Advanced v3 account in AI/API connections, then reopen this window.')
        return profiles[index]

    def visible_scope():
        # Read wx controls only on the UI thread; validation and disk reads run
        # in the existing background operation before any gateway is created.
        index = provider.GetSelection()
        account = profiles[index] if 0 <= index < len(profiles) else None
        return (account, selected.GetValue(), source.GetValue().strip(),
                target.GetValue().strip(), budget.GetValue(), glossary.GetValue().strip() or None)

    def require_current_account(account):
        try:
            current = accounts.get(account.id)
        except (OSError, ValueError):
            current = None
        if current is None or not current.enabled or current.to_dict() != account.to_dict():
            raise ValueError('The selected account is unavailable, disabled or changed. '
                             'Reopen translation after reviewing AI/API connections. '
                             'Completed saved work can still be exported offline.')

    def check_saved_scope(identity, values, *, offline=False):
        job = service.get(identity)
        if not _matches_saved_scope(job, *values, offline=offline):
            raise ValueError('Visible settings differ from saved job ' + job['id'] +
                             '. Select Saved jobs to restore its exact scope, or choose New translation. '
                             'No operation on the saved job was started.')
        if not offline:
            require_current_account(values[0])
        return job

    def controls():
        if not alive:
            return
        editing = not busy and source_ready and current_job is None
        for control in (provider, source, target, glossary, rate, budget):
            control.Enable(editing)
        selected.Enable(not busy and source_ready)
        selected.SetEditable(editing and document is None)
        for name in ('languages', 'preview'):
            buttons[name].Enable(editing)
        buttons['run'].Enable(not busy and source_ready)
        buttons['jobs'].Enable(not busy)
        buttons['new'].Enable(not busy and source_ready)
        buttons['export'].Enable(not busy and current_job is not None)
        buttons['narration'].Enable(not busy and current_job is not None and document is not None)
        buttons['pause'].Enable(busy)

    def begin(title, operation, complete):
        nonlocal busy
        if busy:
            return
        guard()
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            report('Another operation is active. Finish or cancel it before starting translation.', True)
            return
        busy = True
        cancel.clear()
        controls()
        def finish(result):
            nonlocal busy
            busy = False
            controls()
            if alive:
                complete(result)
        def failed(error):
            nonlocal busy
            busy = False
            controls()
            report(str(error), True)
        def checked():
            guard()
            if stopped():
                raise InterruptedError('Translation cancelled before starting.')
            return operation()
        if frame.run_job(title, checked, done=finish, failed=failed) is False:
            busy = False
            controls()

    def languages(event):
        try:
            guard()
            p = profile()
            if wx.MessageBox('Contact the official Google Translation API to fetch its supported language list? '
                'No selected text is sent. Your configured account will be used.\n' + p.endpoint + '\nModel: ' + p.model,
                'Approve language discovery',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            guard()
            identity = uuid4().hex
            approval = Authorization(identity, identity, ((p.id, p.endpoint),), ('translation.languages',),
                                      ('user-instruction',), '0', 1, time() + 600)
            def discover():
                require_current_account(p)
                def discovery_stopped():
                    guard()
                    require_current_account(p)
                    return stopped()
                service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
                return service.refresh_languages(p, authorization=approval, cancelled=discovery_stopped)
            begin('Fetching supported translation languages', discover,
                  lambda rows: report('Provider-declared NMT languages; no quality or 135-language qualification claim:\n' +
                      '\n'.join(row['language'] + ' — ' + row['name'] +
                          ('; source: ' + str(row['source']) + '; target: ' + str(row['target']) if 'source' in row else '')
                          for row in rows), True))
        except Exception as exc:
            report(str(exc), True)

    def preview(event=None):
        nonlocal reviewed
        try:
            guard()
            if not source_ready or current_job is not None:
                raise ValueError('Start a new translation and wait for its source before previewing.')
            p = profile()
            result = service.preview(p, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(),
                                     rate_per_million=rate.GetValue().strip() or None,
                                     glossary=glossary.GetValue().strip() or None)
            total = money(budget.GetValue())
            if result['estimated_cost'] is not None and money(result['estimated_cost']) > total:
                raise ValueError('The entered price estimate exceeds the chosen total budget.')
            reviewed = (p.to_dict(), selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(total),
                        glossary.GetValue().strip() or None, rate.GetValue().strip() or None)
            report(json.dumps(result, ensure_ascii=False, indent=2) +
                   '\n\nNo request sent. Translate asks permission for this exact selected text and total budget. '
                   'The budget is a local reservation bound, not a provider-enforced billing guarantee.', True)
        except Exception as exc:
            reviewed = None
            report(str(exc), True)

    def run(event):
        nonlocal current_job
        try:
            guard()
            submission = None
            saved_values = visible_scope() if current_job is not None else None
            if current_job is None:
                p = profile()
                values = (p.to_dict(), selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(money(budget.GetValue())),
                          glossary.GetValue().strip() or None, rate.GetValue().strip() or None)
                if not source_ready or reviewed != values:
                    raise ValueError('Preview this exact text, languages and budget before approving translation.')
                if wx.MessageBox(f'Send exactly {len(values[1])} selected characters to Google Translation?\n'
                    + _origin(document) + f'\nAccount: {p.name} ({p.id}); {p.adapter}\n{p.endpoint}\nModel: {p.model}'
                    + f'\nGlossary: {values[5] or "none"}; case-sensitive when selected. '
                    + f'\nLanguages: {values[2]} to {values[3]}\nApproved total reservation: {values[4]} USD. '
                    'The provider may charge your account under its terms.',
                    'Approve selected text transfer', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                guard()
                authorization, bound = translation_approval(p, values[1], values[2], values[3], values[4],
                                                            source_bound=document is not None, glossary=values[5])
                submission = (p, values, authorization, bound, document)
            def translate():
                nonlocal current_job
                if submission is not None:
                    p, values, authorization, bound, source_document = submission
                    require_current_account(p)
                    current_job = service.submit(p, values[1], values[2], values[3],
                        title=source_document.title if source_document is not None else 'Translation',
                        authorization=authorization, per_call_cost=bound, source_document=source_document,
                        topic=source_topic if source_document is not None else None, glossary=values[5],
                        cancelled=stopped, guard=guard)
                    account = p
                else:
                    check_saved_scope(current_job, saved_values)
                    account = saved_values[0]
                def remote_guard():
                    guard()
                    require_current_account(account)
                remote_guard()
                service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
                progress = getattr(frame, 'operation_progress', None)
                token = progress.event.token if progress is not None else None
                def advanced(done, total, phase):
                    if progress is not None:
                        progress.update(completed=done, total=total, phase=phase, token=token)
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
                return service.resume(current_job, cancelled=stopped, progress=advanced, guard=remote_guard)
            begin('Translating approved selected text', translate,
                  lambda result: report(json.dumps(result, indent=2) +
                      '\nExport writes new original, translation, bilingual and reading-copy files.', True))
        except Exception as exc:
            report(str(exc), True)

    def choose_job(rows):
        nonlocal current_job, document, source_ready, reviewed
        try:
            guard()
            chooser = wx.SingleChoiceDialog(dialog, 'Choose a saved approved translation. Its original scope and budget remain binding.',
                'Translation jobs', [f"{j['title']}; {j['status']}; {len(j['receipts'])}/{len(j['chunks'])}; "
                    f"{j['source_language']} to {j['target_language']}; account {j['profile']['name']} "
                    f"({j['profile']['id']}); budget {j['authorization']['budget']} USD; job {j['id']}" for j in rows])
            try:
                if chooser.ShowModal() == wx.ID_OK and 0 <= chooser.GetSelection() < len(rows):
                    guard()
                    job = rows[chooser.GetSelection()]
                    binding = job.get('source_binding')
                    document = (ReadingDocument(job['source_text'], title=job['title'], source_anchor=binding['anchor'])
                                if binding else None)
                    current_job, reviewed, source_ready = job['id'], None, True
                    selected.ChangeValue(job['source_text'])
                    source.ChangeValue(job['source_language'])
                    target.ChangeValue(job['target_language'])
                    glossary.ChangeValue(job.get('glossary') or '')
                    budget.ChangeValue(job['authorization']['budget'])
                    rate.ChangeValue('')
                    index = next((i for i, p in enumerate(profiles) if p.to_dict() == job['profile']), -1)
                    provider.SetSelection(index)
                    origin.ChangeValue(_origin(document))
                    controls()
                    account_status = ('Saved account is unavailable or changed in this window; remote resume is blocked. '
                        'Completed work can still be exported offline. ' if index < 0 else
                        'Resume requires the unchanged enabled account and reuses the original authorization. ')
                    report(f"Saved job {job['id']}; {job['status']}; account {job['profile']['name']} "
                        f"({job['profile']['id']}); {job['profile']['adapter']}; model {job['profile']['model']}; "
                        f"glossary {job.get('glossary') or 'none'}; {job['source_language']} to {job['target_language']}; "
                        f"{len(job['source_text'])} characters; budget {job['authorization']['budget']} USD. "
                        + account_status + 'Source authorization is checked again. Uncertain charges are never retried automatically. '
                        'Choose New translation for different text, languages or approval.', True)
            finally:
                chooser.Destroy()
        except Exception as exc:
            report(str(exc), True)

    def jobs(event):
        try:
            begin('Reading saved translation jobs', service.list, choose_job)
        except Exception as exc:
            report(str(exc), True)

    def new(event):
        nonlocal current_job, reviewed, document
        try:
            guard()
            current_job = reviewed = None
            document = original_document
            selected.ChangeValue(document.source_text if document is not None else '')
            origin.ChangeValue(_origin(document))
            source.ChangeValue('auto')
            target.ChangeValue('uk')
            glossary.ChangeValue('')
            budget.ChangeValue('')
            report('Choose languages and a budget, then preview and approve this new translation. No request sent.')
            controls()
            selected.SetFocus()
        except Exception as exc:
            report(str(exc), True)

    def export(event, *, narration_voices=None):
        if current_job is None:
            return
        values = visible_scope()
        chooser = wx.DirDialog(dialog, 'Choose parent folder for a NEW translation export of job ' + current_job)
        try:
            if chooser.ShowModal() == wx.ID_OK:
                guard()
                destination = Path(chooser.GetPath()) / ('Translation-' + current_job[:8] + '-' + uuid4().hex[:8])
                identity = current_job
                def publish():
                    check_saved_scope(identity, values, offline=True)
                    options = {} if narration_voices is None else {'narration_voices': narration_voices}
                    return service.export(identity, destination, cancelled=stopped, guard=guard, **options)
                begin('Exporting translation derivative',
                      publish,
                      lambda result: report('Saved new files in: ' + result['path']
                          + ('\nOpen bilingual.studio.json in Audio Studio, review voices, then Render. '
                             'No speech has run.' if narration_voices is not None else '')))
        except Exception as exc:
            report(str(exc), True)
        finally:
            chooser.Destroy()

    def narration(event):
        if current_job is None:
            return
        identity, values = current_job, visible_scope()

        def inspect():
            check_saved_scope(identity, values, offline=True)
            return service.narration_choices(identity, cancelled=stopped, guard=guard)

        def choose(choices):
            try:
                guard()
                if current_job != identity:
                    raise ValueError('The selected translation changed. Choose its narration voices again.')
                selected_voices = choose_narration_voices(dialog, choices, guard=guard)
                if selected_voices is not None:
                    guard()
                    if current_job != identity:
                        raise ValueError('The selected translation changed before narration export.')
                    export(None, narration_voices=selected_voices)
            except Exception as exc:
                report(str(exc), True)

        try:
            begin('Reviewing installed bilingual narration voices', inspect, choose)
        except Exception as exc:
            report(str(exc), True)

    def loaded(retained):
        nonlocal document, original_document, source_ready
        try:
            guard()
            original_document = document = retained.reading_document()
            source_ready = True
            selected.ChangeValue(document.source_text)
            origin.ChangeValue(_origin(document))
            report((' '.join(retained.warnings[:3]) + '\n' if retained.warnings else '') +
                   'Source loaded locally. Review the selected text and languages, then preview and approve the transfer.')
            controls()
            selected.SetFocus()
        except Exception as exc:
            report(str(exc), True)

    def load_source():
        if alive:
            try:
                begin('Loading authorized translation source', lambda: source_loader(stopped), loaded)
            except Exception as exc:
                report(str(exc), True)

    def detach():
        nonlocal alive
        cancel.set()
        alive = False

    def close(event=None):
        if not alive:
            return
        detach()
        dialog.EndModal(wx.ID_CLOSE)

    def destroyed(event):
        nonlocal native_destroyed
        if event.GetId() == owner_id:
            # Parent destruction can bypass EVT_CLOSE and may supply another
            # wx wrapper. Detach plain worker state without touching the owner.
            native_destroyed = True
            detach()
        event.Skip()

    for name, handler in {'languages': languages, 'preview': preview, 'run': run, 'pause': lambda e: cancel.set(),
                           'jobs': jobs, 'new': new, 'export': export, 'narration': narration, 'close': close}.items():
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    controls()
    if source_loader is not None:
        wx.CallAfter(load_source)
    try:
        dialog.ShowModal()
    finally:
        detach()
        if not native_destroyed:
            dialog.Destroy()


def install_translation_actions(frame):
    import wx
    from .reading_desk import ReadingActions
    from ..speech.source import read_source_range

    bar = frame.menu_bar
    menu = next((bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise RuntimeError('Install the shared Tools menu before translation actions.')
    identity = wx.NewIdRef()
    item = menu.Append(identity, 'Translate selected te&xt...')
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('tools.translation', 'Translate selected text'), item)
    frame.Bind(wx.EVT_MENU, lambda event: show_translation(frame), id=int(identity))

    class BookTranslation:
        def __init__(self, parent, desk, state, guard):
            self.parent, self.desk, self.guard = parent, desk, guard

        def show(self):
            self.guard()
            total = len(self.desk.pages)
            with wx.TextEntryDialog(self.parent,
                f'Choose source reading positions from 1 to {total}. Enter a position or first-last. '
                'The full range selects this document. Positions are not inferred chapter or printed-page labels. '
                'Up to 2,000,000 plain-text characters; formatting is not retained.',
                'Translate a book or source range', f'1-{total}') as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                first, last = source_range(chooser.GetValue(), total)
            self.guard()
            show_translation(self.parent, source_guard=self.guard, source_topic=self.desk.topic,
                source_loader=lambda cancelled: read_source_range(self.desk, first, last,
                    guard=self.guard, cancelled=cancelled))

    actions = ReadingActions(frame, reader_factory=BookTranslation)
    identity = wx.NewIdRef()
    menu.Append(identity, 'Translate selected &book or source range...')
    frame.Bind(wx.EVT_MENU, actions.open_book, id=int(identity))
    frame.translation_reading_actions = actions

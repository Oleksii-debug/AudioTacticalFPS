"""Native review/approval workspace for Basic v2 and Advanced v3 derivatives."""
from decimal import Decimal
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..assistant.translation import (
    TranslationService, language_approval, translation_approval)
from ..assistant.translation_state import MAX_SOURCE_CHARACTERS
from ..assistant.translation_structure import capture_text, source_text, structure_hash
from .api_connections import parse_budget


class TranslationController:
    """Preview is local; only a separate exact-data approval creates a job."""
    def __init__(self, root, *, service=None):
        self.service = service or TranslationService(root)
        if self.service.root != Path(root).absolute():
            raise ValueError('Translation belongs to the selected library root.')
        self.reviewed = None

    @staticmethod
    def selection(profile, value, source, target, budget, title, rate, glossary='', structure=None):
        return (profile.fingerprint, value, source, target, parse_budget(budget), title, rate, glossary,
                None if structure is None else structure_hash(structure))

    def preview(self, profile, value, source, target, budget, *, title='Translation', rate='', glossary='', structure=None):
        self.reviewed = None
        chosen = self.selection(profile, value, source, target, budget, title, rate, glossary, structure)
        options = {'glossary': glossary} if glossary or profile.provider == 'google-translation-v3' else {}
        if structure is not None:
            options['structure'] = structure
        result = self.service.preview(profile, value, source, target, rate_per_million=rate or None, **options)
        if result['estimated_cost'] is not None and Decimal(result['estimated_cost']) * 1000000 > chosen[4]:
            raise ValueError('The entered price estimate exceeds the chosen reservation budget.')
        self.reviewed = chosen
        return {**result, 'budget_micros': chosen[4], 'currency': 'USD',
                'provider_name': profile.name, 'endpoint': profile.endpoint, 'model': profile.model}

    def submit(self, profile, value, source, target, budget, *, approved=False, title='Translation', rate='',
               glossary='', structure=None, cancelled=None):
        chosen = self.selection(profile, value, source, target, budget, title, rate, glossary, structure)
        if approved is not True or chosen != self.reviewed:
            raise PermissionError('Preview and explicitly approve this exact account, text, pair and budget.')
        approval = translation_approval(profile, value, source, target, chosen[4], glossary=glossary, structure=structure)
        options = {'glossary': glossary} if glossary or profile.provider == 'google-translation-v3' else {}
        if structure is not None:
            options['structure'] = structure
        if cancelled is not None:
            options['cancelled'] = cancelled
        identity = self.service.submit(profile, value, source, target, approval=approval, title=title, **options)
        self.reviewed = None
        return identity


def show_translation(frame, *, initial_text='', title='Translation', service=None):
    import wx
    if (not isinstance(initial_text, str) or len(initial_text) > MAX_SOURCE_CHARACTERS
            or not isinstance(title, str) or not title.strip() or len(title) > 512):
        raise ValueError('Choose bounded selected text and a translation title.')
    controller = TranslationController(frame.library.root, service=service)
    service = controller.service
    scoped_library = frame.library
    profiles, source_codes, target_codes = [], [], []
    alive, busy, current_job = True, False, None
    structured_source = None
    cancel = threading.Event()
    dialog = wx.Dialog(frame, title='Translation — selected text derivatives', size=(900, 900),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Translation workspace')
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Google Translation &account; Basic v2 or Advanced v3', wx.Choice, choices=[])
    job_title = field('Translation &title', value=title)
    source = field('&Source language', wx.Choice, choices=[])
    target = field('Target &language', wx.Choice, choices=[])
    glossary = field('Advanced v3 &glossary resource; blank for none', value='')
    glossary.SetMaxLength(512)
    selected = field('Selected te&xt; originals remain unchanged', proportion=1,
                     value=initial_text, style=wx.TE_MULTILINE)
    selected.SetMaxLength(MAX_SOURCE_CHARACTERS)
    source_buttons = {}
    source_row = wx.WrapSizer(wx.HORIZONTAL)
    for key, label in (('documents', 'Choose documents or chapters...'),
                       ('structure', 'Preserve paragraph or Markdown structure...'),
                       ('editable', 'Use editable selected text')):
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        source_row.Add(button, 0, wx.ALL, 3)
        source_buttons[key] = button
    layout.Add(source_row, 0, wx.EXPAND)
    rate = field('Optional current contract price per million characters in USD', value='')
    budget = field('Maximum approved reservation in USD; no default price', value='')
    result_view = field('Accepted translation derivative; not a source quotation', proportion=1,
                        style=wx.TE_MULTILINE | wx.TE_READONLY)
    status = field('Preview, language inventory and charge status', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY)
    buttons = {}
    for entries in (
            [('settings', 'AI/API &connections'), ('languages', '&Fetch languages'),
             ('preview', '&Preview'), ('run', 'T&ranslate / resume'), ('pause', 'Pa&use')],
            [('new', '&New translation'), ('jobs', 'Saved &jobs'), ('charges', 'Charge &history'),
             ('export', '&Export derivative'), ('close', 'Cl&ose')]):
        row = wx.BoxSizer(wx.HORIZONTAL)
        for name, label in entries:
            button = wx.Button(dialog, id=wx.ID_CLOSE if name == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            buttons[name] = button
            row.Add(button, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CLOSE)

    def report(value):
        if alive:
            status.ChangeValue(str(value))

    def profile():
        if frame.library is not scoped_library or Path(frame.library.root).absolute() != service.root:
            raise ValueError('The selected library changed. Reopen translation before reviewing a request.')
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Create a connected Google Basic v2 or Advanced v3 profile in AI/API connections.')
        return profiles[index]

    def pair():
        first, last = source.GetSelection(), target.GetSelection()
        if not 0 <= first < len(source_codes) or not 0 <= last < len(target_codes):
            raise ValueError('Explicitly fetch and choose a supported source/target language pair.')
        return source_codes[first], target_codes[last]

    def controls():
        if not alive:
            return
        editable = not busy and current_job is None
        for control in (provider, job_title, source, target, selected, rate, budget):
            control.Enable(editable)
        for control in source_buttons.values():
            control.Enable(editable)
        selected.SetEditable(editable and structured_source is None)
        index = provider.GetSelection()
        glossary.Enable(editable and 0 <= index < len(profiles) and profiles[index].provider == 'google-translation-v3')
        for name in ('settings', 'languages', 'preview'):
            buttons[name].Enable(editable)
        for name in ('new', 'jobs', 'run'):
            buttons[name].Enable(not busy)
        buttons['pause'].Enable(busy)
        buttons['charges'].Enable(not busy and current_job is not None)
        buttons['export'].Enable(not busy and current_job is not None)

    def load_languages(event=None):
        nonlocal source_codes, target_codes
        rows = ()
        if profiles:
            try:
                rows = service.languages(profile())
            except Exception as exc:
                report(str(exc))
        source_rows = [row for row in rows if row.get('source', True)]
        target_rows = [row for row in rows if row.get('target', True)]
        source_codes = ['auto'] + [row['language'] for row in source_rows]
        target_codes = [row['language'] for row in target_rows]
        source.SetItems(['auto — detect source language'] +
                        [row['language'] + ' — ' + row['name'] for row in source_rows])
        source.SetSelection(0)
        target.SetItems([row['language'] + ' — ' + row['name'] for row in target_rows])
        if target_codes:
            target.SetSelection(target_codes.index('uk') if 'uk' in target_codes else 0)
        controller.reviewed = None
        index = provider.GetSelection()
        if not (0 <= index < len(profiles) and profiles[index].provider == 'google-translation-v3'):
            glossary.ChangeValue('')
        controls()

    def load_profiles():
        nonlocal profiles
        profiles = list(service.available_profiles())
        provider.SetItems([p.name + ' — ' + p.provider + (' — credential not connected' if not p.credential_ref else '') for p in profiles])
        if profiles:
            provider.SetSelection(0)
        load_languages()

    def begin(label, operation, complete, on_error=None):
        nonlocal busy
        if busy or not alive:
            return False
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            report('Another operation is active. Finish or cancel it before starting translation.')
            return False
        busy = True
        cancel.clear()
        controls()
        def finish(result):
            nonlocal busy
            if not alive:
                return
            busy = False
            controls()
            try:
                complete(result)
            except Exception as error:
                if on_error is not None:
                    on_error(error)
                else:
                    report(str(error))
        def failed(error):
            nonlocal busy
            if not alive:
                return
            busy = False
            controls()
            if on_error is not None:
                on_error(error)
            else:
                report(str(error))
        started = frame.run_job(label, operation, done=finish, failed=failed)
        if started is False or (not getattr(frame, 'working', False) and busy):
            busy = False
            controls()
            return False
        return True

    def stopped():
        return (cancel.is_set() or frame.cancel_event.is_set() or frame.library is not scoped_library
                or Path(frame.library.root).absolute() != service.root)

    def settings(event):
        try:
            from .api_connections import show_api_connections
            show_api_connections(frame)
            load_profiles()
            controls()
        except Exception as exc:
            report(str(exc))

    def languages(event):
        try:
            chosen = profile()
            if wx.MessageBox(
                    'Contact Google Translation to fetch its NMT supported-language list for this account? '
                    'No selected text is sent. The account credential stays in its HTTPS header.',
                    'Approve supported-language discovery', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    dialog) != wx.YES:
                return
            approval = language_approval(chosen)
            def complete(rows):
                load_languages()
                report(f'{len(rows)} provider-declared NMT language codes cached for this exact profile. '
                       'This list is not a claim of independently verified translation quality.')
            begin('Fetching supported translation languages',
                  lambda: service.refresh_languages(chosen, approval=approval, cancelled=stopped), complete)
        except Exception as exc:
            report(str(exc))

    def captured(document):
        nonlocal structured_source
        structured_source = document
        controller.reviewed = None
        selected.ChangeValue(source_text(document))
        selected.SetEditable(False)
        report(f"Prepared {len(document['documents'])} ordered document selection(s). "
               'Original sections and source identity are retained. Preview before approving transfer.')
        controls()

    def choose_documents(event=None):
        if busy or current_job is not None:
            return
        try:
            from .translation_sources import choose_translation_sources
            document = choose_translation_sources(frame, dialog)
            if document is not None:
                captured(document)
        except Exception as exc:
            report(str(exc))

    def preserve_structure(event=None):
        if busy or current_job is not None:
            return
        choice = wx.SingleChoiceDialog(dialog,
            'Choose a tested text structure profile. Other layout is not reconstructed.',
            'Translation structure', ['Paragraph boundaries', 'Markdown ATX headings, paragraphs and literal fenced code'])
        try:
            if choice.ShowModal() != wx.ID_OK:
                return
            markdown = choice.GetSelection() == 1
        finally:
            choice.Destroy()
        value, source_title = selected.GetValue(), job_title.GetValue()
        begin('Capturing exact translation text structure',
              lambda: capture_text(value, title=source_title, markdown=markdown, cancelled=stopped), captured)

    def use_editable_text(event=None):
        nonlocal structured_source
        if busy or current_job is not None:
            return
        structured_source = None
        controller.reviewed = None
        selected.SetEditable(True)
        report('Editable selected text. A new preview is required; retained document links are no longer attached.')
        controls()

    def values():
        first, last = pair()
        return (profile(), selected.GetValue(), first, last, budget.GetValue().strip())

    def preview(event=None):
        try:
            result = controller.preview(*values(), title=job_title.GetValue(), rate=rate.GetValue().strip(),
                                        glossary=glossary.GetValue().strip(), structure=structured_source)
            result_view.ChangeValue('')
            report(json.dumps(result, ensure_ascii=False, indent=2) +
                   '\n\nNo text sent. Translate asks permission for these exact data. '
                   'This budget limits local reservations; provider billing is not guaranteed by this application.')
        except Exception as exc:
            controller.reviewed = None
            report(str(exc))

    def completed(result):
        if result['status'] == 'complete':
            job = service.jobs.load(result['id'])
            result_view.ChangeValue(''.join(row['text'] for row in job['receipts']))
        report(json.dumps(result, ensure_ascii=False, indent=2) +
               '\nExport creates new original, translation, bilingual, narration-draft and provenance files.')

    def run(event):
        nonlocal current_job
        try:
            chosen = exact = document = None
            if current_job is None:
                chosen = values()
                exact = controller.selection(*chosen, job_title.GetValue(), rate.GetValue().strip(),
                    glossary.GetValue().strip(), structured_source)
                if exact != controller.reviewed:
                    raise ValueError('Preview the current exact account, text, language pair and budget first.')
                if wx.MessageBox(
                        f'Account: {chosen[0].name}\nEndpoint: {chosen[0].endpoint}\nModel: {chosen[0].model}\n'
                        f'Glossary: {exact[7] or "none"}\n'
                        f'Characters: {len(chosen[1])}\nLanguage pair: {chosen[2]} → {chosen[3]}\n'
                        f'Approved total reservation: USD {Decimal(exact[4]) / 1000000}\n'
                        'Send only this selected text? The provider may charge your account. '
                        'No automatic retry or fallback will be made.',
                        'Approve exact text transfer', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                        dialog) != wx.YES:
                    return
                if controller.selection(*values(), job_title.GetValue(), rate.GetValue().strip(),
                                        glossary.GetValue().strip(), structured_source) != exact:
                    controller.reviewed = None
                    raise ValueError('The selected text, account or glossary changed during approval. Preview it again.')
                # Capture the approved immutable values before entering the
                # shared worker. Source validation can hash a large original.
                document = structured_source
            identity = current_job
            def translate():
                nonlocal current_job
                selected_identity = identity
                if selected_identity is None:
                    selected_identity = controller.submit(*chosen, approved=True,
                        title=exact[5], rate=exact[6], glossary=exact[7], structure=document,
                        cancelled=stopped)
                    # Commit ownership before resume or later inspection can
                    # fail. Only UI-thread completion callbacks touch widgets.
                    current_job = selected_identity
                progress = getattr(frame, 'operation_progress', None)
                token = progress.event.token if progress is not None else None
                def advanced(done, total, phase):
                    if progress is not None:
                        progress.update(done, total, phase, token=token)
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
                return service.resume(selected_identity, cancelled=stopped, progress=advanced)
            def run_failed(error):
                prefix = '' if current_job is None else f'Saved translation {current_job}: '
                report(prefix + str(error))
            begin('Translating explicitly approved text', translate, completed, on_error=run_failed)
        except Exception as exc:
            controls()
            report(str(exc))

    def new(event):
        nonlocal current_job
        current_job = None
        controller.reviewed = None
        budget.ChangeValue('')
        glossary.ChangeValue('')
        result_view.ChangeValue('')
        load_profiles()
        controls()
        report('New translation: review the text, supported pair and budget before approving any request.')

    def jobs(event):
        nonlocal current_job, source_codes, target_codes, structured_source
        try:
            rows = service.list()
            if not rows:
                report('No saved translation jobs.')
                return
            chooser = wx.SingleChoiceDialog(dialog, 'Choose a saved translation with its original binding and budget.',
                'Translation jobs', [f"{j['title']}; {j['status']}; {j['completed']}/{j['total']}; {j['target']}" for j in rows])
            try:
                if chooser.ShowModal() != wx.ID_OK or not 0 <= chooser.GetSelection() < len(rows):
                    return
                result = rows[chooser.GetSelection()]
                current_job = result['id']
                job = service.jobs.load(current_job)
                structured_source = job.get('structure')
                source_codes, target_codes = [job['source_language']], [job['target_language']]
                source.SetItems(source_codes)
                source.SetSelection(0)
                target.SetItems(target_codes)
                target.SetSelection(0)
                provider.SetSelection(next((i for i, p in enumerate(profiles)
                                            if p.fingerprint == job['approval']['profile_fingerprint']), wx.NOT_FOUND))
                job_title.ChangeValue(job['title'])
                glossary.ChangeValue(job.get('glossary', ''))
                selected.ChangeValue(job['source_text'])
                result_view.ChangeValue(''.join(row['text'] for row in job['receipts']))
                budget.ChangeValue(str(Decimal(job['approval']['budget_micros']) / 1000000))
                report(json.dumps(result, ensure_ascii=False, indent=2) +
                       f'\nApproved account: {job["profile"]["name"]}; {job["profile"]["endpoint"]}.\nSaved inputs are read-only. Resume uses the original account, pair and reservation. '
                       'Moved libraries, changed profiles and unresolved charges cannot borrow old permission.')
                controls()
            finally:
                chooser.Destroy()
        except Exception as exc:
            report(str(exc))

    def charges(event):
        try:
            report(json.dumps(service.charge_report(current_job), ensure_ascii=False, indent=2) +
                   '\nAn unresolved or missing paid output is never resubmitted. Review the provider account '
                   'before considering a separately approved new translation.')
        except Exception as exc:
            report(str(exc))

    def export(event):
        if current_job is None:
            return
        chooser = wx.DirDialog(dialog, 'Choose a parent folder outside the library for a NEW translation export')
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            destination = Path(chooser.GetPath()) / ('Translation-' + current_job[:8] + '-' + uuid4().hex[:8])
        finally:
            chooser.Destroy()
        identity = current_job
        begin('Exporting translation derivative',
              lambda: service.export(identity, destination, cancelled=stopped),
              lambda result: report('New derivative and immutable original saved in: ' + result['path']))

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        alive = False
        dialog.EndModal(wx.ID_CLOSE)

    handlers = {'settings': settings, 'languages': languages, 'preview': preview, 'run': run,
                'pause': lambda event: cancel.set(), 'new': new, 'jobs': jobs,
                'charges': charges, 'export': export, 'close': close}
    for name, handler in handlers.items():
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    source_buttons['documents'].Bind(wx.EVT_BUTTON, choose_documents)
    source_buttons['structure'].Bind(wx.EVT_BUTTON, preserve_structure)
    source_buttons['editable'].Bind(wx.EVT_BUTTON, use_editable_text)
    provider.Bind(wx.EVT_CHOICE, load_languages)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        report('LOCAL ONLY until an exact task is explicitly approved. Basic v2 uses an API key; Advanced v3 '
               'uses an explicitly supplied IAM/ADC OAuth token and expiry in Windows Credential Manager. '
               'Advanced v3 verifies its project, location, general/nmt model and optional existing regional glossary. '
               'Choose selected text, retained documents or chapter ranges. Tested structure profiles retain paragraph boundaries and ATX heading levels; other layout and independent language quality are not guaranteed.')
        load_profiles()
        controls()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        dialog.Destroy()



class TranslationActions:
    def __init__(self, frame):
        self.frame = frame

    def open(self, event=None):
        show_translation(self.frame)


def install_translation_actions(frame):
    """Register one stable command before or after keyboard settings installation."""
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'translation_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise ValueError('Install the shared Tools menu before translation actions.')
    actions = TranslationActions(frame)
    item = menu.Append(wx.ID_ANY, 'Translate selected te&xt...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame, CommandSpec('translation.text', 'Translate selected text', 'main'), item)
    frame.translation_actions = actions
    return actions

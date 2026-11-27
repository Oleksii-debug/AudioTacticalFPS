"""Native review/approval workspace for plain-text Google Basic v2 derivatives."""
from decimal import Decimal
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..assistant.translation import (
    TranslationService, language_approval, translation_approval)
from ..assistant.translation_state import MAX_SOURCE_CHARACTERS
from .api_connections import parse_budget
from ..progress import ProgressTracker


def translation_outcome(result):
    """Only a durably completed derivative may announce verified completion."""
    status = result.get('status') if isinstance(result, dict) else None
    if status not in ('complete', 'paused', 'attention'):
        raise ValueError('Translation returned an unrecognized completion state.')
    return {'complete': 'completed', 'paused': 'paused', 'attention': 'failed'}[status]


class TranslationController:
    """Preview is local; only a separate exact-data approval creates a job."""
    def __init__(self, root, *, service=None):
        self.service = service or TranslationService(root)
        if self.service.root != Path(root).absolute():
            raise ValueError('Translation belongs to the selected library root.')
        self.reviewed = None

    @staticmethod
    def selection(profile, value, source, target, budget, title, rate):
        return (profile.fingerprint, value, source, target, parse_budget(budget), title, rate)

    def preview(self, profile, value, source, target, budget, *, title='Translation', rate=''):
        self.reviewed = None
        chosen = self.selection(profile, value, source, target, budget, title, rate)
        result = self.service.preview(profile, value, source, target, rate_per_million=rate or None)
        if result['estimated_cost'] is not None and Decimal(result['estimated_cost']) * 1000000 > chosen[4]:
            raise ValueError('The entered price estimate exceeds the chosen reservation budget.')
        self.reviewed = chosen
        return {**result, 'budget_micros': chosen[4], 'currency': 'USD',
                'provider_name': profile.name, 'endpoint': profile.endpoint, 'model': profile.model}

    def submit(self, profile, value, source, target, budget, *, approved=False, title='Translation', rate=''):
        chosen = self.selection(profile, value, source, target, budget, title, rate)
        if approved is not True or chosen != self.reviewed:
            raise PermissionError('Preview and explicitly approve this exact account, text, pair and budget.')
        approval = translation_approval(profile, value, source, target, chosen[4])
        identity = self.service.submit(profile, value, source, target, approval=approval, title=title)
        self.reviewed = None
        return identity


def show_translation(frame, *, initial_text='', title='Translation', service=None):
    import wx
    if (not isinstance(initial_text, str) or len(initial_text) > MAX_SOURCE_CHARACTERS
            or not isinstance(title, str) or not title.strip() or len(title) > 512):
        raise ValueError('Choose bounded selected text and a translation title.')
    controller = TranslationController(frame.library.root, service=service)
    service = controller.service
    profiles, source_codes, target_codes = [], [], []
    alive, busy, current_job = True, False, None
    cancel = threading.Event()
    dialog = wx.Dialog(frame, title='Translation — selected text derivatives', size=(900, 900),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Google Basic v2 &account', wx.Choice, choices=[])
    job_title = field('Translation &title', value=title)
    source = field('&Source language', wx.Choice, choices=[])
    target = field('Target &language', wx.Choice, choices=[])
    selected = field('Selected te&xt; originals remain unchanged', proportion=1,
                     value=initial_text, style=wx.TE_MULTILINE)
    selected.SetMaxLength(MAX_SOURCE_CHARACTERS)
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
            buttons[name] = button
            row.Add(button, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CLOSE)

    def report(value):
        if alive:
            status.ChangeValue(str(value))

    def profile():
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Create a connected Google Basic v2 profile in AI/API connections.')
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
        source_codes = ['auto'] + [row['language'] for row in rows]
        target_codes = [row['language'] for row in rows]
        labels = [row['language'] + ' — ' + row['name'] for row in rows]
        source.SetItems(['auto — detect source language'] + labels)
        source.SetSelection(0)
        target.SetItems(labels)
        if target_codes:
            target.SetSelection(target_codes.index('uk') if 'uk' in target_codes else 0)
        controller.reviewed = None

    def load_profiles():
        nonlocal profiles
        profiles = list(service.available_profiles())
        provider.SetItems([p.name + (' — key not connected' if not p.credential_ref else '') for p in profiles])
        if profiles:
            provider.SetSelection(0)
        load_languages()

    def begin(label, operation, complete, *, progress_tracker=None, progress_outcome=None):
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
            complete(result)
        def failed(error):
            nonlocal busy
            if not alive:
                return
            busy = False
            controls()
            report(str(error))
        started = frame.run_job(label, operation, done=finish, failed=failed,
                                progress_tracker=progress_tracker, progress_outcome=progress_outcome)
        if started is False or (not getattr(frame, 'working', False) and busy):
            busy = False
            controls()
            return False
        return True

    def stopped():
        return cancel.is_set() or frame.cancel_event.is_set()

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
                    'No selected text is sent. The API key stays in its HTTPS header.',
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

    def values():
        first, last = pair()
        return (profile(), selected.GetValue(), first, last, budget.GetValue().strip())

    def preview(event=None):
        try:
            result = controller.preview(*values(), title=job_title.GetValue(), rate=rate.GetValue().strip())
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
            if current_job is None:
                chosen = values()
                exact = controller.selection(*chosen, job_title.GetValue(), rate.GetValue().strip())
                if exact != controller.reviewed:
                    raise ValueError('Preview the current exact account, text, language pair and budget first.')
                if wx.MessageBox(
                        f'Account: {chosen[0].name}\nEndpoint: {chosen[0].endpoint}\nModel: nmt\n'
                        f'Characters: {len(chosen[1])}\nLanguage pair: {chosen[2]} → {chosen[3]}\n'
                        f'Approved total reservation: USD {Decimal(exact[4]) / 1000000}\n'
                        'Send only this selected text? The provider may charge your account. '
                        'No automatic retry or fallback will be made.',
                        'Approve exact text transfer', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                        dialog) != wx.YES:
                    return
                current_job = controller.submit(*chosen, approved=True,
                    title=job_title.GetValue(), rate=rate.GetValue().strip())
                controls()
            identity = current_job
            tracker = ProgressTracker('Translation', unit='chunks')
            def translate():
                def advanced(done, total, phase):
                    tracker.update(completed=done, total=total, phase=phase, unit='chunks')
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
                return service.resume(identity, cancelled=stopped, progress=advanced)
            begin('Translating explicitly approved text', translate, completed,
                  progress_tracker=tracker, progress_outcome=translation_outcome)
        except Exception as exc:
            controls()
            report(str(exc))

    def new(event):
        nonlocal current_job
        current_job = None
        controller.reviewed = None
        budget.ChangeValue('')
        result_view.ChangeValue('')
        load_profiles()
        controls()
        report('New translation: review the text, supported pair and budget before approving any request.')

    def jobs(event):
        nonlocal current_job, source_codes, target_codes
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
                source_codes, target_codes = [job['source_language']], [job['target_language']]
                source.SetItems(source_codes)
                source.SetSelection(0)
                target.SetItems(target_codes)
                target.SetSelection(0)
                provider.SetSelection(next((i for i, p in enumerate(profiles)
                                            if p.fingerprint == job['approval']['profile_fingerprint']), wx.NOT_FOUND))
                job_title.ChangeValue(job['title'])
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
              lambda result: report('New derivative and immutable original saved in: ' + result['path']),
              progress_outcome=translation_outcome)

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
    provider.Bind(wx.EVT_CHOICE, load_languages)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        report('LOCAL ONLY until an exact task is explicitly approved. Google Basic v2 NMT uses the existing '
               'Windows Credential Manager profile. Advanced v3/IAM, local translation models, formatted '
               'documents and glossaries are separate capabilities and are not selected by this adapter.')
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
        if getattr(self.frame, 'working', False) or getattr(self.frame, '_closing', False):
            self.frame.report('Finish or cancel the active operation before opening translation.')
            return
        try:
            show_translation(self.frame)
        except Exception as exc:
            self.frame.report(str(exc))


def install_translation_actions(frame):
    """Install before the existing productivity menu/keyboard collection."""
    import wx
    existing = getattr(frame, 'translation_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Tools')
    actions = TranslationActions(frame)
    item = menu.Append(wx.ID_ANY, 'Translate selected te&xt...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    frame.translation_actions = actions
    return actions

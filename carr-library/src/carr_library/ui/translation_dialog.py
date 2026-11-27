"""Native explicit plain-text translation workspace on the shared provider gateway."""
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256
import json
from pathlib import Path
import threading
from time import time
from uuid import uuid4

from ..assistant.profiles import ProfileStore
from ..assistant.providers import Authorization, Gateway, _request, money
from ..assistant.models import canonical
from ..assistant.translation import TranslationService, split_text


def translation_approval(profile, value, source, target, budget):
    """Bind an approval to the exact selected plaintext request hashes."""
    identity = uuid4().hex
    hashes = []
    for chunk in split_text(value):
        if not chunk.strip():
            continue
        payload = {'q': chunk, 'target': target}
        if source != 'auto':
            payload['source'] = source
        _, body, _ = _request(profile, 'translation.text', payload)
        hashes.append(sha256(canonical(body).encode('utf-8')).hexdigest())
    approval = Authorization(identity, identity, ((profile.id, profile.endpoint),),
        ('translation.text',), ('selected-excerpt',), str(money(budget)), len(hashes),
        time() + 24 * 3600, tuple(hashes))
    per_call = (money(budget) / Decimal(len(hashes))).quantize(Decimal('0.000000000001'), rounding=ROUND_DOWN)
    return approval, str(per_call)


def show_translation(frame):
    import wx
    root = frame.library.root / 'state' / 'translation'
    service = TranslationService(root)
    profiles = tuple(p for p in ProfileStore(frame.library.root / 'state' / 'providers').list()
                     if p.adapter == 'google-translation-v2' and p.enabled)
    dialog = wx.Dialog(frame, title='Translation — selected text derivatives', size=(850, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    alive, busy, current_job, reviewed = True, False, None, None
    cancel = threading.Event()

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Google Basic v2 &account', wx.Choice, choices=[p.name for p in profiles])
    if profiles:
        provider.SetSelection(0)
    source = field('&Source language code; auto detects', value='auto')
    target = field('Target language co&de', value='uk')
    selected = field('Selected te&xt; originals remain unchanged', proportion=1, style=wx.TE_MULTILINE)
    rate = field('Optional price per million characters in USD; your current contract rate', value='')
    budget = field('Maximum approved &budget in USD; no default price', value='')
    status = field('Preview, supported languages and job status', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='LOCAL ONLY until explicit approval. Configure an enabled google-translation-v2 account in Tools / Translation and assistant service profiles: '
              'https://translation.googleapis.com/language/translate/v2, model nmt, capabilities translation.text,translation.languages. '
              'Basic uses an API key in Windows Credential Manager. Advanced v3 IAM/ADC, local translation models, '
              'formatted documents and glossaries are unavailable in this adapter.')
    buttons = {}
    row = wx.BoxSizer(wx.HORIZONTAL)
    for name, label in [('languages', 'Fetch &languages'), ('preview', '&Preview'), ('run', '&Translate / resume'),
                        ('pause', 'Pa&use'), ('jobs', 'Saved &jobs'), ('export', '&Export'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        buttons[name] = button
        row.Add(button, 0, wx.ALL, 3)
    layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    buttons['pause'].Disable()
    buttons['export'].Disable()

    def report(value):
        if alive:
            status.ChangeValue(str(value))

    def profile():
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Create and enable a Google Basic v2 account in Tools / Translation and assistant service profiles, then reopen this window.')
        return profiles[index]

    def controls():
        if not alive:
            return
        for control in (provider, source, target, selected, rate, budget):
            control.Enable(not busy)
        for name in ('languages', 'preview', 'run', 'jobs', 'export'):
            buttons[name].Enable(not busy and (name != 'export' or current_job is not None))
        buttons['pause'].Enable(busy)

    def begin(title, operation, complete):
        nonlocal busy
        if busy:
            return
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            report('Another operation is active. Finish or cancel it before starting translation.')
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
            report(str(error))
        if frame.run_job(title, operation, done=finish, failed=failed) is False:
            busy = False
            controls()

    def languages(event):
        try:
            p = profile()
            if wx.MessageBox('Contact the official Google Translation API to fetch its supported language list? '
                'No selected text is sent. Your configured account will be used.', 'Approve language discovery',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            identity = uuid4().hex
            approval = Authorization(identity, identity, ((p.id, p.endpoint),), ('translation.languages',),
                                      ('user-instruction',), '0', 1, time() + 600)
            service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
            begin('Fetching supported translation languages',
                  lambda: service.refresh_languages(p, authorization=approval,
                      cancelled=lambda: cancel.is_set() or frame.cancel_event.is_set()),
                  lambda rows: report('Provider-declared NMT languages; no quality or 135-language qualification claim:\n' +
                                      '\n'.join(row['language'] + ' — ' + row['name'] for row in rows)))
        except Exception as exc:
            report(str(exc))

    def preview(event=None):
        nonlocal reviewed, current_job
        try:
            p = profile()
            result = service.preview(p, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(),
                                     rate_per_million=rate.GetValue().strip() or None)
            total = money(budget.GetValue())
            if result['estimated_cost'] is not None and money(result['estimated_cost']) > total:
                raise ValueError('The entered price estimate exceeds the chosen total budget.')
            reviewed = (p.id, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(total))
            current_job = None
            report(json.dumps(result, ensure_ascii=False, indent=2) +
                   '\n\nNo request sent. Translate asks permission for this exact selected text and total budget. '
                   'The budget is a local reservation bound, not a provider-enforced billing guarantee.')
            controls()
        except Exception as exc:
            reviewed = None
            report(str(exc))

    def run(event):
        nonlocal current_job
        try:
            if current_job is None:
                p = profile()
                values = (p.id, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(money(budget.GetValue())))
                if reviewed != values:
                    raise ValueError('Preview this exact text, languages and budget before approving translation.')
                if wx.MessageBox(f'Send exactly {len(values[1])} selected characters to Google Translation? '
                    f'Approved total reservation: {values[4]} USD. Translation is a derived reading copy. '
                    'The provider may charge your account under its terms.', 'Approve selected text transfer',
                    wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                authorization, bound = translation_approval(p, values[1], values[2], values[3], values[4])
                current_job = service.submit(p, values[1], values[2], values[3],
                    authorization=authorization, per_call_cost=bound)
            service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
            identity = current_job
            def translate():
                progress = getattr(frame, 'operation_progress', None)
                token = progress.event.token if progress is not None else None
                def advanced(done, total, phase):
                    if progress is not None:
                        progress.update(done, total, phase, token=token)
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
                return service.resume(identity,
                    cancelled=lambda: cancel.is_set() or frame.cancel_event.is_set(), progress=advanced)
            begin('Translating approved selected text',
                  translate,
                  lambda result: report(json.dumps(result, indent=2) + '\nExport writes new original, translation, bilingual and reading-copy files.'))
        except Exception as exc:
            report(str(exc))

    def jobs(event):
        nonlocal current_job
        try:
            rows = service.list()
            chooser = wx.SingleChoiceDialog(dialog, 'Choose a saved approved translation. Its original scope and budget remain binding.',
                'Translation jobs', [f"{j['title']}; {j['status']}; {len(j['receipts'])}/{len(j['chunks'])}; {j['target_language']}" for j in rows])
            try:
                if chooser.ShowModal() == wx.ID_OK and 0 <= chooser.GetSelection() < len(rows):
                    current_job = rows[chooser.GetSelection()]['id']
                    report('Saved job selected. Resume reuses its verified chunks and original authorization. '
                           'An uncertain charge requires reconciliation; it is never automatically retried.')
                    controls()
            finally:
                chooser.Destroy()
        except Exception as exc:
            report(str(exc))

    def export(event):
        if current_job is None:
            return
        chooser = wx.DirDialog(dialog, 'Choose parent folder for a NEW translation export')
        try:
            if chooser.ShowModal() == wx.ID_OK:
                destination = Path(chooser.GetPath()) / ('Translation-' + current_job[:8] + '-' + uuid4().hex[:8])
                identity = current_job
                begin('Exporting translation derivative', lambda: service.export(identity, destination),
                      lambda result: report('Saved new files in: ' + result['path']))
        finally:
            chooser.Destroy()

    def close(event=None):
        nonlocal alive
        cancel.set()
        alive = False
        dialog.EndModal(wx.ID_CLOSE)

    for name, handler in {'languages': languages, 'preview': preview, 'run': run, 'pause': lambda e: cancel.set(),
                           'jobs': jobs, 'export': export, 'close': close}.items():
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        dialog.Destroy()


def install_translation_actions(frame):
    import wx
    bar = frame.menu_bar
    menu = next((bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise RuntimeError('Install the shared Tools menu before translation actions.')
    identity = wx.NewIdRef()
    menu.Append(identity, 'Translate selected te&xt...')
    frame.Bind(wx.EVT_MENU, lambda event: show_translation(frame), id=int(identity))

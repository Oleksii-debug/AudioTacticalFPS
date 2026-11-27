"""Native explicit plain-text translation workspace on the shared provider gateway."""
import json
from pathlib import Path
import threading
from time import time
from uuid import uuid4

from ..assistant.profiles import ProfileStore
from ..assistant.providers import Authorization, Gateway, money
from ..assistant.translation import TranslationService, translation_approval


def show_translation(frame):
    import wx
    anchored_root = frame.library.root
    root = anchored_root / 'state' / 'translation'
    service = TranslationService(root)
    profiles = tuple(p for p in ProfileStore(frame.library.root / 'state' / 'providers').list()
                     if p.adapter in ('google-translation-v2', 'google-translation-v3') and p.enabled)
    dialog = wx.Dialog(frame, title='Translation — selected text derivatives', size=(850, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Translation workspace')
    layout = wx.BoxSizer(wx.VERTICAL)
    alive, busy, current_job, reviewed = True, False, None, None
    cancel = threading.Event()

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Google API &account and edition', wx.Choice,
                     choices=[p.name + '; ' + p.adapter for p in profiles])
    if profiles:
        provider.SetSelection(0)
    source = field('&Source language code; auto detects', value='auto')
    target = field('Target language co&de', value='uk')
    glossary = field('Advanced v3 &glossary resource; blank for none', value='')
    selected = field('Selected te&xt; originals remain unchanged', proportion=1, style=wx.TE_MULTILINE)
    rate = field('Optional price per million characters in USD; your current contract rate', value='')
    budget = field('Maximum approved &budget in USD; no default price', value='')
    status = field('Preview, supported languages and job status', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='LOCAL ONLY until explicit approval. Configure a Basic v2 API-key account or Advanced v3 NMT OAuth account '
              'in AI and API connections. Advanced requires your numeric project and region, and an unexpired IAM/ADC '
              'access token in Windows Credential Manager; tokens are never automatically refreshed. '
              'A selected existing glossary must share the project and non-global region; choose an explicit source language. '
              'Fetch languages for the exact account before preview. Document formatting and offline translation remain unavailable.')
    buttons = {}
    row = wx.BoxSizer(wx.HORIZONTAL)
    for name, label in [('languages', 'Fetch &languages'), ('preview', '&Preview'), ('run', '&Translate / resume'),
                        ('pause', 'Pa&use'), ('jobs', 'Saved &jobs'), ('export', '&Export'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
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
            raise ValueError('Create and enable a Google Basic v2 or Advanced v3 account in AI and API connections, then reopen this window.')
        return profiles[index]

    def check_library():
        if frame.library.root != anchored_root:
            raise ValueError('The active library changed. Reopen translation in the selected library; saved jobs remain in their original library.')

    def stopped():
        return cancel.is_set() or frame.cancel_event.is_set() or frame.library.root != anchored_root

    def controls():
        if not alive:
            return
        for control in (provider, source, target, glossary, selected, rate, budget):
            control.Enable(not busy)
        for name in ('languages', 'preview', 'run', 'jobs', 'export'):
            buttons[name].Enable(not busy and (name != 'export' or current_job is not None))
        buttons['pause'].Enable(busy)

    def begin(title, operation, complete):
        nonlocal busy
        if busy:
            return
        try:
            check_library()
        except ValueError as error:
            report(str(error))
            return False
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
                if frame.library.root == anchored_root:
                    complete(result)
                else:
                    report('The active library changed. This operation and any saved result belong to the original library.')
                status.SetFocus()
        def failed(error):
            nonlocal busy
            busy = False
            controls()
            report(str(error))
            if alive:
                status.SetFocus()
        def checked_operation():
            check_library()
            return operation()
        if frame.run_job(title, checked_operation, done=finish, failed=failed) is False:
            busy = False
            controls()

    def languages(event):
        try:
            check_library()
            p = profile()
            if wx.MessageBox('Contact the official Google Translation API to fetch its supported language list? '
                'No selected text is sent. Your configured account will be used.\n' + p.endpoint + '\nModel: ' + p.model, 'Approve language discovery',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            identity = uuid4().hex
            approval = Authorization(identity, identity, ((p.id, p.endpoint),), ('translation.languages',),
                                      ('user-instruction',), '0', 1, time() + 600)
            service.gateway = Gateway(root / 'provider', mode='APPROVED_REMOTE')
            begin('Fetching supported translation languages',
                  lambda: service.refresh_languages(p, authorization=approval,
                      cancelled=stopped),
                  lambda rows: report('Provider-declared NMT languages; no quality or 135-language qualification claim:\n' +
                                      '\n'.join(row['language'] + ' — ' + row['name'] +
                                          ('; source: ' + str(row['source']) + '; target: ' + str(row['target']) if 'source' in row else '') for row in rows)))
        except Exception as exc:
            report(str(exc))

    def preview(event=None):
        nonlocal reviewed, current_job
        try:
            check_library()
            p = profile()
            result = service.preview(p, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(),
                                     rate_per_million=rate.GetValue().strip() or None, glossary=glossary.GetValue().strip() or None)
            total = money(budget.GetValue())
            if result['estimated_cost'] is not None and money(result['estimated_cost']) > total:
                raise ValueError('The entered price estimate exceeds the chosen total budget.')
            reviewed = (p.id, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(total),
                        glossary.GetValue().strip() or None, rate.GetValue().strip() or None)
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
            check_library()
            if current_job is None:
                p = profile()
                values = (p.id, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(money(budget.GetValue())),
                          glossary.GetValue().strip() or None, rate.GetValue().strip() or None)
                if reviewed != values:
                    raise ValueError('Preview this exact text, languages and budget before approving translation.')
                if wx.MessageBox(f'Send exactly {len(values[1])} selected characters to Google Translation? '
                    f'Approved total reservation: {values[4]} USD. Translation is a derived reading copy. '
                    'The provider may charge your account under its terms.\n' + p.endpoint + '\nModel: ' + p.model +
                    '\nGlossary: ' + (values[5] or 'none'), 'Approve selected text transfer',
                    wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                check_library()
                authorization, bound = translation_approval(p, values[1], values[2], values[3], values[4], glossary=values[5])
                current_job = service.submit(p, values[1], values[2], values[3],
                    authorization=authorization, per_call_cost=bound, glossary=values[5])
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
                    cancelled=stopped, progress=advanced)
            begin('Translating approved selected text',
                  translate,
                  lambda result: report(json.dumps(result, indent=2) + '\nExport writes new original, translation, bilingual and reading-copy files.'))
        except Exception as exc:
            report(str(exc))

    def jobs(event):
        nonlocal current_job
        try:
            check_library()
            rows = service.list()
            chooser = wx.SingleChoiceDialog(dialog, 'Choose a saved approved translation. Its original scope and budget remain binding.',
                'Translation jobs', [f"{j['title']}; {j['status']}; {len(j['receipts'])}/{len(j['chunks'])}; {j['target_language']}" for j in rows])
            try:
                if chooser.ShowModal() == wx.ID_OK and 0 <= chooser.GetSelection() < len(rows):
                    current_job = rows[chooser.GetSelection()]['id']
                    report('Saved job selected. Resume reuses its verified chunks and original authorization. '
                           'An uncertain charge requires reconciliation; it is never automatically retried. '
                           'Expired Advanced OAuth tokens require explicit re-entry on the unchanged account.\n' +
                           json.dumps({key: rows[chooser.GetSelection()].get(key) for key in
                               ('source_language', 'target_language', 'glossary', 'profile')}, ensure_ascii=False, indent=2))
                    controls()
            finally:
                chooser.Destroy()
        except Exception as exc:
            report(str(exc))

    def export(event):
        if current_job is None:
            return
        try:
            check_library()
        except ValueError as error:
            report(str(error))
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
    provider.SetFocus()
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

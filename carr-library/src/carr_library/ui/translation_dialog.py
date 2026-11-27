"""Native explicit plain-text translation workspace on the shared provider gateway."""
import json
from pathlib import Path
import threading
from time import time
from uuid import uuid4

from ..assistant.profiles import ProfileStore
from ..assistant.providers import Authorization, Gateway, money, translation_authorization_type
from ..assistant.translation import TranslationService, translation_approval
from ..assistant.translation_source import source_options


def show_translation(frame):
    import wx
    anchored_root = frame.library.root
    root = anchored_root / 'state' / 'translation'
    service = TranslationService(root, library=frame.library)
    profiles = tuple(p for p in ProfileStore(frame.library.root / 'state' / 'providers').list()
                     if p.adapter in ('google-translation-v2', 'google-translation-v3', 'libretranslate-local') and p.enabled)
    dialog = wx.Dialog(frame, title='Translation — selected text derivatives', size=(850, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Translation workspace')
    layout = wx.BoxSizer(wx.VERTICAL)
    alive, busy, current_job, reviewed = True, False, None, None
    source_record = None
    batch_items = []
    cancel = threading.Event()

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Translation provider &profile' if any(p.local for p in profiles) else
                     'Google API &account and edition', wx.Choice,
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
              'Fetch languages for the exact account before preview. Document formatting is not preserved. '
              'An explicitly configured local LibreTranslate profile uses only its loopback endpoint, no credential, '
              'and an explicit zero cost bound. No local server/model is installed or started; declared pairs and quality are not independently qualified.')
    buttons = {}
    row = wx.BoxSizer(wx.HORIZONTAL)
    for name, label in [('load_source', 'Load library so&urce'), ('paste', 'Use &pasted text'),
                         ('add_batch', 'Add current source to batch'), ('batches', 'Document batches...'),
                        ('languages', 'Fetch &languages'), ('preview', '&Preview'), ('run', '&Translate / resume'),
                        ('pause', 'Pa&use'), ('jobs', 'Saved &jobs'), ('export', '&Export'), ('close', '&Close')]:
        if name == 'languages':
            layout.Add(row, 0, wx.EXPAND)
            row = wx.BoxSizer(wx.HORIZONTAL)
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
            raise ValueError('Create and enable a Google Basic v2, Advanced v3 or local LibreTranslate profile in AI and API connections, then reopen this window.')
        return profiles[index]

    def check_library():
        if frame.library.root != anchored_root:
            raise ValueError('The active library changed. Reopen translation in the selected library; saved jobs remain in their original library.')
        if source_record is not None and getattr(frame, 'active', None) != (
                source_record['source_anchor']['workspace'], source_record.get('source_topic')):
            raise ValueError('The selected source library or topic changed. Reopen translation in that scope.')

    service.source_guard = check_library

    def source_arguments():
        if source_record is None:
            return {}
        if selected.GetValue() != source_record['text']:
            raise ValueError('The source snapshot changed. Load and review it again, or choose pasted-text mode.')
        return {key: source_record[key] for key in ('source_anchor', 'source_metadata', 'source_topic')}

    def stopped():
        return cancel.is_set() or frame.cancel_event.is_set() or frame.library.root != anchored_root

    def controls():
        if not alive:
            return
        for control in (provider, source, target, glossary, selected, rate, budget):
            control.Enable(not busy)
        if hasattr(selected, 'SetEditable'):
            selected.SetEditable(source_record is None)
        for name in ('load_source', 'paste', 'add_batch', 'batches', 'languages', 'preview', 'run', 'jobs', 'export'):
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

    def paste_mode(event):
        nonlocal source_record, reviewed, current_job
        source_record, reviewed, current_job = None, None, None
        selected.ChangeValue('')
        controls()
        report('Paste mode: no library source identity is claimed. Paste and preview the exact text before approval.')
        selected.SetFocus()

    def load_source(event):
        nonlocal source_record, reviewed, current_job
        try:
            check_library()
            from .reading_desk import ReadingActions
            actions = ReadingActions(frame)
            scope = actions._scope()
            panel = frame.book.GetPage(1) if frame.book is not None else None
            if panel is None or panel.workspace != scope.workspace or not panel.ready:
                raise ValueError('Open Books and Collections and select an imported source first.')
            edition = panel._selection()[2]
            if edition is None or edition not in panel.rows:
                raise ValueError('Select an existing imported book in Books and Collections first.')

            def loaded(record):
                nonlocal source_record, reviewed, current_job
                actions._guard(scope)
                source_record, reviewed, current_job = record, None, None
                selected.ChangeValue(record['text'])
                controls()
                report('Loaded exact source revision ' + record['source_anchor']['revision'] +
                    '. Originals stay unchanged; this extracted-text derivative does not preserve document formatting.\n' +
                    '\n'.join(record['warnings']) + '\nPreview languages, source identity and cost before approving transfer.')

            def choose(options):
                try:
                    choose_position(options)
                except Exception as exc:
                    report(str(exc))

            def choose_position(options):
                actions._guard(scope)
                labels = ['Entire document'] + options['positions'] + ['Source reading-position range']
                picker = wx.SingleChoiceDialog(dialog, 'Choose the exact source selection; positions are in reading order.',
                                               options['title'], labels)
                try:
                    if picker.ShowModal() != wx.ID_OK:
                        return
                    index = picker.GetSelection()
                finally:
                    picker.Destroy()
                if not 0 <= index < len(labels):
                    return
                first, last = 1, len(options['positions'])
                if 1 <= index <= len(options['positions']):
                    first = last = index
                elif index == len(labels) - 1:
                    prompt = wx.TextEntryDialog(dialog, 'First and last reading positions, separated by a comma.',
                                                'Source range', '1,' + str(last))
                    try:
                        if prompt.ShowModal() != wx.ID_OK:
                            return
                        first, last = (int(part.strip()) for part in prompt.GetValue().split(','))
                    finally:
                        prompt.Destroy()
                def capture():
                    actions._guard(scope)
                    result = service.select_source(scope.workspace, edition, topic=scope.topic,
                        first=first, last=last, revision=options['revision'], cancelled=stopped)
                    actions._guard(scope)
                    return result
                begin('Loading exact translation source', capture, loaded)

            def inventory():
                actions._guard(scope)
                result = source_options(scope.library, scope.workspace, edition, topic=scope.topic, cancelled=stopped)
                actions._guard(scope)
                return result
            begin('Loading source reading positions', inventory, choose)
        except Exception as exc:
            report(str(exc))

    def languages(event):
        try:
            check_library()
            p = profile()
            prompt = ('Contact this explicitly selected local LibreTranslate service for declared language pairs? '
                'No selected text or credentials are sent; no server or model is started.\n'
                if p.adapter == 'libretranslate-local' else
                'Contact the official Google Translation API to fetch its supported language list? '
                'No selected text is sent. Your configured account will be used.\n')
            if wx.MessageBox(prompt + p.endpoint + '\nModel label: ' + p.model, 'Approve language discovery',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            identity = uuid4().hex
            approval = translation_authorization_type(p)(identity, identity, ((p.id, p.endpoint),), ('translation.languages',),
                                      ('user-instruction',), '0', 1, time() + 600)
            service.gateway = Gateway(root / 'provider', mode='LOCAL_ONLY' if p.local else 'APPROVED_REMOTE')
            begin('Fetching supported translation languages',
                  lambda: service.refresh_languages(p, authorization=approval,
                      cancelled=stopped),
                  lambda rows: report('Provider-declared languages; no model, quality or language-count qualification claim:\n' +
                                      '\n'.join(row['language'] + ' — ' + row['name'] +
                                          ('; targets: ' + ', '.join(row['targets']) if 'targets' in row else
                                           '; source: ' + str(row['source']) + '; target: ' + str(row['target']) if 'source' in row else '') for row in rows)))
        except Exception as exc:
            report(str(exc))

    def preview(event=None):
        nonlocal reviewed, current_job
        try:
            check_library()
            p = profile()
            total = money(budget.GetValue())
            if p.local and total != 0:
                raise ValueError('Enter an explicit zero external cost bound for local translation.')
            values = (p.id, selected.GetValue(), source.GetValue().strip(), target.GetValue().strip(), str(total),
                      glossary.GetValue().strip() or None, rate.GetValue().strip() or None,
                      json.dumps(source_arguments(), sort_keys=True))
            context = json.loads(values[7])
            reviewed, current_job = None, None
            def review():
                result = service.preview(p, values[1], values[2], values[3], rate_per_million=values[6],
                                         glossary=values[5], cancelled=stopped, **context)
                if result['estimated_cost'] is not None and money(result['estimated_cost']) > total:
                    raise ValueError('The entered price estimate exceeds the chosen total budget.')
                return result
            def complete(result):
                nonlocal reviewed
                reviewed = values
                report(json.dumps(result, ensure_ascii=False, indent=2) +
                       '\n\nNo request sent. Translate asks permission for this exact selected text and total budget. '
                       'The budget is a local reservation bound, not a provider-enforced billing guarantee.')
                controls()
            if context:
                begin('Reviewing exact translation source', review, complete)
            else:
                cancel.clear()
                complete(review())
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
                          glossary.GetValue().strip() or None, rate.GetValue().strip() or None,
                          json.dumps(source_arguments(), sort_keys=True))
                if reviewed != values:
                    raise ValueError('Preview this exact text, languages and budget before approving translation.')
                context = json.loads(values[7])
                prompt = (f'Send exactly {len(values[1])} selected characters to the explicitly selected local LibreTranslate service? '
                    'No external charge is reserved. No model is installed or started, and local server egress and quality are not attested.\n'
                    if p.adapter == 'libretranslate-local' else
                    f'Send exactly {len(values[1])} selected characters to Google Translation? '
                    f'Approved total reservation: {values[4]} USD. Translation is a derived reading copy. '
                    'The provider may charge your account under its terms.\n')
                if wx.MessageBox(prompt + p.endpoint + '\nModel label: ' + p.model +
                    '\nGlossary: ' + (values[5] or 'none'), 'Approve selected text transfer',
                    wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                check_library()
                authorization, bound = translation_approval(p, values[1], values[2], values[3], values[4],
                                                           glossary=values[5], **context)
                def submit():
                    return service.submit(p, values[1], values[2], values[3], authorization=authorization,
                        per_call_cost=bound, glossary=values[5], cancelled=stopped, **context)
                def submitted(identity):
                    nonlocal current_job
                    current_job = identity
                    if stopped():
                        report('The approved job was saved. Resume explicitly to start translation after this pause.')
                    else:
                        resume(identity)
                if context:
                    begin('Preparing approved source translation', submit, submitted)
                    return
                cancel.clear()
                current_job = submit()
            resume(current_job)
        except Exception as exc:
            report(str(exc))

    def resume(identity):
        try:
            def translate():
                saved = service._load(identity)
                service.gateway = Gateway(root / 'provider',
                    mode='LOCAL_ONLY' if saved['profile']['local'] else 'APPROVED_REMOTE')
                from ..progress import current_progress
                progress = current_progress()
                def advanced(done, total, phase):
                    if progress is not None:
                        try:
                            progress.update(completed=done, total=total, phase=phase)
                        except Exception as error:
                            wx.CallAfter(report, 'Progress reporting is unavailable: ' + str(error)
                                         + '. The saved task result is reported separately.')
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
                return service.resume(identity,
                    cancelled=stopped, progress=advanced)
            begin('Translating approved selected text',
                  translate,
                  lambda result: report(json.dumps(result, indent=2) + '\nExport writes new original, translation, bilingual and reading-copy files.'))
        except Exception as exc:
            report(str(exc))

    def jobs(event):
        nonlocal current_job, source_record
        try:
            check_library()
            rows = service.list()
            chooser = wx.SingleChoiceDialog(dialog, 'Choose a saved approved translation. Its original scope and budget remain binding.',
                'Translation jobs', [f"{j['title']}; {j['status']}; {len(j['receipts'])}/{len(j['chunks'])}; {j['target_language']}" for j in rows])
            try:
                if chooser.ShowModal() == wx.ID_OK and 0 <= chooser.GetSelection() < len(rows):
                    saved = rows[chooser.GetSelection()]
                    current_job = saved['id']
                    source_record = ({'text': saved['source_text'], **{key: saved[key] for key in
                        ('source_anchor', 'source_metadata', 'source_topic')}} if saved.get('source_anchor') is not None else None)
                    if source_record is not None:
                        selected.ChangeValue(source_record['text'])
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

    def add_batch(event=None):
        try:
            check_library()
            if busy or getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise RuntimeError('Finish the active operation before collecting another batch document.')
            from ..assistant.translation_batch import MAX_ITEMS, MAX_CHARACTERS
            p = profile()
            value = selected.GetValue()
            if not value.strip():
                raise ValueError('Load or paste the exact document text first.')
            if len(batch_items) >= MAX_ITEMS or sum(len(row['text']) for row in batch_items) + len(value) > MAX_CHARACTERS:
                raise ValueError('A translation batch is limited to 32 documents and two million characters.')
            item = {'profile': p.to_dict(), 'text': value,
                'title': source_record.get('title', 'Library source') if source_record is not None else 'Selected text',
                'source': source.GetValue().strip(), 'target': target.GetValue().strip(),
                'budget': str(money(budget.GetValue())), 'glossary': glossary.GetValue().strip() or None,
                'rate_per_million': rate.GetValue().strip() or None, **source_arguments()}
            batch_items.append(json.loads(json.dumps(item, ensure_ascii=False)))
            report(f'Added document {len(batch_items)} with its exact source and settings. No text sent. '
                   'Load the next source and add it, or open Document batches to review order, cost and approval.')
        except Exception as error:
            report(str(error))

    def document_batches(event=None):
        nonlocal batch_items
        try:
            check_library()
            if busy or getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise RuntimeError('Finish the active operation before opening document batches.')
            from .translation_batch_dialog import show_translation_batch
            batch_items = show_translation_batch(frame, items=batch_items)
            if alive and not getattr(frame, '_closing', False):
                report(f'Draft retains {len(batch_items)} documents. Add another source or reopen its batch review.')
                controls()
        except Exception as error:
            report(str(error))

    def close(event=None):
        nonlocal alive
        cancel.set()
        alive = False
        dialog.EndModal(wx.ID_CLOSE)

    for name, handler in {'load_source': load_source, 'paste': paste_mode,
                           'add_batch': add_batch, 'batches': document_batches,
                           'languages': languages, 'preview': preview, 'run': run, 'pause': lambda e: cancel.set(),
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
    item = menu.Append(identity, 'Translate selected te&xt...')
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('tools.translation', 'Translate selected text'), item)
    frame.Bind(wx.EVT_MENU, lambda event: show_translation(frame), id=int(identity))

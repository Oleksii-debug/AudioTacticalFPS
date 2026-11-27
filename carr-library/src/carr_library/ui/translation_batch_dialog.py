"""Native ordered document review over the existing translation jobs.

Adapted from the reviewed PR152 batch UI; the Desktop worker remains the sole
queue. File capture, source authority, provider receipts and exports reuse the
current application services.
"""
from decimal import Decimal
import json
from pathlib import Path
import threading
from time import time
from uuid import uuid4

from ..assistant.models import canonical
from ..assistant.profiles import ProfileStore
from ..assistant.providers import Gateway, money, translation_language_approval
from ..assistant.translation import TranslationService
from ..assistant.translation_batch import TranslationBatchService, MAX_ITEMS, MAX_CHARACTERS
from ..assistant.translation_source import capture_source, read_text_file
from ..research.export_file import capture_export_destination

HELP = (
    'Choose explicit UTF-8 files or retained library books. Each added document keeps its selected account, '
    'languages, glossary and USD reservation; change a row only with Apply settings to selected document. '
    'Move up/down sets the exact order. This review supports whole retained books and plain-text files; '
    'the CLI also accepts explicit source positions or one EPUB chapter.\n\n'
    'Review checks every source and shows aggregate character/call counts, source identities and per-document '
    'reservations. Prepare asks permission for that unchanged review and saves ordinary translation jobs; '
    'it sends no text. Run/resume is a separate explicit action. Accepted chunks are reused. A paused document '
    'or uncertain charge stops this invocation; no automatic paid retry or new approval is inferred.\n\n'
    'Saved batches retain their existing approvals and expiry. Restore pauses the batch. A source-bound batch '
    'restored to a different library identity needs a new explicit review for further remote work; completed '
    'receipts remain available for offline export. Export requires all '
    'documents verified complete and a newly chosen ZIP path. The package contains separate original, translated, '
    'bilingual, reading-copy and provenance files. Original formatting is not retained; originals and quotation '
    'authority remain unchanged. Limits: 32 documents and two million source/translated package characters.\n'
    'Tab/Shift+Tab visit named controls; Escape closes and requests cancellation.'
)


def create_translation_batch_dialog(frame, *, items=(), wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    library, root, scope = frame.library, frame.library.root, getattr(frame, 'active', None)
    root_stat = root.stat()
    root_identity = root_stat.st_dev, root_stat.st_ino
    service = TranslationService(root / 'state' / 'translation', library=library)
    batches = TranslationBatchService(service)
    accounts = ProfileStore(root / 'state' / 'providers')
    profiles = tuple(p for p in accounts.list()
                     if p.adapter in ('google-translation-v2', 'google-translation-v3', 'libretranslate-local') and p.enabled)
    draft = json.loads(canonical(list(items)))
    if (len(draft) > MAX_ITEMS or any(type(row) is not dict or type(row.get('text')) is not str for row in draft)
            or sum(len(row['text']) for row in draft) > MAX_CHARACTERS):
        raise ValueError('Choose at most 32 documents and two million characters for the batch.')
    alive, busy, current, reviewed, saved_review = True, False, None, None, None
    cancel = threading.Event()

    def dispose():
        nonlocal alive
        alive = False
        cancel.set()

    class BatchDialog(wx.Dialog):
        def Destroy(self):
            dispose()
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

    dialog = BatchDialog(frame, title='Translation — reviewed document batch', size=(1040, 930),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    native_id = dialog.GetId()
    dialog.SetName('Translation document batch')
    def destroyed(event):
        if event.GetId() == native_id:
            dialog._destroy_requested = True
            dispose()
        event.Skip()
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, cls=wx.TextCtrl, proportion=0, **options):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 3)
        control = cls(dialog, **options)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.ALL | wx.EXPAND, 3)
        return control

    title = field('Translation batch &title', value='Translation batch')
    provider = field('Account for added or selected documents', wx.Choice,
                     choices=[f'{p.name} ({p.id}); {p.adapter}' for p in profiles])
    if profiles:
        provider.SetSelection(0)
    source = field('Source language code; auto detects', value='auto')
    target = field('Target language code', value='uk')
    glossary = field('Advanced glossary resource; blank for none', value='')
    budget = field('Per-document USD reservation; enter 0 for local translation', value='')
    rate = field('Optional contract rate per million characters', value='')
    selected = field('Ordered translation &documents', wx.Choice, choices=[])
    status = field('Translation batch review and existing checkpoints', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY, value=HELP)
    buttons = {}
    for entries in (
        (('files', 'Add UTF-8 &files...'), ('books', 'Add library &books...'),
         ('apply', '&Apply settings to selected document'), ('languages', 'Fetch account &languages...')),
        (('up', 'Move document &up'), ('down', 'Move document &down'), ('remove', '&Remove document')),
        (('review', '&Review entire translation batch'), ('prepare', '&Prepare approved batch'),
         ('run', 'Run or re&sume batch'), ('pause', 'Pa&use batch')),
        (('saved', 'Saved translation batches'), ('new', '&New batch draft'),
         ('export', '&Export completed ZIP...'), ('help', '&Help'), ('close', '&Close batch')),
    ):
        row = wx.WrapSizer(wx.HORIZONTAL)
        for name, label in entries:
            control = wx.Button(dialog, wx.ID_CANCEL if name == 'close' else wx.ID_ANY, label=label)
            control.SetName(label.replace('&', ''))
            buttons[name] = control
            row.Add(control, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)

    def guard():
        if not alive:
            raise InterruptedError('The translation batch window was closed.')
        if (frame.library is not library or frame.library.root != root
                or getattr(frame, 'active', None) != scope or getattr(frame, '_closing', False)):
            raise PermissionError('The active library or topic changed. Reopen the batch in its original scope.')
        found = root.stat()
        if (found.st_dev, found.st_ino) != root_identity:
            raise PermissionError('The library directory changed. Reopen the batch.')
        if scope is not None:
            from ..research.topic_tree import TopicTree
            with library.read(scope[0]) as db:
                if scope[1] is not None:
                    TopicTree(db, scope[0])._visible(scope[1])

    def stopped():
        return (cancel.is_set() or not alive or frame.cancel_event.is_set()
                or getattr(frame, '_closing', False) or frame.library is not library
                or getattr(frame, 'active', None) != scope)

    def report(value):
        if alive and dialog and not getattr(frame, '_closing', False):
            status.ChangeValue(str(value))

    def display(value):
        return json.dumps(value, ensure_ascii=False, indent=2)

    def settings():
        guard()
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Configure and explicitly choose an enabled translation account.')
        p = profiles[index]
        if accounts.get(p.id) != p or not p.enabled:
            raise PermissionError('The selected account changed. Reopen its selection.')
        if p.local and money(budget.GetValue()) != 0:
            raise ValueError('Local translation requires an explicit zero external cost bound.')
        return dict(profile=p.to_dict(), source=source.GetValue().strip(), target=target.GetValue().strip(),
                    glossary=glossary.GetValue().strip() or None, budget=str(money(budget.GetValue())),
                    rate_per_million=rate.GetValue().strip() or None)

    def require_accounts(rows):
        guard()
        for row in rows:
            if accounts.get(row['profile']['id']).to_dict() != row['profile'] or not row['profile']['enabled']:
                raise PermissionError('A reviewed account changed or was disabled. Review a new batch before transmission.')

    def scope_review(review):
        guard()
        for row in review['items']:
            anchor = row.get('source_anchor')
            if anchor is not None and (scope is None
                    or (anchor['workspace'], row.get('source_topic')) != scope):
                raise PermissionError('Choose the original workspace and topic for this source-bound batch.')
        return review

    def controls():
        if not alive or not dialog:
            return
        for key, button in buttons.items():
            button.Enable(not busy or key in ('pause', 'close'))
        for key in ('files', 'books', 'apply', 'up', 'down', 'remove'):
            buttons[key].Enable(not busy and current is None and
                                (key in ('files', 'books') or bool(draft)))
        buttons['review'].Enable(not busy and current is None and bool(draft))
        buttons['prepare'].Enable(not busy and (current is not None or reviewed is not None))
        buttons['run'].Enable(not busy and current is not None)
        buttons['export'].Enable(not busy and current is not None)
        buttons['pause'].Enable(busy)
        for control in (title, provider, source, target, glossary, budget, rate):
            control.Enable(not busy and current is None)
        selected.Enable(not busy)

    def refresh():
        if not alive or not dialog:
            return
        rows = saved_review['items'] if current is not None else draft
        labels = [f"{index}. {row['title']}; {row.get('source', 'auto')} → {row['target']}; "
                  f"{row['profile']['name']}; {row['budget']} USD" for index, row in enumerate(rows, 1)]
        selected.SetItems(labels)
        if labels:
            selected.SetSelection(0)
        controls()

    def begin(label, operation, complete, *, committed=False):
        nonlocal busy
        if not alive or busy:
            return False
        try:
            guard()
            if getattr(frame, 'working', False):
                raise RuntimeError('Finish or cancel the current operation before starting a batch action.')
            busy = True
            cancel.clear()
            controls()
            def checked():
                guard()
                if stopped():
                    raise InterruptedError('Translation batch operation cancelled before work.')
                result = operation()
                if not committed and stopped():
                    raise InterruptedError('Translation batch operation stopped before review delivery.')
                return result
            def finished(result):
                nonlocal busy
                busy = False
                if alive and dialog:
                    try:
                        guard()
                        complete(result)
                        controls()
                        if alive and dialog:
                            status.SetFocus()
                    except Exception as error:
                        report(str(error))
                        controls()
            def failed(error):
                nonlocal busy
                busy = False
                if alive and dialog:
                    report(str(error))
                    controls()
            accepted = frame.run_job(label, checked, done=finished, failed=failed)
            if accepted is False or (busy and not getattr(frame, 'working', False)):
                busy = False
                controls()
                return False
            return True
        except (Exception, KeyboardInterrupt) as error:
            busy = False
            controls()
            report(str(error))
            return False

    def append_rows(rows):
        nonlocal reviewed
        guard()
        candidate = draft + rows
        if len(candidate) > MAX_ITEMS or sum(len(row['text']) for row in candidate) > MAX_CHARACTERS:
            raise ValueError('This batch exceeds 32 documents or two million characters.')
        draft[:] = json.loads(canonical(candidate))
        reviewed = None
        refresh()
        report(f'{len(draft)} explicit documents. Review the entire ordered batch before preparing.')

    def add_files(event=None):
        if not alive or busy or current is not None:
            return
        try:
            chosen = settings()
            chooser = wx.FileDialog(dialog, 'Choose explicit UTF-8 text files',
                wildcard='UTF-8 plain text (*.txt)|*.txt|All files (*.*)|*.*',
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
            try:
                result = chooser.ShowModal()
                if not alive or not chooser or result != wx.ID_OK:
                    return
                guard()
                paths = tuple(Path(value) for value in chooser.GetPaths())
            finally:
                if chooser:
                    chooser.Destroy()
            if not alive:
                return
            if not 1 <= len(paths) <= MAX_ITEMS - len(draft):
                raise ValueError('Choose a bounded number of explicit text files.')
            def work():
                require_accounts((chosen,))
                result = []
                for path in paths:
                    value = read_text_file(path, cancelled=stopped)
                    result.append({**chosen, 'text': value, 'title': path.name})
                    if sum(len(row['text']) for row in result) + sum(len(row['text']) for row in draft) > MAX_CHARACTERS:
                        raise ValueError('The batch exceeds two million characters.')
                return result
            begin('Capturing selected UTF-8 documents', work, append_rows)
        except Exception as error:
            report(str(error))

    def add_books(event=None):
        if not alive or busy or current is not None:
            return
        try:
            chosen = settings()
            if scope is None:
                raise ValueError('Choose the current library workspace before selecting retained books.')
            def choose(rows):
                guard()
                if not rows:
                    report('This workspace has no retained books.'); return
                chooser = wx.MultiChoiceDialog(dialog,
                    'Choose explicit whole books; showing up to 200. The aggregate text limit is two million characters.',
                    'Translation source books',
                    [f'{row.title or "Untitled"}; {row.edition_id}' for row in rows[:200]])
                try:
                    answer = chooser.ShowModal()
                    if not alive or not chooser or answer != wx.ID_OK:
                        return
                    guard()
                    indices = tuple(chooser.GetSelections())
                finally:
                    if chooser:
                        chooser.Destroy()
                if not alive:
                    return
                if (not 1 <= len(indices) <= MAX_ITEMS - len(draft)
                        or len(set(indices)) != len(indices)
                        or any(type(index) is not int or not 0 <= index < min(200, len(rows)) for index in indices)):
                    raise ValueError('Choose valid explicit book rows within the batch limit.')
                ids = tuple(rows[index].edition_id for index in indices)
                def capture():
                    require_accounts((chosen,))
                    result = []
                    for edition in ids:
                        record = capture_source(library, scope[0], edition, topic=scope[1],
                                                cancelled=stopped, guard=guard)
                        result.append({**chosen, **{key: record[key] for key in
                            ('text', 'title', 'source_anchor', 'source_metadata', 'source_topic')}})
                        if sum(len(row['text']) for row in result) + sum(len(row['text']) for row in draft) > MAX_CHARACTERS:
                            raise ValueError('The batch exceeds two million characters.')
                    return result
                begin('Capturing retained source books', capture, append_rows)
            begin('Listing retained books for explicit selection',
                  lambda: library.browse(scope[0], limit=201), choose)
        except Exception as error:
            report(str(error))

    def apply(event=None):
        nonlocal reviewed
        if not alive or busy or current is not None:
            return
        try:
            chosen = settings()
            index = selected.GetSelection()
            if not 0 <= index < len(draft):
                return
            draft[index].update(chosen)
            reviewed = None
            refresh()
            selected.SetSelection(index)
            report('Document settings changed. Review the full batch again.')
        except Exception as error:
            report(str(error))

    def edit(action):
        nonlocal reviewed
        if not alive or busy or current is not None:
            return
        try:
            guard()
            index = selected.GetSelection()
            if not 0 <= index < len(draft):
                return
            if action == 'remove':
                draft.pop(index)
            else:
                other = index + (-1 if action == 'up' else 1)
                if not 0 <= other < len(draft):
                    return
                draft[index], draft[other] = draft[other], draft[index]
                index = other
            reviewed = None
            refresh()
            if draft:
                selected.SetSelection(min(index, len(draft) - 1))
            report('Draft order changed. Review all documents again.')
        except Exception as error:
            report(str(error))

    def languages(event=None):
        if not alive or busy:
            return
        try:
            guard()
            index = provider.GetSelection()
            if not 0 <= index < len(profiles):
                raise ValueError('Choose an enabled translation account.')
            p = profiles[index]
            if accounts.get(p.id) != p:
                raise PermissionError('The account changed. Reopen the batch.')
            chooser = wx.MessageDialog(dialog,
                f'Fetch the declared supported languages from {p.endpoint} using {p.name}? '
                ('No document text is sent. This contacts only the selected local endpoint.' if p.local else
                 'No document text is sent. This is an explicit remote account request.'),
                'Fetch translation languages', wx.YES_NO | wx.NO_DEFAULT)
            try:
                answer = chooser.ShowModal()
                if not alive or not chooser or answer != wx.ID_YES:
                    return
                guard()
            finally:
                if chooser:
                    chooser.Destroy()
            if not alive:
                return
            def work():
                def current_account():
                    guard()
                    if accounts.get(p.id) != p or not p.enabled:
                        raise PermissionError('The selected account was disabled or changed.')
                current_account()
                service.gateway = Gateway(service.directory / 'provider',
                                          mode='LOCAL_ONLY' if p.local else 'APPROVED_REMOTE')
                current_account()
                authorization = translation_language_approval(p, expires_at=time() + 600)
                def cancelled():
                    current_account()
                    return stopped()
                return service.refresh_languages(p, authorization=authorization, cancelled=cancelled,
                                                 interrupt_cancelled=stopped)
            begin('Fetching explicitly requested translation language declarations', work,
                  lambda rows: report(display(rows) + '\nProvider declarations are not translation-quality evidence.'))
        except Exception as error:
            report(str(error))

    def review(event=None):
        nonlocal reviewed
        if not alive or busy or current is not None:
            return
        try:
            guard()
            frozen_title, raw = title.GetValue(), canonical(draft)
            frozen = json.loads(raw)
            reviewed = None
            def ready(result):
                nonlocal reviewed
                reviewed = (frozen_title, raw, result['review_sha256'])
                report(display(result) + '\n\nREVIEW ONLY. Prepare asks for the unchanged transfer approval.')
            def work():
                require_accounts(frozen)
                return scope_review(batches.preview(frozen, title=frozen_title, cancelled=stopped, guard=guard))
            begin('Reviewing every translation document', work, ready)
        except Exception as error:
            report(str(error))

    def retained(identity):
        return scope_review(batches.get_review(identity))

    def prepared(identity, result, review):
        nonlocal current, saved_review
        current, saved_review = identity, review
        refresh()
        report(display(result) + '\nPREPARED — no text sent. Run/resume is a separate explicit action.')

    def prepare(event=None):
        if not alive or busy:
            return
        try:
            guard()
            if current is not None:
                identity = current
                def work():
                    review = retained(identity)
                    return batches.prepare(identity, cancelled=stopped, guard=guard), review
                begin('Finishing saved translation preparation', work,
                      lambda value: prepared(identity, value[0], value[1]), committed=True)
                return
            frozen_title, raw = title.GetValue(), canonical(draft)
            if reviewed is None or reviewed[:2] != (frozen_title, raw):
                raise ValueError('Review this exact order, text, settings and reservations before approval.')
            frozen, review_hash = json.loads(raw), reviewed[2]
            total = sum((Decimal(row['budget']) for row in frozen), Decimal(0))
            local_transfer = any(row['profile']['local'] for row in frozen)
            remote_transfer = any(not row['profile']['local'] for row in frozen)
            cost_notice = ('Provider terms and charges apply; this is not a provider billing cap.'
                           if remote_transfer else 'All selected transfers use local endpoints with zero external cost.')
            accounts_text = '\n'.join(f"{index}. {row['title']}: {row['profile']['endpoint']}; "
                f"model {row['profile']['model']}; {row['source']} → {row['target']}; "
                f"glossary {row.get('glossary') or 'none'}; {row['budget']} USD"
                for index, row in enumerate(frozen, 1))
            chooser = wx.MessageDialog(dialog,
                f'Approve these {len(frozen)} documents and {sum(len(row["text"]) for row in frozen)} characters?\n'
                f'Total reservation: {total} USD. {cost_notice}\n'
                f'Review SHA-256: {review_hash}\n{accounts_text}\n\nPrepare saves approvals only. Run/resume sends text.',
                'Approve exact translation batch', wx.YES_NO | wx.NO_DEFAULT)
            try:
                answer = chooser.ShowModal()
                if not alive or not chooser or answer != wx.ID_YES:
                    return
                guard()
            finally:
                if chooser:
                    chooser.Destroy()
            if not alive:
                return
            def work():
                require_accounts(frozen)
                identity = batches.submit(frozen, title=frozen_title, review_sha256=review_hash,
                                          approve_remote=remote_transfer, approve_local=local_transfer,
                                          cancelled=stopped, guard=guard)
                # These reads verify the committed record. Window lifetime was
                # checked before submission and cannot revoke a saved approval.
                return identity, batches.get(identity), batches.get_review(identity)
            begin('Preparing approved translation documents', work,
                  lambda value: prepared(value[0], value[1], value[2]), committed=True)
        except Exception as error:
            report(str(error))

    def run(event=None):
        if not alive or busy or current is None:
            return
        identity = current
        def work():
            review = retained(identity)
            require_accounts(review['items'])
            local_transfer = any(row['profile']['local'] for row in review['items'])
            remote_transfer = any(not row['profile']['local'] for row in review['items'])
            mode = ('HYBRID' if local_transfer else 'APPROVED_REMOTE') if remote_transfer else 'LOCAL_ONLY'
            service.gateway = Gateway(service.directory / 'provider', mode=mode)
            require_accounts(review['items'])
            from ..progress import current_progress
            progress = current_progress()
            def advanced(done, total, phase):
                if progress is not None:
                    progress.update(completed=done, total=total, phase=phase)
                if alive:
                    wx.CallAfter(report, f'{done}/{total}: {phase}')
            return batches.resume(identity, cancelled=stopped, progress=advanced,
                                  guard=lambda: require_accounts(review['items']))
        begin('Translating explicitly approved document batch', work,
              lambda value: report(display(value) + '\nAccepted chunks were retained; uncertain charges require reconciliation.'),
              committed=True)

    def choose_saved(rows):
        if not alive:
            return
        guard()
        chooser = wx.SingleChoiceDialog(dialog, 'Select an existing batch; this never renews its approvals.',
            'Saved translation batches',
            [f'{row["title"]}; {row["status"]}; {row["completed"]}/{row["total"]}' for row in rows])
        try:
            answer = chooser.ShowModal()
            if not alive or not chooser or answer != wx.ID_OK:
                return
            guard()
            index = chooser.GetSelection()
        finally:
            if chooser:
                chooser.Destroy()
        if not alive or not 0 <= index < len(rows):
            return
        identity = rows[index]['id']
        def loaded(value):
            nonlocal current, saved_review, reviewed
            result, saved_review = value
            current, reviewed = identity, None
            title.ChangeValue(saved_review['title'])
            refresh()
            report(display(saved_review) + '\nExisting checkpoints:\n' + display(result)
                   + '\nRun/resume remains an explicit action; opening this review sent nothing.')
        begin('Reading saved translation approval',
              lambda: (batches.get(identity), retained(identity)), loaded)

    def saved(event=None):
        begin('Listing saved translation batches', lambda: batches.list(cancelled=stopped), choose_saved)

    def new(event=None):
        nonlocal current, reviewed, saved_review
        if not alive or busy:
            return
        try:
            guard()
            current = reviewed = saved_review = None
            draft.clear()
            title.ChangeValue('Translation batch')
            refresh()
            report(HELP)
        except Exception as error:
            report(str(error))

    def export(event=None):
        if not alive or busy or current is None:
            return
        try:
            guard()
            identity = current
            chooser = wx.FileDialog(dialog, 'Choose a NEW plain-text translation ZIP',
                wildcard='Translation package (*.zip)|*.zip',
                defaultFile='TranslationBatch-' + identity[:8] + '-' + uuid4().hex[:8] + '.zip',
                style=wx.FD_SAVE)
            try:
                answer = chooser.ShowModal()
                if not alive or not chooser or answer != wx.ID_OK:
                    return
                guard()
                output = Path(chooser.GetPath())
                approval = capture_export_destination(output, private_root=root, cancelled=stopped)
                if not approval.require_absent:
                    raise FileExistsError('Choose a new package; replacement is not authorized.')
            finally:
                if chooser:
                    chooser.Destroy()
            if not alive:
                return
            def work():
                retained(identity)
                return batches.export(identity, output, cancelled=stopped, guard=guard, approval=approval)
            begin('Publishing verified translation ZIP', work,
                  lambda result: report('Saved translation package: ' + result['path']
                      + ('\n' + result['cleanup_warning'] if result.get('cleanup_warning') else '')),
                  committed=True)
        except Exception as error:
            report(str(error))

    def pause(event=None):
        if alive and busy:
            cancel.set()

    def close(event=None):
        if not alive:
            return
        dispose()
        if dialog:
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)
            else:
                dialog.Hide()

    handlers = {'files': add_files, 'books': add_books, 'apply': apply, 'languages': languages,
        'up': lambda event: edit('up'), 'down': lambda event: edit('down'), 'remove': lambda event: edit('remove'),
        'review': review, 'prepare': prepare, 'run': run, 'pause': pause, 'saved': saved, 'new': new,
        'export': export, 'help': lambda event: report(HELP), 'close': close}
    for name, callback in handlers.items():
        buttons[name].Bind(wx.EVT_BUTTON, callback)
    dialog.Bind(wx.EVT_CLOSE, close)
    refresh()
    title.SetFocus()
    return dialog, close, lambda: json.loads(canonical(draft))


def show_translation_batch(frame, *, items=()):
    dialog, close, draft = create_translation_batch_dialog(frame, items=items)
    try:
        dialog.ShowModal()
        return draft()
    finally:
        close()
        if dialog:
            dialog.Destroy()

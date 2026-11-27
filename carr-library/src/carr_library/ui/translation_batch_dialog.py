"""Native ordered document review over the existing translation jobs."""
from decimal import Decimal
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..assistant.models import canonical
from ..assistant.providers import Gateway
from ..assistant.translation import TranslationService
from ..assistant.translation_batch import TranslationBatchService, MAX_ITEMS, MAX_CHARACTERS


HELP = (
    'Load a library source or paste exact text in the translation workspace, choose its account, languages, '
    'glossary and reservation, then Add current source to batch. Close this window to add the next document. '
    'Each row keeps its own settings. Move up/down fixes the explicit order.\n\n'
    'Review checks every source and shows character/call counts, original identities, exact providers and '
    'per-document USD reservations. Prepare asks permission for that unchanged review and saves ordinary '
    'translation jobs; it sends no text. Run/resume is a separate explicit action. '
    'Accepted chunks are reused. A paused document or uncertain provider charge stops this invocation; '
    'no automatic paid retry or new approval is inferred.\n\n'
    'Saved batches retain their existing approvals and expiry. Export requires all documents to be verified '
    'complete and an explicitly chosen new directory, including after restore. Originals remain unchanged; '
    'translated text has no source-quotation authority. Limits: 32 documents and two million selected characters. '
    'Tab/Shift+Tab visit named controls; Escape closes and requests cancellation.'
)


def create_translation_batch_dialog(frame, *, items=(), wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    library, root, scope = frame.library, frame.library.root, getattr(frame, 'active', None)
    service = TranslationService(root / 'state' / 'translation', library=library)
    batches = TranslationBatchService(service)
    draft = json.loads(canonical(list(items)))
    if (len(draft) > MAX_ITEMS or any(type(row) is not dict or type(row.get('text')) is not str for row in draft)
            or sum(len(row['text']) for row in draft) > MAX_CHARACTERS):
        raise ValueError('Choose at most 32 documents and two million characters for the batch.')
    dialog = wx.Dialog(frame, title='Translation — reviewed document batch', size=(1000, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Translation document batch')
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, cls=wx.TextCtrl, proportion=0, **options):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = cls(dialog, **options)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
        return control

    title = field('Translation batch &title', value='Translation batch')
    selected = field('Ordered translation &documents', wx.Choice, choices=[])
    status = field('Translation batch review and existing checkpoints', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY, value=HELP)
    buttons = {}
    for entries in (
        (('up', 'Move document &up'), ('down', 'Move document &down'), ('remove', '&Remove document')),
        (('review', '&Review entire translation batch'), ('prepare', '&Prepare approved batch'),
         ('run', 'Run or re&sume batch'), ('pause', 'Pa&use batch')),
        (('saved', 'Saved translation &batches'), ('new', '&New batch draft'), ('export', '&Export completed batch'),
         ('help', '&Help'), ('close', '&Close batch')),
    ):
        row = wx.BoxSizer(wx.HORIZONTAL)
        for name, label in entries:
            control = wx.Button(dialog, wx.ID_CANCEL if name == 'close' else wx.ID_ANY, label=label)
            control.SetName(label.replace('&', ''))
            buttons[name] = control
            row.Add(control, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    alive, busy, current, reviewed, saved_review = True, False, None, None, None
    cancel = threading.Event()

    def guard():
        if (frame.library is not library or frame.library.root != root
                or getattr(frame, 'active', None) != scope):
            raise PermissionError('The active library or topic changed. Reopen the batch in its original scope.')

    def stopped():
        return (cancel.is_set() or not alive or frame.cancel_event.is_set()
                or getattr(frame, '_closing', False) or frame.library is not library
                or getattr(frame, 'active', None) != scope)

    service.source_guard = guard

    def scope_review(review):
        guard()
        for row in review['items']:
            anchor = row.get('source_anchor')
            if anchor is not None and (scope is None
                    or (anchor['workspace'], row.get('source_topic')) != scope):
                raise PermissionError('Choose the original workspace and topic before opening this source-bound batch.')
        return review

    def report(value):
        if alive and not getattr(frame, '_closing', False):
            status.ChangeValue(str(value))

    def display(value):
        return json.dumps(value, ensure_ascii=False, indent=2)

    def snapshot():
        return title.GetValue(), canonical(draft)

    def controls():
        if not alive:
            return
        for key, button in buttons.items():
            button.Enable(not busy or key in ('pause', 'close'))
        for key in ('up', 'down', 'remove'):
            buttons[key].Enable(not busy and current is None and bool(draft))
        buttons['review'].Enable(not busy and current is None and bool(draft))
        buttons['prepare'].Enable(not busy and (current is not None or reviewed is not None))
        buttons['run'].Enable(not busy and current is not None)
        buttons['export'].Enable(not busy and current is not None)
        buttons['pause'].Enable(busy)
        title.Enable(not busy and current is None)
        selected.Enable(not busy)

    def refresh():
        rows = saved_review['items'] if current is not None else draft
        labels = []
        for index, row in enumerate(rows, 1):
            profile = row['profile']
            labels.append(f"{index}. {row['title']}; {row.get('source', 'auto')} → {row['target']}; "
                          f"{profile['name']}; {row['budget']} USD")
        selected.SetItems(labels)
        if labels:
            selected.SetSelection(0)
        controls()

    def begin(label, operation, complete):
        nonlocal busy
        if not alive or busy:
            return False
        try:
            guard()
            if getattr(frame, '_closing', False) or getattr(frame, 'working', False):
                raise RuntimeError('Finish or cancel the active operation before starting a batch action.')
            busy = True
            cancel.clear()
            controls()
            def checked():
                guard()
                if stopped():
                    raise InterruptedError('Translation batch operation cancelled before work.')
                return operation()
            def finished(result):
                nonlocal busy
                busy = False
                if alive and not getattr(frame, '_closing', False):
                    controls()
                    try:
                        guard()
                        complete(result)
                    except Exception as error:
                        report(str(error))
                    status.SetFocus()
            def failed(error):
                nonlocal busy
                busy = False
                if alive and not getattr(frame, '_closing', False):
                    controls()
                    report(str(error))
                    status.SetFocus()
            if frame.run_job(label, checked, done=finished, failed=failed) is False:
                busy = False
                controls()
                return False
            return True
        except Exception as error:
            busy = False
            controls()
            report(str(error))
            return False

    def edit(action):
        nonlocal reviewed
        if busy or current is not None:
            return
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
        report('Draft order changed. Review all documents again before approval.')

    def review(event=None):
        nonlocal reviewed
        frozen_title, raw = snapshot()
        frozen = json.loads(raw)
        reviewed = None
        def ready(result):
            nonlocal reviewed
            reviewed = (frozen_title, raw, result['review_sha256'])
            report(display(result) + '\n\nREVIEW ONLY — no provider request or new approval. Prepare asks permission.')
            controls()
        begin('Reviewing every translation document',
              lambda: scope_review(batches.preview(frozen, title=frozen_title, cancelled=stopped)), ready)

    def retained(identity):
        return scope_review(batches.get_review(identity))

    def prepared(identity, result, review):
        nonlocal current, saved_review
        current, saved_review = identity, review
        refresh()
        report(display(result) + '\n\nPREPARED — no text sent. Run/resume uses only the saved document approvals.')

    def prepare(event=None):
        if current is not None:
            identity = current
            def work():
                review = retained(identity)
                result = batches.prepare(identity, cancelled=stopped)
                return result, review
            begin('Finishing saved translation preparation', work,
                  lambda value: prepared(identity, value[0], value[1]))
            return
        frozen_title, raw = snapshot()
        if reviewed is None or reviewed[:2] != (frozen_title, raw):
            report('Review this exact document order, text, settings and reservations before approval.')
            return
        frozen, review_hash = json.loads(raw), reviewed[2]
        def approve_and_start():
            guard()
            # The full immutable per-item review is already visible; repeat the
            # transfer and total reservation in the explicit native confirmation.
            total = sum((Decimal(row['budget']) for row in frozen), Decimal(0))
            accounts = '\n'.join(f"{index}. {row['title']}: {row['profile']['endpoint']}; "
                f"model {row['profile']['model']}; {row['source']} → {row['target']}; "
                f"glossary {row.get('glossary') or 'none'}; reservation {row['budget']} USD"
                for index, row in enumerate(frozen, 1))
            message = (f"Approve transfer of these {len(frozen)} documents, "
                f"{sum(len(row['text']) for row in frozen)} exact characters, in this order?\n"
                f"Total local reservation: {total} USD. Provider terms and charges apply; this is not a "
                f"provider-enforced billing limit.\nReview SHA-256: {review_hash}\n{accounts}\n\n"
                "Prepare saves approvals only. A separate Run/resume starts transmission.")
            if wx.MessageBox(message, 'Approve exact translation batch', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            guard()
            def work():
                identity = batches.submit(frozen, title=frozen_title, review_sha256=review_hash,
                                          approve_remote=True, cancelled=stopped)
                return identity, batches.get(identity), retained(identity)
            begin('Preparing approved translation documents', work,
                  lambda value: prepared(value[0], value[1], value[2]))
        try:
            approve_and_start()
        except Exception as error:
            report(str(error))

    def run(event=None):
        if current is None:
            report('Prepare an explicitly approved batch or select its saved review first.')
            return
        identity = current
        def work():
            retained(identity)
            service.gateway = Gateway(service.directory / 'provider', mode='APPROVED_REMOTE')
            from ..progress import current_progress
            progress = current_progress()
            def advanced(done, total, phase):
                if progress is not None:
                    progress.update(completed=done, total=total, phase=phase)
                wx.CallAfter(report, f'{done}/{total}: {phase}')
            return batches.resume(identity, cancelled=stopped, progress=advanced)
        begin('Translating approved document batch', work,
              lambda result: report(display(result) + '\nAccepted chunks are reused; uncertain charges require reconciliation.'))

    def choose_saved(rows):
        picker = wx.SingleChoiceDialog(dialog, 'Select an existing batch; this never renews its approvals.',
            'Saved translation batches', [f"{row['title']}; {row['status']}; {row['completed']}/{row['total']}"
                                           for row in rows])
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            index = picker.GetSelection()
        finally:
            picker.Destroy()
        if not 0 <= index < len(rows):
            return
        identity = rows[index]['id']
        def loaded(value):
            nonlocal current, saved_review, reviewed
            result, saved_review = value
            current, reviewed = identity, None
            title.ChangeValue(saved_review['title'])
            refresh()
            report(display(saved_review) + '\n\nExisting checkpoints:\n' + display(result)
                   + '\nSelect Run/resume explicitly. No approval or provider call was created by opening this review.')
        begin('Reading saved translation approval',
              lambda: (batches.get(identity), retained(identity)), loaded)

    def saved(event=None):
        begin('Listing existing translation batches', batches.list, choose_saved)

    def new(event=None):
        nonlocal current, reviewed, saved_review
        current = reviewed = saved_review = None
        title.ChangeValue('Translation batch')
        refresh()
        report(HELP)

    def export(event=None):
        if current is None:
            return
        identity = current
        picker = wx.DirDialog(dialog, 'Choose parent folder for a NEW completed translation batch export')
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            output = Path(picker.GetPath()) / ('TranslationBatch-' + identity[:8] + '-' + uuid4().hex[:8])
        finally:
            picker.Destroy()
        def work():
            retained(identity)
            return batches.export(identity, output, cancelled=stopped)
        begin('Publishing verified translation documents', work,
              lambda result: report('Saved new batch files in: ' + result['path']))

    def close(event=None):
        nonlocal alive
        cancel.set()
        if alive:
            alive = False
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    handlers = {'up': lambda event: edit('up'), 'down': lambda event: edit('down'),
        'remove': lambda event: edit('remove'), 'review': review, 'prepare': prepare, 'run': run,
        'pause': lambda event: cancel.set(), 'saved': saved, 'new': new, 'export': export,
        'help': lambda event: report(HELP), 'close': close}
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
        dialog.Destroy()

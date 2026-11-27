"""Native multi-book review using the existing Book dialog and Studio jobs."""
import json
import threading

from ..studio.book_batch import BookBatchService, MAX_BOOKS, select_books


HELP = (
    'Choose existing books from the active library. Import files or folders through the existing Import Center first. '
    'Use Review selected book for each entry: its source range, split, voice and supported controls, format, names and output folder '
    'are reviewed in the existing Book audio window. Move up/down sets the explicit execution order.\n\n'
    'Preview rechecks all sources and exact destinations. Prepare saves ordinary Studio jobs and starts no speech. '
    'Run is separate and explicit. A stopped or failed item stops this invocation; another explicit Run verifies '
    'completed outputs and resumes the existing jobs. No second audio queue or provider is created.\n\n'
    'A saved batch freezes order and settings. New batch starts another review without changing saved work. '
    'After restore, choose Fresh output reviews, then review every new folder and Prepare. Historical jobs remain unchanged. Stored checkpoints do not prove output '
    'integrity until explicit Run verifies the actual files. Pause/Cancel retain recoverable Studio items.\n\n'
    'Limits: 32 books, 256 total parts and eight million selected characters. Duration estimates are not split '
    'boundaries. Tab/Shift+Tab visit named controls; Escape closes and withdraws this window’s active operation.'
)


def batch_review_text(batch, plans, review):
    from .book_audio_dialog import review_text
    return '\n\n'.join([
        'PREPARED — Run is separate.' if review.prepared else 'BATCH PREVIEW — no audio started.',
        batch.title + '; ' + str(len(plans)) + ' books in the displayed order.',
        'Batch identity: ' + batch.id, 'Batch review SHA-256: ' + review.fingerprint,
        *['BOOK %d\n%s' % (index, review_text(plan, item))
          for index, (plan, item) in enumerate(zip(plans, review.books), 1)]])


def create_book_batch_dialog(frame, *, service, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    scope = getattr(frame, 'active', None)
    if scope is None or service.library is not frame.library or service.workspace != scope[0]:
        raise ValueError('Open a multi-book review inside its existing scoped Book audio workspace.')
    library, root = frame.library, frame.library.root
    groups = BookBatchService(service)
    dialog = wx.Dialog(frame, title='Multiple books — reviewed audio batch', size=(980, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Multiple book audio workspace')
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, cls=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        widget = cls(dialog, **kwargs)
        widget.SetName(label.replace('&', ''))
        layout.Add(widget, proportion, wx.ALL | wx.EXPAND, 4)
        return widget

    title = field('Batch &title', value='Book audio batch')
    query = field('Library title &filter; literal text, blank for first 500 books')
    selected = field('Ordered &books; review each book before Preview', wx.Choice, choices=[])
    status = field('Multiple book review and existing Studio checkpoints', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY, value=HELP)
    buttons = {}
    for items in [
        [('choose', 'Choose library &books'), ('edit', '&Review selected book'),
         ('up', 'Move selected book &up'), ('down', 'Move selected book &down'), ('remove', 'Remove selected book')],
        [('preview', '&Preview entire batch'), ('prepare', '&Prepare batch items'),
         ('run', '&Run or resume batch'), ('pause', 'Pause batch'), ('cancel', 'Cancel batch')],
        [('saved', '&Saved batches'), ('fresh', 'Fresh output reviews'), ('new', '&New batch'), ('help', '&Help'), ('close', '&Close')],
    ]:
        row = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in items:
            button = wx.Button(dialog, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            buttons[key] = button
            row.Add(button, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    rows, batch, review, approved_snapshot = [], None, None, None
    alive, busy, locked = True, False, False
    dialog_destroyed, owner_id = False, dialog.GetId()
    dialog._book_destroyed = False
    cancel = threading.Event()
    action = 'pause'

    def guard():
        if not alive or getattr(frame, '_closing', False):
            raise ValueError('The Book batch window is closed.')
        service._check_root()
        if frame.library is not library or frame.library.root != root or getattr(frame, 'active', None) != scope:
            raise ValueError('The active library or topic changed. Reopen the batch in its original scope.')

    def stopped():
        return (cancel.is_set() or not alive or frame.cancel_event.is_set()
                or getattr(frame, '_closing', False) or service._closed.is_set())

    def report(message):
        if alive and not getattr(frame, '_closing', False):
            status.ChangeValue(str(message))

    def snapshot():
        return (title.GetValue(), tuple((row['edition'], row['plan'], row['output']) for row in rows))

    def controls():
        if not alive:
            return
        for key, button in buttons.items():
            button.Enable(not busy or key in ('pause', 'cancel', 'close'))
        for key in ('choose', 'up', 'down', 'remove'):
            buttons[key].Enable(not busy and not locked)
        buttons['edit'].Enable(not busy and bool(rows))
        buttons['preview'].Enable(not busy and bool(rows))
        buttons['prepare'].Enable(not busy and review is not None)
        buttons['run'].Enable(not busy and review is not None and review.prepared)
        buttons['fresh'].Enable(not busy and locked and batch is not None)
        buttons['pause'].Enable(busy or locked)
        buttons['cancel'].Enable(busy or locked)
        title.Enable(not busy and not locked)
        query.Enable(not busy and not locked)
        selected.Enable(not busy)

    def refresh(index=0):
        labels = []
        for number, row in enumerate(rows, 1):
            plan = row['plan']
            detail = ('; review required' if plan is None else
                      '; ' + plan.role.voice_id + '; ' + plan.format + '; ' + str(len(plan.parts)) + ' parts')
            if not row['output']:
                detail += '; output folder required'
            labels.append(f'{number}. {row["title"]}' + detail)
        selected.SetItems(labels)
        if rows:
            selected.SetSelection(max(0, min(index, len(rows) - 1)))
        controls()

    def invalidate(event=None):
        nonlocal batch, review, approved_snapshot
        if not locked:
            batch = None
        review, approved_snapshot = None, None
        controls()
        if event is not None:
            event.Skip()

    def begin(label, operation, done):
        nonlocal busy, action
        if busy:
            return
        try:
            guard()
            if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise ValueError('Finish or stop the current desktop operation first.')
        except Exception as exc:
            report(exc)
            return
        busy, action = True, 'pause'
        cancel.clear()
        controls()

        def checked():
            if stopped():
                raise InterruptedError('The book batch was stopped before starting.')
            guard()
            return operation()

        def finished(value):
            nonlocal busy
            busy = False
            if not alive:
                return
            controls()
            try:
                guard()
                done(value)
            except Exception as exc:
                report(exc)
            if alive and not getattr(frame, '_closing', False):
                status.SetFocus()

        def failed(error):
            nonlocal busy
            busy = False
            if alive:
                controls()
                report(error)
                status.SetFocus()

        if frame.run_job(label, checked, done=finished, failed=failed) is False:
            busy = False
            controls()

    def choose_books(event=None):
        search = query.GetValue().strip()
        def choose(values):
            existing = {row['edition'] for row in rows}
            values = tuple(value for value in values if value.edition_id not in existing)
            if not values:
                report('No additional books match this literal title filter in the active library.')
                return
            picker = wx.MultiChoiceDialog(dialog, 'Choose books; then review each voice, split and output.',
                'Existing library books', [str(value.title or 'Untitled') + '; ' + value.edition_id for value in values])
            try:
                if picker.ShowModal() != wx.ID_OK or not alive:
                    return
                indices = tuple(picker.GetSelections())
            finally:
                if not dialog_destroyed:
                    picker.Destroy()
            if (len(rows) + len(indices) > MAX_BOOKS or len(indices) != len(set(indices))
                    or any(type(index) is not int or not 0 <= index < len(values) for index in indices)):
                raise ValueError('Choose at most 32 distinct books for this batch.')
            rows.extend({'edition': values[index].edition_id, 'title': str(values[index].title or 'Untitled'),
                         'plan': None, 'output': ''} for index in indices)
            invalidate()
            refresh()
            report('Choose each book in order and use Review selected book. No source was rendered.')
        begin('Listing bounded existing library books',
              lambda: library.browse(scope[0], title=search or None, limit=500), choose)

    def selection():
        guard()
        index = selected.GetSelection()
        if busy or not 0 <= index < len(rows):
            raise ValueError('Choose one book while no batch operation is running.')
        return index, rows[index]

    def edit_book(event=None):
        try:
            index, row = selection()
            if getattr(frame, 'working', False):
                raise ValueError('Finish the current desktop operation first.')
            from .book_audio_dialog import create_book_audio_dialog
            def retained(plan, reviewed):
                guard()
                if plan.source['source_anchor']['edition'] != row['edition']:
                    raise ValueError('The reviewed book differs from the selected entry.')
                if locked and (row['plan'] is None or row['plan'].fingerprint != plan.fingerprint):
                    raise ValueError('A saved batch keeps its immutable per-book plan.')
                row.update(plan=plan, title=plan.source['title'], output=reviewed.output_parent)
                invalidate()
                refresh(index)
                report('This book review is captured. Preview the entire batch before Prepare or Run.')
            child, close_child = create_book_audio_dialog(frame, service=service, wx_module=wx,
                edition_id=row['edition'], on_reviewed=retained,
                initial_plan=row['plan'] if locked else None, initial_output=row['output'])
            try:
                child.ShowModal()
            finally:
                close_child()
                if not child._book_destroyed:
                    child.Destroy()
        except Exception as exc:
            report(exc)

    def move(direction):
        try:
            index, row = selection()
            if locked:
                raise ValueError('Start a new batch to change saved order or settings.')
            target = index + direction
            if 0 <= target < len(rows):
                rows[index], rows[target] = rows[target], rows[index]
                invalidate()
                refresh(target)
        except Exception as exc:
            report(exc)

    def remove(event=None):
        try:
            index, row = selection()
            if locked:
                raise ValueError('Start a new batch to change saved order or settings.')
            rows.pop(index)
            invalidate()
            refresh(index)
        except Exception as exc:
            report(exc)

    def preview(event=None):
        nonlocal batch, review, approved_snapshot
        try:
            frozen = snapshot()
            plans = tuple(row['plan'] for row in rows)
            outputs = tuple(row['output'] for row in rows)
            if not plans or any(plan is None for plan in plans) or any(not output for output in outputs):
                raise ValueError('Review every selected book and its explicit output folder first.')
            retained_batch = batch if locked or approved_snapshot == frozen else None
        except Exception as exc:
            report(exc)
            return
        review, approved_snapshot = None, None
        controls()
        def ready(value):
            nonlocal batch, review, approved_snapshot
            if snapshot() != frozen:
                raise ValueError('The selected order, books or output folders changed. Preview again.')
            batch, review = value
            approved_snapshot = frozen
            report(batch_review_text(batch, plans, review))
            controls()
        def work():
            candidate = retained_batch or select_books(plans, title=frozen[0])
            if candidate.topic != scope[1]:
                raise ValueError('The selected books belong to another topic scope.')
            return candidate, groups.review(candidate, outputs, plans=plans, cancelled=stopped, guard=guard)
        begin('Reviewing all selected book sources and output names',
              work, ready)

    def checked_review():
        guard()
        if batch is None or review is None or snapshot() != approved_snapshot:
            raise ValueError('Preview this exact batch order, voices and output folders first.')
        return batch, tuple(row['plan'] for row in rows), tuple(row['output'] for row in rows), review

    def prepare(event=None):
        try:
            candidate, plans, outputs, approved = checked_review()
            if wx.MessageBox('Save %d ordinary Studio parts for these %d reviewed books?\n\n'
                'The displayed per-book voices, formats and output folders are fixed. No speech starts now.' %
                (sum(len(plan.parts) for plan in plans), len(plans)), 'Prepare reviewed batch',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        except Exception as exc:
            report(exc)
            return
        def ready(value):
            nonlocal review, locked
            review, locked = value, True
            report(batch_review_text(candidate, plans, value))
            controls()
        begin('Preparing the existing Studio items for reviewed books',
            lambda: groups.prepare(candidate, outputs, plans=plans, approval=approved.fingerprint,
                                   cancelled=stopped, guard=guard), ready)

    def run(event=None):
        try:
            candidate, plans, outputs, approved = checked_review()
            if not approved.prepared:
                raise ValueError('Prepare and review the actual saved names before Run.')
            if wx.MessageBox('Render these %d reviewed books in the displayed order?\n\n'
                'Existing completed files are verified before reuse. A stopped or failed item stops this run. '
                'This starts local speech.' % len(plans), 'Run reviewed book batch',
                wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        except Exception as exc:
            report(exc)
            return
        begin('Rendering reviewed books through existing Studio jobs',
            lambda: groups.run(candidate.id, outputs, approval=approved.fingerprint, cancelled=stopped,
                cancel_action=lambda: action, guard=guard,
                progress=lambda message: wx.CallAfter(report, message)),
            lambda result: report('Batch invocation: ' + result['status'] + '\n\n'
                                  + json.dumps(result['stored'], ensure_ascii=False, indent=2)))

    def stop(requested):
        nonlocal action
        if busy:
            action = requested
            cancel.set()
            report('Requesting ' + requested + '; waiting for current native cleanup. Saved Studio items remain.')
        elif batch is not None and locked:
            identity = batch.id
            begin('Requesting ' + requested + ' for the active saved batch',
                  lambda: groups.request_stop(identity, requested), lambda result: report(result['notice']))

    def saved(event=None):
        def choose(values):
            values = tuple(value for value in values if value.topic == scope[1])
            if not values:
                report('No saved batches in this library/topic scope.')
                return
            picker = wx.SingleChoiceDialog(dialog, 'Choose an immutable ordered selection.',
                'Saved book batches', [value.title + '; ' + value.id for value in values])
            try:
                if picker.ShowModal() != wx.ID_OK or not alive:
                    return
                index = picker.GetSelection()
            finally:
                if not dialog_destroyed:
                    picker.Destroy()
            if not 0 <= index < len(values):
                raise ValueError('Choose one saved batch.')
            candidate = values[index]
            def loaded(value):
                nonlocal batch, rows, review, approved_snapshot, locked
                plans, stored = value
                batch = candidate
                rows = [{'edition': plan.source['source_anchor']['edition'], 'title': plan.source['title'],
                         'plan': plan, 'output': item['output_parent'] or ''}
                        for plan, item in zip(plans, stored['books'])]
                locked, review, approved_snapshot = True, None, None
                title.ChangeValue(batch.title)
                refresh()
                report(stored['notice'] + '\n\nReview each missing output folder, then Preview and Prepare. '
                       'No saved batch is automatically resumed.\n\n'
                       + json.dumps(stored, ensure_ascii=False, indent=2))
            begin('Reading this batch’s existing Studio checkpoints',
                  lambda: (groups.plans(candidate), groups.status(candidate.id)), loaded)
        begin('Listing saved immutable batch references', groups.saved, choose)

    def fresh_outputs(event=None):
        try:
            guard()
            if batch is None or not locked:
                raise ValueError('Choose a saved batch before creating fresh output reviews.')
            captured = batch
            parents = tuple(row['output'] for row in rows)
            if wx.MessageBox('Create fresh Book and job identities for this saved ordered batch?\n\n'
                    'Historical output receipts stay unchanged. Review every new output folder, '
                    'Preview and Prepare before a separate Run.',
                    'Fresh batch output reviews', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        except Exception as exc:
            report(exc)
            return
        def loaded(value):
            nonlocal batch, rows, review, approved_snapshot, locked
            batch, plans = value
            rows = [{'edition': item.source['source_anchor']['edition'], 'title': item.source['title'],
                     'plan': item, 'output': parent} for item, parent in zip(plans, parents)]
            review, approved_snapshot, locked = None, None, True
            refresh()
            report('Fresh output identities reviewed. Choose each output folder, then Preview and Prepare. '
                   'No jobs or speech have started; the historical batch remains unchanged.')
        begin('Reviewing fresh batch output identities',
              lambda: groups.fresh(captured.id, cancelled=stopped, guard=guard), loaded)

    def new(event=None):
        nonlocal rows, batch, review, approved_snapshot, locked
        rows, batch, review, approved_snapshot, locked = [], None, None, None, False
        title.ChangeValue('Book audio batch')
        refresh()
        report(HELP)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        alive = False
        # The parent Book workspace owns the one service and worker finalizer.
        if getattr(dialog, 'IsModal', lambda: True)():
            dialog.EndModal(wx.ID_CANCEL)

    callbacks = {'choose': choose_books, 'edit': edit_book, 'up': lambda event: move(-1),
        'down': lambda event: move(1), 'remove': remove, 'preview': preview, 'prepare': prepare,
        'run': run, 'pause': lambda event: stop('pause'), 'cancel': lambda event: stop('cancel'),
        'saved': saved, 'fresh': fresh_outputs, 'new': new, 'help': lambda event: (report(HELP), status.SetFocus()), 'close': close}
    for key, callback in callbacks.items():
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    title.Bind(wx.EVT_TEXT, invalidate)
    def destroyed(event):
        nonlocal alive, dialog_destroyed
        if event.GetId() == owner_id:
            alive, dialog_destroyed = False, True
            dialog._book_destroyed = True
            cancel.set()
        event.Skip()

    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    refresh()
    selected.SetFocus()
    return dialog, close


def show_book_batch(frame, *, service):
    dialog, close = create_book_batch_dialog(frame, service=service)
    try:
        dialog.ShowModal()
    finally:
        close()
        if not dialog._book_destroyed:
            dialog.Destroy()


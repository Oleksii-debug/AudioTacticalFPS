"""Explicit retained-document selection over the existing translation service.

Native controls use the frame's existing worker/progress/cancellation path.
This module owns no executor, queue, database, model or provider process.
"""
import json
from pathlib import Path
import re
import stat
from threading import Event
from time import time
from uuid import uuid4

from ..assistant.models import local_path
from ..assistant.profiles import ProfileStore
from ..assistant.providers import Gateway, translation_authorization_type
from ..assistant.translation import TranslationService
from ..assistant.translation_documents import (
    MAX_DOCUMENTS, MAX_CHARACTERS, capture_document, review_documents, prepare_documents,
    run_documents, export_documents, _selections)
from ..speech._io import check_cancelled


def library_identity(root):
    """Bind a native window to the actual validated local directory."""
    path = local_path(root)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Open an existing local library directory.')
    return info.st_dev, info.st_ino


class DocumentTranslationController:
    """One native window's lifetime and exact library binding."""
    def __init__(self, frame):
        if frame.active is None:
            raise ValueError('Open a library before selecting document translations.')
        self.frame, self.library, self.active = frame, frame.library, frame.active
        self.root = Path(self.library.root)
        self.root_identity = library_identity(self.root)
        self.cancel = Event()
        self.alive, self.busy = True, False
        self.guard()
        self.directory = self.root / 'state' / 'translation'
        self.service = TranslationService(self.directory, library=self.library)
        self.profiles = ProfileStore(self.root / 'state' / 'providers')
        self.guard()

    def current(self):
        if (not self.alive or self.frame.library is not self.library
                or Path(self.library.root) != self.root or self.frame.active != self.active
                or getattr(self.frame, '_closing', False)):
            return False
        try:
            return library_identity(self.root) == self.root_identity
        except (OSError, ValueError):
            return False

    def guard(self):
        if not self.current():
            raise InterruptedError('The document window, original library or research topic changed.')

    def stopped(self):
        return (not self.current() or self.cancel.is_set()
                or self.frame.cancel_event.is_set())

    def profile_guard(self, profile):
        self.guard()
        current = self.profiles.get(profile.id)
        if not current.enabled or current.to_dict() != profile.to_dict():
            raise ValueError('The selected translation profile changed; review it again.')

    def start(self, title, operation, accepted, failed, *, accept_committed=False):
        if type(accept_committed) is not bool:
            raise ValueError('Committed-result handling must be an explicit boolean.')
        self.guard()
        if self.busy or getattr(self.frame, 'working', False):
            raise ValueError('Finish or cancel the active operation first.')
        self.cancel.clear()
        self.busy = True
        def work():
            self.guard()
            check_cancelled(self.stopped)
            return operation()
        def done(value):
            self.busy = False
            committed = (accept_committed and type(value) is dict
                and value.get('status') == 'complete'
                and type(value.get('path')) is str and bool(value['path']))
            if self.current() and (committed or not self.stopped()):
                accepted(value)
        def failure(error):
            self.busy = False
            if self.current():
                failed(error)
        try:
            scheduled = self.frame.run_job(title, work, done=done, failed=failure)
        except BaseException:
            self.busy = False
            raise
        if scheduled is False:
            self.busy = False
            return False
        return True

    def close(self):
        self.cancel.set()
        self.alive = False


def selected_range(value):
    if value.strip() == '*':
        return 1, None
    match = re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?', value.strip())
    if not match:
        raise ValueError('Enter * for the entire document or an explicit reading range such as 1-3.')
    first, last = int(match[1]), int(match[2] or match[1])
    if first < 1 or last < first:
        raise ValueError('Choose positive reading positions in their original order.')
    return first, last


def show_translation_documents(frame):
    import wx
    controller = DocumentTranslationController(frame)
    service = controller.service
    profiles = tuple(p for p in controller.profiles.list()
        if p.enabled and p.adapter in ('google-translation-v2', 'google-translation-v3', 'libretranslate-local'))
    dialog = wx.Dialog(frame, title='Translate retained documents and ranges', size=(920, 840),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Document translation workstation')
    layout = wx.BoxSizer(wx.VERTICAL)
    selections, saved_ids, reviewed = [], [], None

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
        return control

    provider = field('Translation profile', wx.Choice,
                     choices=[p.name + '; ' + p.adapter + '; ' + p.id for p in profiles])
    if profiles:
        provider.SetSelection(0)
    source = field('Source language code; auto detects', value='auto')
    target = field('Target language code', value='uk')
    glossary = field('Advanced v3 glossary resource; blank for none', value='')
    budget = field('Total approved batch budget in USD; local service requires 0', value='')
    rate = field('Optional current contract price per million characters', value='')
    listing = field('Explicitly selected retained books or ranges', wx.ListBox, proportion=1)
    status = field('Document translation review and saved job IDs', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='Add each book or range explicitly from the currently visible catalog page. '
              'Up to 64 selections and 2,000,000 source characters. Paragraph order is retained; '
              'original document layout, tables and images are not reconstructed. '
              'Preview and approve the whole selection before saving ordinary translation jobs. '
              'Run/resume uses those existing approvals. No provider request starts during preparation.')
    buttons = {}
    row = wx.WrapSizer(wx.HORIZONTAL)
    for name, label in (
        ('add', '&Add book or range'), ('remove', '&Remove selection'), ('languages', 'Fetch &languages'),
        ('preview', '&Preview document batch'), ('prepare', 'Approve and &save jobs'),
        ('run', '&Run / resume saved jobs'), ('saved', 'Choose saved &document jobs'),
        ('export', '&Export saved documents'), ('layout', 'Translate DOCX with &layout'),
        ('pause', 'Pa&use'), ('close', '&Close')):
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        button.Bind(wx.EVT_BUTTON, lambda event, key=name: dispatch(key))
        buttons[name] = button
        row.Add(button, 0, wx.ALL, 3)
    layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)

    def report(value, focus=True):
        if controller.current():
            status.ChangeValue(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            if focus:
                status.SetFocus()

    def failed(error):
        controls()
        receipt = getattr(error, 'receipt', None)
        report(str(error) + ('\n' + json.dumps(receipt, ensure_ascii=False, indent=2) if receipt else ''))

    def controls():
        if not controller.current():
            return
        busy = controller.busy
        for control in (provider, source, target, glossary, budget, rate, listing):
            control.Enable(not busy)
        for key, button in buttons.items():
            button.Enable(key == 'close' or (busy if key == 'pause' else not busy))
        buttons['prepare'].Enable(not busy and bool(selections))
        buttons['run'].Enable(not busy and bool(saved_ids))
        buttons['export'].Enable(not busy and bool(saved_ids))

    def launch(title, operation, accepted):
        def done(value):
            try:
                accepted(value)
            except Exception as error:
                failed(error)
            controls()
        controller.start(title, operation, done, failed)
        controls()

    def account():
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Configure and enable a translation profile in AI and API connections first.')
        return profiles[index]

    def values():
        return (account(), tuple(selections), source.GetValue().strip(),
                target.GetValue().strip(), budget.GetValue(),
                rate.GetValue().strip() or None, glossary.GetValue().strip() or None)

    def refresh_selected():
        listing.Clear()
        for item in selections:
            positions = item.document.source_anchor['positions']
            listing.Append(item.document.title + '; positions ' + str(positions[0]['ordinal'])
                + '-' + str(positions[-1]['ordinal']) + '; ' + str(len(item.document.source_text)) + ' characters')

    def add():
        nonlocal reviewed
        controller.guard()
        if len(selections) >= MAX_DOCUMENTS:
            raise ValueError('The selected batch already has 64 documents or ranges.')
        panel = frame.book.GetPage(1) if frame.book is not None else None
        if panel is None or not panel.ready or panel.workspace != controller.active[0]:
            raise ValueError('Refresh Books and Collections before opening the document workstation.')
        rows = tuple(panel.rows.values())
        if not rows:
            raise ValueError('No editions are visible in the current catalog page.')
        chooser = wx.SingleChoiceDialog(dialog, 'Explicitly choose one visible retained edition.',
            'Add one book', [(row.title or 'Untitled') + '; ' + row.edition_id for row in rows])
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            index = chooser.GetSelection()
            if not 0 <= index < len(rows):
                return
            selected = rows[index]
        finally:
            chooser.Destroy()
        controller.guard()
        if panel.rows.get(selected.edition_id) != selected:
            raise ValueError('The displayed catalog selection changed; choose again.')
        chooser = wx.TextEntryDialog(dialog,
            '* selects the full document. Otherwise enter reading positions such as 1-3. '
            'These are source reading positions, not printed page or inferred chapter labels.',
            'Choose retained source range', '*')
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            first, last = selected_range(chooser.GetValue())
        finally:
            chooser.Destroy()
        controller.guard()
        snapshot = tuple(selections)
        def capture():
            return capture_document(controller.library, controller.active[0], selected.edition_id,
                first=first, last=last, topic=controller.active[1], cancelled=controller.stopped,
                guard=controller.guard,
                max_characters=MAX_CHARACTERS - sum(len(row.document.source_text) for row in snapshot))
        def captured(item):
            nonlocal reviewed
            controller.guard()
            _selections((*snapshot, item))
            selections.append(item)
            reviewed = None
            refresh_selected()
            report('Added only the explicitly selected source. Preview the complete selection before approval.')
        launch('Reading explicitly selected translation source', capture, captured)

    def remove():
        nonlocal reviewed
        index = listing.GetSelection()
        if 0 <= index < len(selections):
            del selections[index]
            reviewed = None
            refresh_selected()

    def languages():
        p = account()
        if wx.MessageBox('Contact this exact ' + ('local service' if p.local else 'Google account')
            + ' for its declared languages? No document text is sent.\n' + p.endpoint,
            'Approve language discovery', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        controller.guard()
        identity = uuid4().hex
        approval = translation_authorization_type(p)(identity, identity, ((p.id, p.endpoint),),
            ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
        def discover():
            controller.profile_guard(p)
            service.gateway = Gateway(service.directory / 'provider',
                mode='LOCAL_ONLY' if p.local else 'APPROVED_REMOTE')
            def stop():
                if controller.stopped():
                    return True
                controller.profile_guard(p)
                return False
            return service.refresh_languages(p, authorization=approval, cancelled=stop)
        launch('Fetching declared translation languages', discover,
               lambda rows: report({'languages': rows, 'quality_or_model_identity_qualification': 'NOT_RUN'}))

    def preview():
        p, selected, first, last, bound, price, terminology = values()
        def review():
            controller.profile_guard(p)
            return review_documents(service, p, selected, first, last, bound,
                glossary=terminology, rate_per_million=price, cancelled=controller.stopped,
                guard=lambda: controller.profile_guard(p))
        def accepted(value):
            nonlocal reviewed
            reviewed = value
            report(value)
        launch('Reviewing explicit document translation batch', review, accepted)

    def prepare():
        if reviewed is None:
            raise ValueError('Preview this exact document selection and budget first.')
        p, selected, first, last, bound, price, terminology = values()
        approved = reviewed['review_sha256']
        if wx.MessageBox('Save translation jobs for this exact reviewed selection and total budget?\n'
            + json.dumps(reviewed, ensure_ascii=False, indent=2)
            + '\nNo provider call starts now. Run/resume will use these exact saved approvals.',
            'Approve selected documents', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        controller.guard()
        def work():
            return prepare_documents(service, p, selected, first, last, bound,
                approved_review=approved, glossary=terminology, rate_per_million=price,
                cancelled=controller.stopped, guard=lambda: controller.profile_guard(p))
        def accepted(value):
            saved_ids[:] = value['jobs']
            report(value)
        launch('Saving reviewed document translation jobs', work, accepted)

    def choose_saved():
        def accepted(rows):
            controller.guard()
            if not rows:
                report('No retained-source translation jobs are saved.')
                return
            chooser = wx.MultiChoiceDialog(dialog, 'Select only the saved source jobs to run or export.',
                'Saved document translation jobs',
                [row['title'] + '; ' + row['status'] + '; ' + row['id'] for row in rows])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                indexes = list(chooser.GetSelections())
                if not indexes or len(indexes) > MAX_DOCUMENTS or any(i < 0 or i >= len(rows) for i in indexes):
                    raise ValueError('Choose between 1 and 64 distinct saved jobs.')
                controller.guard()
                saved_ids[:] = [rows[i]['id'] for i in indexes]
                report({'selected_jobs': list(saved_ids), 'provider_calls_started': False})
            finally:
                chooser.Destroy()
        launch('Reading saved document translation jobs',
            lambda: tuple(row for row in service.list() if row.get('kind') == 'translation/2'), accepted)

    def run():
        identities = tuple(saved_ids)
        def progress(done, total, phase):
            from ..progress import current_progress
            tracker = current_progress()
            if tracker is not None:
                tracker.update(completed=done, total=total, phase=phase)
        launch('Translating saved document jobs',
            lambda: run_documents(service, identities, cancelled=controller.stopped,
                guard=controller.guard, progress=progress), report)

    def export():
        identities = tuple(saved_ids)
        chooser = wx.DirDialog(dialog, 'Choose an existing parent for NEW document translation exports')
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            parent = Path(chooser.GetPath())
        finally:
            chooser.Destroy()
        controller.guard()
        launch('Exporting verified document translation derivatives',
            lambda: export_documents(service, identities, parent,
                cancelled=controller.stopped, guard=controller.guard), report)

    def formatted():
        from .translation_layout import show_translation_layout
        controller.guard()
        show_translation_layout(frame)
        controller.guard()

    def close():
        controller.close()
        dialog.EndModal(wx.ID_CLOSE)

    def dispatch(key):
        try:
            controller.guard()
            if key != 'close' and controller.busy and key != 'pause':
                raise ValueError('Finish or pause the active document operation first.')
            {'add': add, 'remove': remove, 'languages': languages, 'preview': preview,
             'prepare': prepare, 'run': run, 'saved': choose_saved, 'export': export, 'layout': formatted,
             'pause': controller.cancel.set, 'close': close}[key]()
            controls()
        except Exception as error:
            failed(error)

    dialog.Bind(wx.EVT_CLOSE, lambda event: close())
    controls()
    try:
        dialog.ShowModal()
    finally:
        controller.close()
        dialog.Destroy()

"""Native DOCX layout workflow over the existing document controller and jobs.

No executor, provider, source store, queue or automatic whole-catalog selection
is introduced here. Completed paragraph jobs may export without credentials.
"""
import json
from pathlib import Path
from time import time
from uuid import uuid4

from ..assistant.providers import Gateway, translation_authorization_type
from ..assistant.translation_documents import run_documents
from ..assistant.translation_layout import (
    capture_layout, review_layout, prepare_layout, export_layout, order_layout_jobs)
from ..assistant.translation_layout_worker import NOTICE
from ..research.export_file import capture_export_destination
from .translation_documents import DocumentTranslationController


def show_translation_layout(frame, *, document_format='docx'):
    if document_format not in ('docx', 'epub'):
        raise ValueError('Choose a supported retained document format.')
    import wx
    capture, review_source, prepare_source, export_source, order_jobs = (
        capture_layout, review_layout, prepare_layout, export_layout, order_layout_jobs)
    notice = NOTICE
    if document_format == 'epub':
        from ..assistant import translation_epub_layout as epub
        capture, review_source, prepare_source, export_source, order_jobs = (
            epub.capture_layout, epub.review_layout, epub.prepare_layout, epub.export_layout,
            epub.order_layout_jobs)
        notice = epub.NOTICE
    def label_for_format(value):
        if document_format == 'docx':
            return value
        return value.replace('larger DOCX', 'complete EPUB').replace('DOCX', 'EPUB').replace(
            '.docx', '.epub').replace('Word document', 'EPUB publication')
    controller = DocumentTranslationController(frame)
    service = controller.service
    profiles = tuple(p for p in controller.profiles.list() if p.enabled and
        p.adapter in ('google-translation-v2', 'google-translation-v3', 'libretranslate-local'))
    dialog = wx.Dialog(frame, title=label_for_format('Translate retained DOCX with layout'), size=(920, 820),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName(label_for_format('Formatted DOCX translation workstation'))
    panel = wx.BoxSizer(wx.VERTICAL)
    captured, reviewed, saved_ids = None, None, []

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        label = label_for_format(label)
        if type(kwargs.get('value')) is str:
            kwargs['value'] = label_for_format(kwargs['value'])
        panel.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label)
        panel.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
        return control

    provider = field('DOCX translation profile', wx.Choice,
        choices=[p.name + '; ' + p.adapter + '; ' + p.id for p in profiles])
    if profiles:
        provider.SetSelection(0)
    source = field('DOCX source language; auto detects', value='auto')
    target = field('DOCX target language', value='uk')
    glossary = field('DOCX Advanced glossary; blank for none', value='')
    budget = field('DOCX total approved USD budget; local service requires 0', value='')
    rate = field('DOCX optional current contract price per million characters', value='')
    source_limits = (
        'Up to 2,048 nonempty text fragments and 2,000,000 source characters. '
        'Eligible DOCX notes and embedded pictures use a fixed-marker source profile; '
        'the actual inspected source and its limitations are shown before approval.'
        if document_format == 'docx' else
        'Up to 2,048 nonempty canonical spine blocks and 2,000,000 source characters. '
        'The actual inspected XHTML profile and its limitations are shown before approval.')
    selected = field('Selected retained DOCX and paragraph jobs', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY,
        value='Explicitly choose one retained DOCX from the current catalog page. ' + source_limits)
    status = field('Formatted translation status and limitations', proportion=1,
        style=wx.TE_MULTILINE | wx.TE_READONLY, value=notice)
    buttons, row = {}, wx.WrapSizer(wx.HORIZONTAL)
    for key, label in (
        ('choose', '&Choose retained DOCX'), ('languages', 'Fetch DOCX &languages'),
        ('preview', '&Preview DOCX translation'), ('prepare', 'Approve and &save DOCX jobs'),
        ('saved', 'Choose saved &paragraph jobs'), ('run', '&Run / resume DOCX jobs'),
        ('export', '&Export new translated DOCX'), ('stop', '&Stop after current request'),
        ('close', '&Close formatted translation')):
        label = label_for_format(label)
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        button.Bind(wx.EVT_BUTTON, lambda event, action=key: dispatch(action))
        row.Add(button, 0, wx.ALL, 3)
        buttons[key] = button
    panel.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(panel)

    def report(value):
        if controller.current():
            status.ChangeValue(value if isinstance(value, str)
                               else json.dumps(value, ensure_ascii=False, indent=2))
            status.SetFocus()

    def controls():
        if not controller.current():
            return
        busy = controller.busy
        for control in (provider, source, target, glossary, budget, rate):
            control.Enable(not busy)
        for key, button in buttons.items():
            button.Enable(key == 'close' or (busy if key == 'stop' else not busy))
        buttons['preview'].Enable(not busy and captured is not None and bool(profiles))
        buttons['prepare'].Enable(not busy and reviewed is not None and bool(profiles))
        buttons['saved'].Enable(not busy and captured is not None)
        buttons['run'].Enable(not busy and captured is not None and bool(saved_ids))
        buttons['export'].Enable(not busy and captured is not None and bool(saved_ids))
        buttons['languages'].Enable(not busy and bool(profiles))

    def refresh_selection():
        if controller.current():
            value = captured.summary() if captured is not None else {'selected_source': None}
            selected.ChangeValue(json.dumps({**value, 'explicit_saved_jobs': list(saved_ids)},
                                            ensure_ascii=False, indent=2))

    def failed(error):
        if not controller.current():
            return
        receipt = getattr(error, 'receipt', None)
        report(str(error) + ('\n' + json.dumps(receipt, ensure_ascii=False, indent=2) if receipt else ''))
        controls()

    def launch(title, operation, accepted, *, committed=False):
        def done(value):
            if not controller.current():
                return
            try:
                accepted(value)
            except Exception as error:
                failed(error)
            controls()
        controller.start(label_for_format(title), operation, done, failed, accept_committed=committed)
        controls()

    def account():
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Configure and explicitly enable a translation profile first. '
                             'Completed saved jobs can still be exported offline.')
        return profiles[index]

    def values():
        if captured is None:
            raise ValueError(label_for_format('Choose the retained DOCX source first.'))
        return (account(), captured, source.GetValue().strip(), target.GetValue().strip(),
                budget.GetValue(), glossary.GetValue().strip() or None,
                rate.GetValue().strip() or None)

    def choose():
        controller.guard()
        catalog = frame.book.GetPage(1) if frame.book is not None else None
        if catalog is None or not catalog.ready or catalog.workspace != controller.active[0]:
            raise ValueError(label_for_format('Refresh Books and Collections before selecting the retained DOCX.'))
        rows = tuple(catalog.rows.values())
        if not rows:
            raise ValueError('No retained editions are visible on the current catalog page.')
        chooser = wx.SingleChoiceDialog(dialog, label_for_format('Choose one retained DOCX. Unsupported rich constructs '
            'are rejected before any document text is sent.'), label_for_format('Choose retained DOCX'),
            [(item.title or 'Untitled') + '; ' + item.edition_id for item in rows])
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            index = chooser.GetSelection()
            if not 0 <= index < len(rows):
                return
            item = rows[index]
        finally:
            chooser.Destroy()
        controller.guard()
        if catalog.rows.get(item.edition_id) != item:
            raise ValueError('The displayed catalog selection changed; select it again.')
        def work():
            return capture(controller.library, controller.active[0], item.edition_id,
                topic=controller.active[1], cancelled=controller.stopped, guard=controller.guard)
        def accepted(value):
            nonlocal captured, reviewed
            captured, reviewed = value, None
            saved_ids.clear()
            refresh_selection()
            report(value.summary())
        launch('Inspecting explicitly selected retained DOCX', work, accepted)

    def languages():
        p = account()
        if wx.MessageBox('Contact this exact ' + ('local service' if p.local else 'Google account')
            + label_for_format(' for its declared languages? No DOCX text or document is sent.\n') + p.endpoint,
            label_for_format('Approve DOCX language discovery'), wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        controller.profile_guard(p)
        identity = uuid4().hex
        authorization = translation_authorization_type(p)(identity, identity, ((p.id, p.endpoint),),
            ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
        def work():
            controller.profile_guard(p)
            service.gateway = Gateway(service.directory / 'provider',
                mode='LOCAL_ONLY' if p.local else 'APPROVED_REMOTE')
            def stopped():
                if controller.stopped():
                    return True
                controller.profile_guard(p)
                return False
            return service.refresh_languages(p, authorization=authorization, cancelled=stopped)
        launch('Fetching declared DOCX translation languages', work,
            lambda value: report({'languages': value, 'translation_quality': 'NOT_RUN'}))

    def preview():
        p, document, first, last, bound, terminology, price = values()
        def accepted(value):
            nonlocal reviewed
            reviewed = value
            report(value)
        launch('Reviewing retained DOCX paragraphs, limitations and budget',
            lambda: review_source(service, p, document, first, last, bound,
                glossary=terminology, rate_per_million=price, cancelled=controller.stopped,
                guard=lambda: controller.profile_guard(p)), accepted)

    def prepare():
        if reviewed is None:
            raise ValueError(label_for_format('Preview this exact DOCX, settings and budget before saving jobs.'))
        p, document, first, last, bound, terminology, price = values()
        approved = reviewed['review_sha256']
        if wx.MessageBox(label_for_format('Save the exact reviewed DOCX paragraph jobs and approved budget?\n')
            + json.dumps(reviewed, ensure_ascii=False, indent=2)
            + '\nOnly source paragraph text is eligible for the selected translator. '
              'Run/resume is a separate explicit action. Original files remain unchanged.',
            label_for_format('Approve formatted DOCX translation'), wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        controller.profile_guard(p)
        def accepted(value):
            saved_ids[:] = value['jobs']
            refresh_selection()
            report(value)
        launch('Saving approved DOCX paragraph jobs',
            lambda: prepare_source(service, p, document, first, last, bound,
                approved_review=approved, glossary=terminology, rate_per_million=price,
                cancelled=controller.stopped, guard=lambda: controller.profile_guard(p)), accepted)

    def choose_saved():
        document = captured
        if document is None:
            raise ValueError(label_for_format('Reopen the retained DOCX before selecting its saved paragraph jobs.'))
        def work():
            controller.guard()
            candidates = []
            for job in service.list():
                if controller.stopped():
                    raise InterruptedError('Saved paragraph selection stopped.')
                binding = service._binding(job)
                if binding is None or binding['topic'] != document.topic:
                    continue
                anchor = binding['anchor']
                if (anchor['workspace'] == document.workspace and anchor['edition'] == document.edition
                        and anchor['revision'] == document.revision
                        and anchor['asset_sha256'] == document.asset_sha256
                        and ((document.source_partition is None and len(anchor['positions']) == 1)
                             or (document.source_partition is not None
                                 and job.get('source_partition') == document.source_partition))):
                    candidates.append(job)
            return tuple(candidates)
        def accepted(rows):
            controller.guard()
            if not rows:
                report(label_for_format('No matching source-bound paragraph jobs are saved for this exact DOCX revision.'))
                return
            chooser = wx.MultiChoiceDialog(dialog,
                (label_for_format('Select the one complete saved partition job for this larger DOCX.')
                 if document.source_partition is not None else
                 'Explicitly select one saved job per paragraph with the same profile and language settings.'),
                label_for_format('Saved DOCX paragraph jobs'),
                ['Position ' + str(item['source_binding']['anchor']['positions'][0]['ordinal'])
                 + '; ' + item['status'] + '; ' + item['source_language'] + ' → '
                 + item['target_language'] + '; ' + item['profile']['name'] + '; ' + item['id']
                 for item in rows])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                indexes = tuple(chooser.GetSelections())
                if not indexes or any(type(index) is not int or not 0 <= index < len(rows)
                                      for index in indexes):
                    raise ValueError('Explicitly select the saved paragraph jobs.')
                identities = tuple(rows[index]['id'] for index in indexes)
            finally:
                chooser.Destroy()
            controller.guard()
            def selected_jobs(value):
                saved_ids[:] = value
                refresh_selection()
                report({'selected_jobs': list(value), 'provider_calls_started': False})
            launch('Checking saved DOCX paragraph source identities',
                lambda: order_jobs(service, document, identities,
                    cancelled=controller.stopped, guard=controller.guard), selected_jobs)
        launch('Reading saved DOCX paragraph jobs', work, accepted)

    def run():
        document, identities = captured, tuple(saved_ids)
        def progress(done, total, phase):
            from ..progress import current_progress
            tracker = current_progress()
            if tracker is not None:
                tracker.update(completed=done, total=total, phase=phase)
        def work():
            ordered = order_jobs(service, document, identities,
                                        cancelled=controller.stopped, guard=controller.guard)
            return run_documents(service, ordered, cancelled=controller.stopped,
                                 guard=controller.guard, progress=progress)
        launch('Translating explicitly saved DOCX paragraph jobs', work, report)

    def export():
        document, identities = captured, tuple(saved_ids)
        if document is None or not identities:
            raise ValueError('Choose the retained source and its completed saved paragraph jobs.')
        chooser = wx.FileDialog(dialog, label_for_format('Export a NEW translated DOCX'),
            wildcard=label_for_format('Word document (*.docx)|*.docx'), style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            destination = Path(chooser.GetPath())
        finally:
            chooser.Destroy()
        controller.guard()
        approval = capture_export_destination(destination, private_root=controller.root,
                                               cancelled=lambda: controller.guard() or False)
        if approval.state is not None:
            raise FileExistsError(label_for_format('Choose a new DOCX filename; existing files are not replaced.'))
        def work():
            ordered = order_jobs(service, document, identities,
                                        cancelled=controller.stopped, guard=controller.guard)
            return export_source(service, document, ordered, destination, approval=approval,
                                 cancelled=controller.stopped, guard=controller.guard)
        launch('Exporting a new verified translated DOCX', work, report, committed=True)

    def close():
        controller.close()
        dialog.EndModal(wx.ID_CLOSE)

    def destroyed(event):
        if event.GetEventObject() is dialog:
            controller.close()
        event.Skip()

    def dispatch(action):
        try:
            controller.guard()
            if controller.busy and action not in ('stop', 'close'):
                raise ValueError('Finish or stop the active formatted translation operation first.')
            {'choose': choose, 'languages': languages, 'preview': preview,
             'prepare': prepare, 'saved': choose_saved, 'run': run, 'export': export,
             'stop': controller.cancel.set, 'close': close}[action]()
            controls()
        except Exception as error:
            failed(error)

    dialog.Bind(wx.EVT_CLOSE, lambda event: close())
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    controls()
    try:
        dialog.ShowModal()
    finally:
        controller.close()
        dialog.Destroy()


def show_epub_translation(frame):
    return show_translation_layout(frame, document_format='epub')

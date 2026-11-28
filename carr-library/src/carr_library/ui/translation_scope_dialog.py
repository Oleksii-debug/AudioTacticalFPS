"""Accessible native document, EPUB chapter and batch translation workspace."""
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

from ..assistant.models import canonical
from ..providers import LOCAL_TRANSLATION_PROVIDER
from ..assistant.translation import language_approval
from ..assistant.translation_selection import DocumentSelection, capture_selection
from ..assistant.translation_scope import TranslationScopeService
from ..assistant.translation_scope_state import normalized_rate, scope_approval
from ..reader.ebook_contents import open_ebook
from ..research.topic_tree import TopicTree
from .api_connections import parse_budget


class TranslationScopeController:
    def __init__(self, library, workspace, topic=None, *, service=None):
        self.library, self.workspace, self.topic = library, workspace, topic
        self.service = service or TranslationScopeService(library)
        if self.service.root != Path(library.root).resolve(strict=True):
            raise ValueError('Scoped translation belongs to the current local library.')
        self.reviewed = None

    def key(self, profile, selections, mode, source, target, budget, glossary, rate):
        return canonical(dict(profile=profile.fingerprint, selections=[asdict(item) for item in selections],
            mode=mode, source=source, target=target, budget_micros=parse_budget(budget),
            glossary=glossary, rate=normalized_rate(rate), workspace=self.workspace, topic=self.topic))

    def preview(self, profile, selections, mode, source, target, budget, *, glossary='', rate='',
                cancelled=lambda: False):
        self.reviewed = None
        key = self.key(profile, selections, mode, source, target, budget, glossary, rate)
        selection = capture_selection(self.library, self.workspace, selections, mode=mode,
                                      topic=self.topic, cancelled=cancelled)
        result = self.service.preview(profile, selection, source, target, parse_budget(budget),
                                      glossary=glossary, rate=rate, cancelled=cancelled)
        self.reviewed = (key, json.loads(canonical(selection)), json.loads(canonical(result)))
        return dict(preview=result, selection=selection)

    def submit(self, profile, selections, mode, source, target, budget, *, approved=False,
               glossary='', rate='', cancelled=lambda: False):
        key = self.key(profile, selections, mode, source, target, budget, glossary, rate)
        if approved is not True or self.reviewed is None or self.reviewed[0] != key:
            raise PermissionError('Preview and explicitly approve this exact document selection and per-item transfer.')
        _, selection, preview = self.reviewed
        permit = scope_approval(profile, selection, source, target, parse_budget(budget),
            language_catalog_sha256=preview['language_catalog_sha256'], glossary=glossary, rate=rate)
        identity = self.service.submit(profile, selection, source, target, parse_budget(budget),
            approval=permit, glossary=glossary, rate=rate, cancelled=cancelled)
        self.reviewed = None
        return identity


def show_translation_scope(frame, *, service=None):
    import wx
    if frame.active is None or getattr(frame, '_closing', False):
        raise ValueError('Open a library before selecting documents for translation.')
    library, scope = frame.library, tuple(frame.active)
    workspace, topic = scope
    controller = TranslationScopeController(library, workspace, topic, service=service)
    scoped, translation = controller.service, controller.service.service
    alive, busy, current_job = True, False, None
    cancel = Event()
    profiles, source_codes, target_codes, rows, chapters = [], [], [], [], []
    chosen = {}
    after, current_filter, chapter_edition = None, '', None
    dialog = wx.Dialog(frame, title='Translation — documents, chapters and batches', size=(980, 940),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Structured translation workspace')
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 3)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 3)
        return control

    mode = field('Translation &scope', wx.Choice, choices=['Whole document', 'EPUB chapter', 'Document batch'])
    mode.SetSelection(0)
    title_filter = field('Document title &filter; exact literal text', value='')
    documents = field('Documents to translate; Space checks each &item', wx.CheckListBox, proportion=1, choices=[])
    selection_view = field('Selected documents in transfer order', style=wx.TE_MULTILINE | wx.TE_READONLY)
    chapter = field('Exact EPUB &chapter; load contents after choosing one book', wx.Choice, choices=[])
    provider = field('Translation &connection; Google or local', wx.Choice, choices=[])
    source = field('&Source language', wx.Choice, choices=[])
    target = field('Target &language', wx.Choice, choices=[])
    glossary = field('Advanced v3 &glossary resource; blank for none', value='')
    glossary.SetMaxLength(512)
    rate = field('Optional owner contract price per million characters in USD', value='')
    budget = field('Total approved reservation in USD; preview allocates each item', value='')
    selected_text = field('Exact selected source and original headings', proportion=1,
                          style=wx.TE_MULTILINE | wx.TE_READONLY)
    status = field('Per-item preview, approval and receipt status', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY)
    buttons = {}
    for entries in (
            [('books', '&Load books'), ('next', 'Next &books'), ('clear', 'Clear selection'),
             ('chapters', 'Load &chapters'), ('connections', 'AI/API connections'), ('languages', 'Fetch languages')],
            [('preview', '&Preview transfers'), ('run', 'T&ranslate / resume'), ('pause', 'Pa&use'),
             ('saved', 'Saved scoped &jobs'), ('charges', 'Charge history'), ('export', '&Export bilingual'),
             ('new', '&New selection'), ('close', 'Cl&ose')]):
        line = wx.BoxSizer(wx.HORIZONTAL)
        for name, label in entries:
            button = wx.Button(dialog, id=wx.ID_CLOSE if name == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, lambda event: None)
            buttons[name] = button
            line.Add(button, 0, wx.ALL, 2)
        layout.Add(line, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CLOSE)

    def guard():
        if frame.library is not library or tuple(frame.active or ()) != scope or getattr(frame, '_closing', False):
            raise ValueError('The active library or topic changed. Reopen scoped translation.')
        with library.read(workspace) as db:
            if topic is not None:
                TopicTree(db, workspace)._visible(topic)

    def stopped():
        return (cancel.is_set() or frame.cancel_event.is_set() or frame.library is not library
                or tuple(frame.active or ()) != scope or getattr(frame, '_closing', False))

    def report(value):
        if alive:
            status.ChangeValue(str(value))

    def controls():
        if not alive:
            return
        editable = not busy and current_job is None
        for control in (mode, title_filter, documents, chapter, provider, source, target, rate, budget):
            control.Enable(editable)
        index = provider.GetSelection()
        glossary.Enable(editable and 0 <= index < len(profiles) and profiles[index].provider == 'google-translation-v3')
        if 0 <= index < len(profiles) and profiles[index].provider == LOCAL_TRANSLATION_PROVIDER:
            rate.Enable(False)
            budget.Enable(False)
        for name in ('books', 'next', 'clear', 'chapters', 'connections', 'languages', 'preview'):
            buttons[name].Enable(editable)
        buttons['chapters'].Enable(editable and mode.GetSelection() == 1)
        for name in ('run', 'saved', 'new'):
            buttons[name].Enable(not busy)
        for name in ('charges', 'export'):
            buttons[name].Enable(not busy and current_job is not None)
        buttons['pause'].Enable(busy)

    def begin(label, operation, complete):
        nonlocal busy
        if busy or not alive:
            return False
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            report('Finish or cancel the active operation first.')
            return False
        guard()
        busy = True
        cancel.clear()
        controls()
        def done(result):
            nonlocal busy
            if not alive:
                return
            busy = False
            controls()
            try:
                guard()
                complete(result)
            except Exception as exc:
                report(str(exc))
        def failed(error):
            nonlocal busy
            if not alive:
                return
            busy = False
            controls()
            report(str(error))
        started = frame.run_job(label, operation, done=done, failed=failed)
        if started is False or (not getattr(frame, 'working', False) and busy):
            busy = False
            controls()
            return False
        return True

    def profile():
        guard()
        index = provider.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Choose an explicit Google or local translation connection.')
        return profiles[index]

    def languages_loaded(event=None):
        nonlocal source_codes, target_codes
        try:
            inventory = translation.languages(profile()) if profiles else ()
        except Exception as exc:
            inventory = ()
            report(str(exc))
        sources = [row for row in inventory if row.get('source', bool(row.get('targets', True)))]
        local_targets = ({target for row in inventory for target in row['targets']}
                         if inventory and all('targets' in row for row in inventory) else None)
        targets = [row for row in inventory if row.get('target', True)
                   and (local_targets is None or row['language'] in local_targets)]
        source_codes = ['auto'] + [row['language'] for row in sources]
        target_codes = [row['language'] for row in targets]
        source.SetItems(['auto — detect'] + [row['language'] + ' — ' + row['name'] for row in sources])
        source.SetSelection(0)
        target.SetItems([row['language'] + ' — ' + row['name'] for row in targets])
        if target_codes:
            target.SetSelection(target_codes.index('uk') if 'uk' in target_codes else 0)
        selected_index = provider.GetSelection()
        if not 0 <= selected_index < len(profiles) or profiles[selected_index].provider != 'google-translation-v3':
            glossary.ChangeValue('')
        if 0 <= selected_index < len(profiles) and profiles[selected_index].provider == LOCAL_TRANSLATION_PROVIDER:
            budget.ChangeValue('0')
            rate.ChangeValue('')
        controller.reviewed = None
        controls()

    def load_profiles():
        nonlocal profiles
        profiles = list(translation.available_profiles())
        provider.SetItems([item.name + ' — ' + item.provider for item in profiles])
        if profiles:
            provider.SetSelection(0)
        languages_loaded()

    def selected(event=None):
        before = tuple(chosen)
        for index, item in enumerate(rows):
            if documents.IsChecked(index):
                chosen[item.edition_id] = item
            else:
                chosen.pop(item.edition_id, None)
        if len(chosen) > 100:
            raise ValueError('A translation batch contains at most 100 explicitly selected documents.')
        selection_view.ChangeValue('\n'.join(str(i + 1) + '. ' + (item.title or 'Untitled') +
                                    ' — ' + item.edition_id for i, item in enumerate(chosen.values())))
        if tuple(chosen) != before:
            controller.reviewed = None

    def load_books(next_page=False):
        nonlocal after, current_filter
        try:
            selected()
            literal = title_filter.GetValue()
            cursor = after if next_page and literal == current_filter else None
            def loaded(found):
                nonlocal rows, after, current_filter
                rows = list(found[:100])
                after = rows[-1].edition_id if len(found) > 100 else None
                current_filter = literal
                documents.SetItems([(item.title or 'Untitled') + ' — ' + item.edition_id for item in rows])
                for index, item in enumerate(rows):
                    documents.Check(index, item.edition_id in chosen)
                report(str(len(rows)) + ' books on this page. ' +
                       ('More books are available with Next books.' if after else 'End of this filtered catalog.') +
                       ' Checked documents remain selected across pages.')
            if next_page and after is None:
                report('No next catalog page is available. Load books to restart this filter.')
                return
            begin('Loading explicit translation document choices',
                  lambda: library.browse(workspace, title=literal or None, after=cursor, limit=101), loaded)
        except Exception as exc:
            report(str(exc))

    def load_chapters(event=None):
        try:
            selected()
            if len(chosen) != 1:
                raise ValueError('Check exactly one EPUB book before loading chapters.')
            edition = next(iter(chosen))
            def loaded(book):
                nonlocal chapters, chapter_edition
                chapter_edition = book['edition_id']
                chapters = list(book['chapters'])
                chapter.SetItems([str(item['ordinal']) + '. ' + item['title'] for item in chapters])
                if chapters:
                    chapter.SetSelection(0)
                controller.reviewed = None
                report('Choose one declared EPUB chapter. No page range is inferred to be a chapter.')
            begin('Loading sealed EPUB chapter headings',
                  lambda: open_ebook(library, workspace, edition, authorized_topic=topic, cancelled=stopped), loaded)
        except Exception as exc:
            report(str(exc))

    def values():
        selected()
        first, last = source.GetSelection(), target.GetSelection()
        if not 0 <= first < len(source_codes) or not 0 <= last < len(target_codes):
            raise ValueError('Fetch and choose a supported source/target pair.')
        scope_mode = ('document', 'chapter', 'batch')[mode.GetSelection()]
        if not chosen or (scope_mode != 'batch' and len(chosen) != 1):
            raise ValueError('Check exactly one document/chapter, or an explicit batch of at most 100 documents.')
        chapter_number = None
        if scope_mode == 'chapter':
            index = chapter.GetSelection()
            if not 0 <= index < len(chapters) or chapter_edition != next(iter(chosen)):
                raise ValueError('Load and choose an exact EPUB chapter for this selected book.')
            chapter_number = chapters[index]['ordinal']
        selections = tuple(DocumentSelection(identity, chapter=chapter_number) for identity in chosen)
        return (profile(), selections, scope_mode, source_codes[first], target_codes[last], budget.GetValue().strip())

    def snapshot_text(selection):
        return '\n\n'.join(item['title'] + '\nSource revision: ' + item['revision'] + '\n' +
            '\n\n'.join(section['heading'] + '\n' + section['source_text'] for section in item['sections'])
            for item in selection['items'])

    def preview(event=None):
        try:
            args = values()
            options = dict(glossary=glossary.GetValue().strip(), rate=rate.GetValue().strip())
            def complete(result):
                selected_text.ChangeValue(snapshot_text(result['selection']))
                report(json.dumps(result['preview'], ensure_ascii=False, indent=2) +
                       '\nNo source text sent. Translate requires separate approval of every listed item.')
            begin('Preparing exact structured translation transfers',
                  lambda: controller.preview(*args, **options, cancelled=stopped), complete)
        except Exception as exc:
            controller.reviewed = None
            report(str(exc))

    def run(event=None):
        nonlocal current_job
        try:
            if current_job is None:
                # values() synchronizes checkboxes; preserve a review only if its exact key still matches.
                reviewed = controller.reviewed
                args = values()
                controller.reviewed = reviewed
                options = dict(glossary=glossary.GetValue().strip(), rate=rate.GetValue().strip())
                key = controller.key(*args, options['glossary'], options['rate'])
                if reviewed is None or reviewed[0] != key:
                    raise ValueError('Preview the current exact documents, account, languages and item budgets first.')
                result = reviewed[2]
                items = '\n'.join(str(row['index'] + 1) + '. ' + row['title'] +
                    ': ' + str(row['characters']) + ' characters; USD ' +
                    str(Decimal(row['budget_micros']) / 1000000) + ' reservation'
                    for row in result['items'])
                if wx.MessageBox('Account: ' + args[0].name + '\nEndpoint: ' + args[0].endpoint +
                    '\nModel: ' + args[0].model + '\nGlossary: ' + (options['glossary'] or 'none') +
                    '\nLanguage pair: ' + args[3] + ' → ' + args[4] + '\n' + items
                    + ('\nSend the reviewed source blocks to this local endpoint with zero remote reservation? '
                       if args[0].local else
                       '\nSend exactly the reviewed extracted source blocks? The provider may charge this account. ')
                    + 'Originals remain unchanged.',
                    'Approve exact structured source transfer', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    dialog) != wx.YES:
                    return
                checked = values()
                controller.reviewed = reviewed
                if controller.key(*checked, glossary.GetValue().strip(), rate.GetValue().strip()) != key:
                    controller.reviewed = None
                    raise ValueError('The selection changed during approval. Preview it again.')
                def create_and_run():
                    identity = controller.submit(*args, approved=True, **options, cancelled=stopped)
                    return identity, scoped.resume(identity, cancelled=stopped, progress=advanced)
                def completed(value):
                    nonlocal current_job
                    current_job, result = value
                    controls()
                    report(json.dumps(result, ensure_ascii=False, indent=2))
                begin('Translating explicitly approved source structure', create_and_run, completed)
            else:
                identity = current_job
                begin('Resuming approved structured translation',
                      lambda: scoped.resume(identity, cancelled=stopped, progress=advanced),
                      lambda result: report(json.dumps(result, ensure_ascii=False, indent=2)))
        except Exception as exc:
            report(str(exc))

    def advanced(done, total, phase):
        operation = getattr(frame, 'operation_progress', None)
        if operation is not None:
            operation.update(done, total, phase, token=operation.event.token)
        wx.CallAfter(report, str(done) + '/' + str(total) + ': ' + phase)

    def saved(event=None):
        nonlocal current_job
        try:
            plans = scoped.list()
            if not plans:
                report('No saved scoped translation plans.')
                return
            chooser = wx.SingleChoiceDialog(dialog, 'Choose a saved plan with its original request authority.',
                'Scoped translation jobs', [row['mode'] + '; ' + row['status'] + '; ' +
                str(row['completed']) + '/' + str(row['total']) + '; ' + row['id'] for row in plans])
            try:
                if chooser.ShowModal() != wx.ID_OK or not 0 <= chooser.GetSelection() < len(plans):
                    return
                current_job = plans[chooser.GetSelection()]['id']
                plan = scoped.plans.load(current_job)
                selected_text.ChangeValue(snapshot_text(plan['selection']))
                report(json.dumps(plans[chooser.GetSelection()], ensure_ascii=False, indent=2) +
                       '\nSaved source, profile and reservation are immutable. Restored plans cannot borrow old remote permission.')
                controls()
            finally:
                chooser.Destroy()
        except Exception as exc:
            report(str(exc))

    def export(event=None):
        if current_job is None:
            return
        chooser = wx.DirDialog(dialog, 'Choose a parent folder outside the library for a NEW structured derivative')
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            output = Path(chooser.GetPath()) / ('Structured-translation-' + current_job[:8] + '-' + uuid4().hex[:8])
        finally:
            chooser.Destroy()
        identity = current_job
        begin('Exporting structured bilingual derivative',
              lambda: scoped.export(identity, output, cancelled=stopped),
              lambda result: report('Complete derivative saved in: ' + result['path']))

    def new(event=None):
        nonlocal current_job
        current_job = None
        controller.reviewed = None
        budget.ChangeValue('')
        selected_text.ChangeValue('')
        controls()
        report('Review a new exact selection before authorizing another transfer.')

    def clear(event=None):
        chosen.clear()
        for index in range(len(rows)):
            documents.Check(index, False)
        selected()
        chapter.SetItems([])

    def language_query(event=None):
        try:
            selected_profile = profile()
            if wx.MessageBox('Fetch this account’s supported languages? No document text is sent.',
                    'Approve supported-language discovery', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    dialog) != wx.YES:
                return
            approval = language_approval(selected_profile)
            begin('Fetching supported languages',
                lambda: translation.refresh_languages(selected_profile, approval=approval, cancelled=stopped),
                lambda result: languages_loaded())
        except Exception as exc:
            report(str(exc))

    def connections(event=None):
        try:
            from .api_connections import show_api_connections
            show_api_connections(frame)
            load_profiles()
        except Exception as exc:
            report(str(exc))

    def charges(event=None):
        try:
            report(json.dumps(scoped.charge_report(current_job), ensure_ascii=False, indent=2))
        except Exception as exc:
            report(str(exc))

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        alive = False
        cancel.set()
        dialog.EndModal(wx.ID_CLOSE)

    handlers = dict(books=lambda event: load_books(), next=lambda event: load_books(True),
                    clear=clear, chapters=load_chapters, connections=connections, languages=language_query,
                    preview=preview, run=run, pause=lambda event: cancel.set(), saved=saved,
                    charges=charges, export=export, new=new, close=close)
    for name, handler in handlers.items():
        buttons[name].Unbind(wx.EVT_BUTTON)
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    def changed(event=None):
        controller.reviewed = None
        controls()
    def selection_changed(event=None):
        try:
            selected()
        except Exception as exc:
            report(str(exc))
    documents.Bind(wx.EVT_CHECKLISTBOX, selection_changed)
    mode.Bind(wx.EVT_CHOICE, changed)
    chapter.Bind(wx.EVT_CHOICE, changed)
    provider.Bind(wx.EVT_CHOICE, languages_loaded)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        load_profiles()
        controls()
        report('Choose documents and an explicit scope. Load actual EPUB chapters when needed. '
               'Preview is local. Only a separately approved exact transfer contacts the provider. '
               'Outputs retain section order, original headings and source locators; page layout and translation quality are not qualified.')
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        dialog.Destroy()


class TranslationScopeActions:
    def __init__(self, frame):
        self.frame = frame
    def open(self, event=None):
        try:
            show_translation_scope(self.frame)
        except Exception as exc:
            self.frame.report('Scoped translation not opened: ' + str(exc))


def install_translation_scope_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'translation_scope_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise ValueError('Install the shared Tools menu before scoped translation.')
    actions = TranslationScopeActions(frame)
    item = menu.Append(wx.ID_ANY, 'Translate documents, &chapters or batch...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame, CommandSpec('translation.scope', 'Translate documents, chapters or batch', 'main'), item)
    frame.translation_scope_actions = actions
    return actions

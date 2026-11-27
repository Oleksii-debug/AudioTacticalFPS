"""Real recovery/CAS and actual tab handlers with guarded native event stand-ins."""
from dataclasses import replace
from threading import get_ident
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderResume
from carr_library.speech.tabs import ReadingTabs, ReadingTabsStore
from carr_library.ui.reader_tabs import NativeReaderTabs


class Widget:
    def __init__(self, parent=None, *args, value='', label='', **kwargs):
        self.owner, self.parent, self.value, self.label = get_ident(), parent, value, label
        self.destroyed, self.children, self.handlers, self.name = False, [], {}, ''
        self.enabled, self.focused = True, False
        if parent is not None:
            parent.children.append(self)

    def native(self):
        assert get_ident() == self.owner
        assert not self.destroyed, 'Native control used after its destruction.'

    def GetId(self): self.native(); return id(self)
    def SetName(self, value): self.native(); self.name = value
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def SetFocus(self): self.native(); self.focused = True
    def SetSizer(self, value): self.native()
    def Enable(self, value): self.native(); self.enabled = value
    def Bind(self, event, handler): self.native(); self.handlers[event] = handler
    def Destroy(self):
        self.native()
        for child in tuple(self.children): child.Destroy()
        if self.parent is not None: self.parent.children.remove(self)
        self.destroyed = True
        callback = self.handlers.get('destroy')
        if callback is not None:
            callback(SimpleNamespace(GetId=lambda: id(self), Skip=lambda: None))


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class Event:
    def __init__(self, selection): self.selection, self.vetoed, self.skipped = selection, False, False
    def GetSelection(self): return self.selection
    def Veto(self): self.vetoed = True
    def Skip(self): self.skipped = True


@pytest.fixture
def view(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    context = SimpleNamespace(busy=False, authorized=True, stop_error=None, stops=0,
        reports=[], confirmations=[], accepted=True, modal_hook=lambda: None,
        picked_name='Renamed copy', destroyed=False)
    wx = SimpleNamespace(TE_MULTILINE=1, TE_DONTWRAP=2, VERTICAL=3, EXPAND=4,
        EVT_NOTEBOOK_PAGE_CHANGING=5, EVT_NOTEBOOK_PAGE_CHANGED=6, EVT_BUTTON=7, ID_OK=8,
        EVT_WINDOW_DESTROY='destroy', Panel=Widget, TextCtrl=Widget, Button=Widget, BoxSizer=Sizer)
    class Notebook(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); self.pages, self.labels, self.selection = [], [], -1
        def AddPage(self, page, label, select=False):
            self.native(); self.pages.append(page); self.labels.append(label); return True
        def ChangeSelection(self, index): self.native(); self.selection = index
        def GetPageCount(self): self.native(); return len(self.pages)
        def SetPageText(self, index, value): self.native(); self.labels[index] = value
        def DeletePage(self, index):
            self.native(); self.pages.pop(index).Destroy(); self.labels.pop(index)
            # Reentrant page-change events must never capture a destroyed old editor.
            if wx.EVT_NOTEBOOK_PAGE_CHANGING in self.handlers:
                self.handlers[wx.EVT_NOTEBOOK_PAGE_CHANGING](Event(0))
                self.handlers[wx.EVT_NOTEBOOK_PAGE_CHANGED](Event(0))
        def select(self, index):
            self.native(); event = Event(index)
            self.handlers[wx.EVT_NOTEBOOK_PAGE_CHANGING](event)
            if not event.vetoed:
                self.selection = index
                self.handlers[wx.EVT_NOTEBOOK_PAGE_CHANGED](Event(index))
            return event
    class Picker(Widget):
        def ShowModal(self): self.native(); context.modal_hook(); return wx.ID_OK
        def GetValue(self): self.native(); return context.picked_name
    wx.Notebook, wx.TextEntryDialog = Notebook, Picker
    parent = Widget()
    initial = ReaderResume(ReadingDocument('😀 First original.\r\nДругий рядок.'),
                           'voice:first', 2, 71, 3, 4.25)
    tabs = NativeReaderTabs(parent, root, initial, wx_module=wx)
    current = SimpleNamespace(state=tabs.current, voice=initial.voice_id,
        rate=initial.rate, volume=initial.volume, cursor=initial.cursor, seconds=initial.audio_seconds,
        pitch=initial.pitch, punctuation=initial.punctuation)
    def capture():
        state = current.state
        state.resume.document.set_text(state.editor.GetValue())
        # The real reader captures the live native insertion point, which
        # cannot remain beyond the end after an edit deletes its text.
        # Keep the stand-in's code-point cursor within that same native bound;
        # ReaderResume still rejects malformed saved recovery coordinates.
        current.cursor = min(max(0, current.cursor), len(state.resume.document.text))
        return (ReaderResume(state.resume.document, current.voice, current.rate,
                current.volume, current.cursor, current.seconds,
                pitch=current.pitch, punctuation=current.punctuation), state.save_path)
    def activate(state):
        current.state = state
        current.voice, current.rate, current.volume = state.resume.voice_id, state.resume.rate, state.resume.volume
        current.cursor, current.seconds = state.resume.cursor, state.resume.audio_seconds
        current.pitch, current.punctuation = state.resume.pitch, state.resume.punctuation
        state.editor.SetFocus()
    def guard():
        if not context.authorized: raise ValueError('Library changed.')
    def stop():
        context.stops += 1
        if context.stop_error: raise RuntimeError(context.stop_error)
    def confirm():
        context.confirmations.append(current.state.entry.id)
        return context.accepted
    tabs.bind(capture=capture, activate=activate, guard=guard, stop=stop,
        report=lambda message, **kwargs: context.reports.append(message),
        busy=lambda: context.busy, confirm_close=confirm)
    result = SimpleNamespace(tabs=tabs, root=root, current=current, context=context,
                             parent=parent, wx=wx, capture=capture)
    yield result
    tabs.close()
    if not parent.destroyed: parent.Destroy()


def two_copies(view):
    first = view.tabs.current
    first.editor.ChangeValue('😀 First edited.\r\nДругий рядок.')
    view.tabs.on_new()
    assert len(view.tabs.states) == 2
    second = view.tabs.current
    second.editor.ChangeValue('Second independent text.')
    view.current.voice, view.current.rate, view.current.volume = 'voice:second', -2, 63
    view.current.cursor, view.current.seconds = 7, 2.5
    return first, second


def test_switch_and_fresh_reopen_keep_each_copy_voice_cursor_and_undo(view):
    first, second = two_copies(view)
    assert not view.tabs.notebook.select(0).vetoed
    assert view.current.state is first
    assert (view.current.voice, view.current.rate, view.current.cursor, view.current.seconds) == ('voice:first', 2, 3, 4.25)
    assert not view.tabs.notebook.select(1).vetoed
    assert (view.current.voice, view.current.rate, view.current.cursor, view.current.seconds) == ('voice:second', -2, 7, 2.5)
    view.tabs.save_current()
    fresh = NativeReaderTabs(view.parent, view.root, ReaderResume(ReadingDocument('ignored')),
                             wx_module=view.wx)
    assert fresh.active == 1
    assert [state.resume.document.text for state in fresh.states] == [
        '😀 First edited.\r\nДругий рядок.', 'Second independent text.']
    assert fresh.states[0].resume.document.undo()
    assert fresh.states[0].resume.document.text == '😀 First original.\r\nДругий рядок.'
    assert fresh.states[1].resume.document.undo()
    assert fresh.states[1].resume.document.text == ''
    assert first.resume.document.source_text == '😀 First original.\r\nДругий рядок.'
    assert view.context.stops == 3
    fresh.close()


@pytest.mark.parametrize('failure', ['busy', 'library', 'stop', 'draft', 'membership'])
def test_failed_switch_keeps_native_selection_and_both_drafts(view, failure):
    first, second = two_copies(view)
    view.tabs.save_current()
    index_before = view.tabs.store.path.read_bytes()
    expected_second = second.store.path.read_bytes()
    if failure == 'busy': view.context.busy = True
    elif failure == 'library': view.context.authorized = False
    elif failure == 'stop': view.context.stop_error = 'Device close is unconfirmed.'
    elif failure == 'draft':
        other = ReaderResume(ReadingDocument('Independent newer draft.'))
        second.store.save(other)
        expected_second = second.store.path.read_bytes()
    else:
        other = ReadingTabsStore(view.root)
        index, token = other.load()
        other.save(ReadingTabs(index.tabs, first.entry.id), expected_token=token)
        index_before = other.path.read_bytes()
    event = view.tabs.notebook.select(0)
    assert event.vetoed and view.tabs.active == 1 and view.tabs.notebook.selection == 1
    assert view.current.state is second and second.editor.GetValue() == 'Second independent text.'
    assert first.store.load().document.text == '😀 First edited.\r\nДругий рядок.'
    assert second.store.path.read_bytes() == expected_second
    assert view.tabs.store.path.read_bytes() == index_before
    assert view.context.reports[-1]


def test_close_uses_confirmation_preserves_files_and_ignores_reentrant_events(view):
    first, second = two_copies(view)
    view.tabs.save_current()
    retained = second.store.path.read_bytes()
    view.context.accepted = False
    view.tabs.on_close_tab()
    assert len(view.tabs.states) == 2 and view.tabs.active == 1
    view.context.accepted = True
    view.tabs.on_close_tab()
    assert len(view.tabs.states) == 1 and view.tabs.current is first
    assert second.editor.destroyed and first.editor.GetValue().startswith('😀 First edited.')
    assert second.store.path.read_bytes() == retained
    index, _ = view.tabs.store.load()
    assert index.tabs == (first.entry,) and index.active == first.entry.id
    assert not view.tabs.buttons['close'].enabled


def test_late_native_events_after_close_do_not_access_destroyed_controls(view):
    two_copies(view)
    view.tabs.close(); view.parent.Destroy()
    event = Event(0)
    view.tabs.on_changing(event); view.tabs.on_changed(Event(0))
    assert event.vetoed
    for handler in (view.tabs.on_new, view.tabs.on_close_tab, view.tabs.on_rename):
        handler()
    assert len(view.tabs.states) == 2


@pytest.mark.parametrize('changed', [False, True])
def test_rename_after_modal_revalidates_exact_membership_before_native_label(view, changed):
    two_copies(view); view.tabs.save_current()
    before = view.tabs.current.entry
    def mutate():
        if changed:
            other = ReadingTabsStore(view.root)
            index, token = other.load()
            other.save(replace(index, active=index.tabs[0].id), expected_token=token)
    view.context.modal_hook = mutate
    view.tabs.on_rename()
    if changed:
        assert view.tabs.current.entry == before
        assert view.tabs.notebook.labels[1] == before.label
        assert 'Another window' in view.context.reports[-1]
    else:
        assert view.tabs.current.entry.label == 'Renamed copy'
        assert view.tabs.notebook.labels[1] == 'Renamed copy'
        assert view.tabs.store.load()[0].tabs[1].label == 'Renamed copy'


def test_save_all_writes_independent_projects_and_restores_selected_tab(view):
    first, second = two_copies(view)
    def save():
        state = view.tabs.current
        state.resume.document.save(view.root / (state.entry.id + '.reading.json'))
        view.tabs.save_current()
        return True
    assert view.tabs.save_all(save)
    assert view.tabs.current is second
    assert not first.resume.document.dirty and not second.resume.document.dirty
    for state in (first, second):
        loaded = ReadingDocument.load(view.root / (state.entry.id + '.reading.json'))
        assert loaded.text == state.resume.document.text and loaded.source_text == state.resume.document.source_text
    # A newly created, already-persisted blank is clean. Erasure is tested below.
    view.tabs.on_new()
    assert view.tabs.save_all(lambda: pytest.fail('An unchanged saved blank must not be saved again.'))


def test_cancel_close_stays_on_unsaved_tab_and_retains_other_copy(view):
    first, second = two_copies(view)
    view.context.accepted = False
    assert not view.tabs.confirm_all_close()
    assert view.tabs.current is first
    assert view.context.confirmations == [first.entry.id]
    assert second.resume.document.text == 'Second independent text.'


def test_busy_close_does_not_activate_a_different_copy_for_confirmation(view):
    first, second = two_copies(view)
    view.context.busy = True
    with pytest.raises(ValueError, match='Stop the current reader operation'):
        view.tabs.confirm_all_close()
    assert view.tabs.current is second and view.context.confirmations == []


def test_edit_invalidates_saved_numeric_playback_position(view):
    document = view.tabs.current.resume.document
    assert view.tabs.saved_audio_position(document) == 4.25
    document.set_text('Changed text invalidates old timing.')
    assert view.tabs.saved_audio_position(document) == 0


def test_explicit_text_appends_without_overwriting_recovered_copy(view):
    view.tabs.save_current()
    before = view.tabs.current.store.path.read_bytes()
    added = ReaderResume(ReadingDocument('New explicit selected text.'))
    fresh = NativeReaderTabs(view.parent, view.root, added, explicit_text=True, wx_module=view.wx)
    assert fresh.active == 1 and len(fresh.states) == 2
    assert fresh.current.resume.document.text == 'New explicit selected text.'
    assert fresh.states[0].store.path.read_bytes() == before
    fresh.close()


def test_explicit_text_preserves_pre_tabs_manual_recovery(view):
    view.tabs.current.store.save(view.tabs.current.resume)
    assert not view.tabs.store.path.exists()
    before = view.tabs.current.store.path.read_bytes()
    fresh = NativeReaderTabs(view.parent, view.root,
        ReaderResume(ReadingDocument('New explicit text.')), explicit_text=True, wx_module=view.wx)
    assert len(fresh.states) == 2 and fresh.active == 1
    assert fresh.states[0].resume.document.source_text.startswith('😀 First original.')
    assert fresh.current.resume.document.source_text == 'New explicit text.'
    assert fresh.states[0].store.path.read_bytes() == before
    fresh.close()



def _save_clean_first_copy(view):
    state = view.tabs.current
    state.resume.document.save(view.root / 'saved-reading-copy.json')
    view.tabs.save_current()
    assert not state.resume.document.dirty
    return state


def test_clean_close_flushes_latest_voice_cursor_and_numeric_audio_before_autosave(view):
    state = _save_clean_first_copy(view)
    view.current.voice, view.current.rate, view.current.volume = 'voice:chosen-later', -1, 62
    view.current.cursor, view.current.seconds = 11, 6.75
    assert view.tabs.confirm_all_close()
    assert view.context.confirmations == []
    restored = NativeReaderTabs(view.parent, view.root, ReaderResume(ReadingDocument('ignored')),
                                wx_module=view.wx)
    resume = restored.current.resume
    assert (resume.voice_id, resume.rate, resume.volume, resume.cursor, resume.audio_seconds) == (
        'voice:chosen-later', -1, 62, 11, 6.75)
    assert resume.document.source_text == state.resume.document.source_text
    assert resume.document.text == state.resume.document.text
    restored.close()


def test_dirty_discard_does_not_flush_rejected_edits_or_replace_prior_recovery(view):
    first = _save_clean_first_copy(view)
    view.tabs.on_new()
    second = view.tabs.current
    before = second.store.path.read_bytes()
    first_before = first.store.path.read_bytes()
    second.editor.ChangeValue('Changes explicitly not saved on close.')
    view.context.accepted = True  # Confirm returns true without invoking Save.
    assert view.tabs.confirm_all_close()
    assert view.context.confirmations == [second.entry.id]
    assert second.resume.document.dirty
    assert second.store.path.read_bytes() == before
    assert first.store.path.read_bytes() == first_before


def test_cancel_other_dirty_copy_does_not_flush_active_clean_copy(view):
    first = _save_clean_first_copy(view)
    view.tabs.on_new()
    second = view.tabs.current
    second.editor.ChangeValue('Another copy still needs a close decision.')
    assert not view.tabs.notebook.select(0).vetoed
    assert view.tabs.current is first
    before = first.store.path.read_bytes()
    view.current.cursor = 12
    view.context.accepted = False
    assert not view.tabs.confirm_all_close()
    assert view.context.confirmations == [second.entry.id]
    assert first.store.path.read_bytes() == before
    assert view.tabs.current is second


def test_clean_close_cas_conflict_preserves_other_writer_and_prevents_completion(view):
    state = _save_clean_first_copy(view)
    index_before = view.tabs.store.path.read_bytes()
    other = view.tabs.store.draft(state.entry)
    other.save(ReaderResume(ReadingDocument('Another window saved these exact bytes.'),
                            'voice:other-window'))
    retained = other.path.read_bytes()
    view.current.cursor = 12
    with pytest.raises(ValueError, match='Another window changed'):
        view.tabs.confirm_all_close()
    assert other.path.read_bytes() == retained
    assert view.tabs.store.path.read_bytes() == index_before
    assert view.context.confirmations == []


def test_erased_dirty_copy_is_saved_by_shared_all_tab_action(view):
    first, second = two_copies(view)
    view.tabs.save_current()  # Retain the inserted text before its explicit deletion.
    second.editor.ChangeValue('')
    saved = []
    def save():
        state = view.tabs.current
        saved.append(state.entry.id)
        state.resume.document.save(view.root / (state.entry.id + '.empty-check.reading.json'))
        view.tabs.save_current()
        return True
    assert view.tabs.save_all(save)
    assert saved == [first.entry.id, second.entry.id]
    assert second.resume.document.text == '' and not second.resume.document.dirty
    assert second.resume.cursor == 0 and second.store.load().cursor == 0
    restored = ReadingDocument.load(view.root / (second.entry.id + '.empty-check.reading.json'))
    assert restored.text == '' and restored.undo()
    assert restored.text == 'Second independent text.'
    assert first.resume.document.source_text.startswith('😀 First original.')


def test_erased_dirty_copy_cannot_skip_close_or_cas_conflict(view):
    first, _ = two_copies(view)
    assert not view.tabs.notebook.select(0).vetoed
    before = first.store.path.read_bytes()
    first.editor.ChangeValue('')
    view.context.accepted = False
    assert not view.tabs.confirm_all_close()
    assert view.context.confirmations == [first.entry.id]
    assert first.resume.document.dirty and first.resume.document.text == ''
    assert first.resume.cursor == 0
    assert first.store.path.read_bytes() == before
    other = ReaderResume(ReadingDocument('Another window won the draft save.'))
    first.store.save(other)
    winning = first.store.path.read_bytes()
    with pytest.raises(ValueError, match='Another window'):
        view.tabs.save_current()
    assert first.store.path.read_bytes() == winning
    assert first.editor.GetValue() == '' and first.resume.document.dirty
    assert first.resume.document.source_text.startswith('😀 First original.')


def test_explicit_incoming_text_is_added_to_membership_before_fresh_reopen(view):
    view.tabs.save_current()
    retained = view.tabs.current.store.path.read_bytes()
    incoming = ReaderResume(ReadingDocument('Explicit new text must remain a listed tab.'))
    fresh = NativeReaderTabs(view.parent, view.root, incoming, explicit_text=True, wx_module=view.wx)
    fresh.bind(capture=lambda: (fresh.current.resume, None), activate=lambda state: None,
        guard=lambda: None, stop=lambda: None, report=lambda *_args, **_kw: None,
        busy=lambda: False, confirm_close=lambda: True)
    assert fresh.published is None
    fresh.save_current()
    added_id = fresh.current.entry.id
    fresh.close()
    reopened = NativeReaderTabs(view.parent, view.root, ReaderResume(ReadingDocument('')),
                                wx_module=view.wx)
    assert reopened.current.entry.id == added_id and len(reopened.states) == 2
    assert reopened.current.resume.document.source_text == incoming.document.source_text
    assert reopened.states[0].store.path.read_bytes() == retained
    reopened.close()


def test_owner_id_destruction_closes_tabs_without_touching_the_invalid_wrapper(view):
    child = SimpleNamespace(GetId=lambda: view.tabs.current.editor.GetId(), Skip=lambda: None)
    view.parent.handlers['destroy'](child)
    assert view.tabs.alive
    owner_id = view.parent.GetId()
    view.parent.destroyed = True
    view.parent.handlers['destroy'](SimpleNamespace(GetId=lambda: owner_id, Skip=lambda: None))
    assert not view.tabs.alive
    view.tabs.on_new()
    view.tabs.on_changed(Event(0))
    assert view.tabs.current.resume.document.source_text.startswith('😀 First original.')


@pytest.fixture
def installed_root_probe():
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_reading_tabs_smoke.py'
    module = ast.parse(source.read_text(encoding='utf-8'))
    node = next(item for item in module.body if isinstance(item, ast.FunctionDef)
                and item.name == '_installed_package_root')
    scope = {'Path': Path}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), scope)
    return scope['_installed_package_root']


def _distribution_record(root, members=('carr_library/__init__.py',)):
    return SimpleNamespace(files=members, locate_file=lambda member: root / str(member))


def test_installed_probe_uses_recorded_package_when_source_egg_info_precedes_wheel(
        tmp_path, installed_root_probe):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    source = checkout / 'src' / 'carr_library'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text('# Current source', encoding='utf-8')
    installed = tmp_path / 'site-packages' / 'carr_library'
    installed.mkdir(parents=True)
    actual = installed / '__init__.py'
    actual.write_text('# Current installed artifact', encoding='utf-8')
    records = [_distribution_record(checkout, ('src/carr_library/__init__.py',)),
               _distribution_record(checkout),  # Stale flat project metadata.
               _distribution_record(installed.parent)]
    assert installed_root_probe(records, actual, checkout) == installed


@pytest.mark.parametrize('records', ['missing', 'another-wheel', 'ambiguous'])
def test_installed_probe_never_accepts_an_unbound_or_ambiguous_package(
        tmp_path, installed_root_probe, records):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    installed = tmp_path / 'site-packages' / 'carr_library'
    installed.mkdir(parents=True)
    actual = installed / '__init__.py'
    actual.write_text('# Captured package', encoding='utf-8')
    if records == 'missing':
        inventory = [SimpleNamespace(files=None)]
    elif records == 'another-wheel':
        other = tmp_path / 'other-site' / 'carr_library'
        other.mkdir(parents=True)
        (other / '__init__.py').write_text('# Not imported', encoding='utf-8')
        inventory = [_distribution_record(other.parent)]
    else:
        inventory = [_distribution_record(installed.parent), _distribution_record(installed.parent)]
    with pytest.raises(RuntimeError, match='Exactly one installed wheel record'):
        installed_root_probe(inventory, actual, checkout)


def test_installed_probe_rejects_checkout_even_with_matching_distribution_record(
        tmp_path, installed_root_probe):
    checkout = tmp_path / 'checkout'
    package = checkout / 'carr_library'
    package.mkdir(parents=True)
    actual = package / '__init__.py'
    actual.write_text('# Checkout cannot satisfy installed acceptance', encoding='utf-8')
    with pytest.raises(RuntimeError, match='imported the checkout'):
        installed_root_probe([_distribution_record(checkout)], actual, checkout)


def test_native_synthesis_options_inherit_then_remain_independent_across_tabs_and_reopen(view):
    view.current.pitch, view.current.punctuation = 0, True
    first = view.tabs.current
    view.tabs.on_new()
    assert len(view.tabs.states) == 2
    assert (view.current.pitch, view.current.punctuation) == (0, True)
    second = view.tabs.current
    second.editor.ChangeValue('A different narrated reading copy.')
    view.current.pitch, view.current.punctuation = 99, False
    assert not view.tabs.notebook.select(0).vetoed
    assert view.current.state is first
    assert (view.current.pitch, view.current.punctuation) == (0, True)
    assert not view.tabs.notebook.select(1).vetoed
    assert view.current.state is second
    assert (view.current.pitch, view.current.punctuation) == (99, False)
    view.tabs.save_current()
    reopened = NativeReaderTabs(view.parent, view.root, ReaderResume(ReadingDocument('ignored')),
                                wx_module=view.wx)
    assert [(row.resume.pitch, row.resume.punctuation) for row in reopened.states] == [(0, True), (99, False)]
    assert reopened.current.resume.document.text == 'A different narrated reading copy.'
    assert first.resume.document.source_text == '😀 First original.\r\nДругий рядок.'
    reopened.close()


def test_failed_tab_cas_keeps_unsaved_native_settings_and_prior_disk_record(view):
    view.tabs.save_current()
    first = view.tabs.current
    original, token = first.store.load_with_token()
    replacement = replace(original, pitch=40, punctuation=True)
    newer = first.store.save_with_token(replacement, expected_token=token)
    view.current.pitch, view.current.punctuation = 75, False
    view.tabs.on_new()
    assert len(view.tabs.states) == 1 and view.tabs.current is first
    assert (view.current.pitch, view.current.punctuation) == (75, False)
    persisted, actual_token = first.store.load_with_token()
    assert actual_token == newer and (persisted.pitch, persisted.punctuation) == (40, True)
    assert any('Another window' in report for report in view.context.reports)


@pytest.mark.parametrize('field,value', [('voice_id', 'other:voice'), ('rate', 3), ('volume', 72),
                                         ('pitch', 50), ('punctuation', True)])
def test_saved_audio_position_binds_exact_synthesis_request(view, field, value):
    state = view.tabs.current
    saved = state.resume
    request = dict(voice_id=saved.voice_id, rate=saved.rate, volume=saved.volume,
                   pitch=saved.pitch, punctuation=saved.punctuation)
    exact = tuple(request.values())
    assert view.tabs.saved_audio_position(saved.document, synthesis=exact) == saved.audio_seconds == 4.25
    request[field] = value
    assert view.tabs.saved_audio_position(saved.document, synthesis=tuple(request.values())) == 0
    assert state.resume is saved and state.resume.audio_seconds == 4.25
    saved.document.replace(0, 1, 'Changed')
    assert view.tabs.saved_audio_position(saved.document, synthesis=exact) == 0

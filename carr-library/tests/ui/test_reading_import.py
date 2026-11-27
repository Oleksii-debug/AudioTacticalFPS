"""Headless scope/lifecycle policy for the native historical-reading flow."""
from hashlib import sha256
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.legacy_reading import DIRECTORY, LegacyReadingStore
from carr_library.reader.narration import NarrationStore
from carr_library.ui.reading_import import ReadingImportActions, install_reading_import_actions


class Frame:
    def __init__(self, library, workspace):
        self.library, self.active = library, (workspace, None)
        self.working, self.busy, self._closing = False, False, False
        self.cancel_event = threading.Event()
        self.jobs, self.messages = [], []
        self.before_done = None

    def run_job(self, title, work, done):
        assert not self.working
        self.working = True
        self.cancel_event.clear()
        self.jobs.append((title, work, done))

    def settle(self):
        _, work, done = self.jobs.pop(0)
        try:
            value = work()
        except Exception as exc:
            self.messages.append(str(exc))
            self.working = False
            return
        self.working = False
        if self.before_done is not None:
            callback, self.before_done = self.before_done, None
            callback()
        done(value)

    def report(self, value):
        self.messages.append(value)


class Dialogs:
    def __init__(self, path):
        self.selected_path = path
        self.choices = [1]
        self.title = 'Explicitly imported reading copy'
        self.previews = []
        self.on_choose = None
        self.on_preview = None

    def path(self, parent):
        return self.selected_path

    def choose(self, parent, title, label, choices):
        if self.on_choose is not None:
            self.on_choose()
        return self.choices.pop(0)

    def preview(self, parent, snapshot):
        self.previews.append(snapshot)
        if self.on_preview is not None:
            self.on_preview()
        return self.title


@pytest.fixture
def flow(tmp_path):
    library = LibraryApp(tmp_path / 'data')
    workspace = library.create_workspace('Active library')
    value = dict(schema='carr.reading-copy.v1', title='Earlier saved copy',
        source_text='Original', source_sha256=sha256(b'Original').hexdigest(),
        edits=[dict(start=0, end=8, replacement='Selected historical text', label='Retained edit')],
        revision=0)
    path = tmp_path / 'earlier.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    frame, dialogs = Frame(library, workspace), Dialogs(path)
    opened = []
    actions = ReadingImportActions(frame, dialogs=dialogs, editor=lambda *args, **kwargs: opened.append((args, kwargs)))
    return frame, dialogs, actions, opened, path


def finish(frame):
    while frame.jobs:
        frame.settle()


def test_explicit_historical_selection_saves_exact_archive_then_opens_existing_editor(flow):
    frame, dialogs, actions, opened, path = flow
    original = path.read_bytes()
    actions.import_file()
    frame.settle()
    assert not (frame.library.root / DIRECTORY).exists()
    frame.settle()
    assert dialogs.previews[0].selected_revision == 1
    assert not (frame.library.root / DIRECTORY).exists()
    frame.settle()
    assert len(opened) == 1 and not frame.jobs
    args, kwargs = opened[0]
    assert args[0] is frame and isinstance(args[1], NarrationStore)
    project = args[2]
    assert project.current_text == 'Selected historical text'
    assert project.legacy_origin.selected_revision == 1
    assert args[1].load(project.id) == (project, kwargs['expected_digest'])
    assert LegacyReadingStore(frame.library.root).load(project.legacy_origin.archive_sha256).raw == original
    assert path.read_bytes() == original
    kwargs['guard']()


@pytest.mark.parametrize('stage', ['file', 'revision', 'preview'])
def test_cancelling_any_review_stage_keeps_both_original_and_store_unchanged(flow, stage):
    frame, dialogs, actions, opened, path = flow
    original = path.read_bytes()
    if stage == 'file':
        dialogs.selected_path = None
    elif stage == 'revision':
        dialogs.choices = [None]
    else:
        dialogs.title = None
    actions.import_file()
    finish(frame)
    assert not opened
    assert not (frame.library.root / DIRECTORY).exists()
    assert not (frame.library.root / 'narration-projects').exists()
    assert path.read_bytes() == original


@pytest.mark.parametrize('stage', ['revision', 'preview', 'completion'])
def test_scope_changes_cannot_publish_or_open_into_new_library(flow, stage):
    frame, dialogs, actions, opened, _ = flow
    old_root = frame.library.root
    other = frame.library.create_workspace('Another active library')
    change = lambda: setattr(frame, 'active', (other, None))
    if stage == 'revision':
        dialogs.on_choose = change
    elif stage == 'preview':
        dialogs.on_preview = change
    actions.import_file()
    if stage == 'completion':
        frame.before_done = change
    finish(frame)
    assert not opened and any('changed' in message for message in frame.messages)
    assert not (old_root / DIRECTORY).exists()


def test_late_cancelled_worker_result_never_launches_editor_or_new_save(flow):
    frame, dialogs, actions, opened, _ = flow
    actions.import_file()
    frame.cancel_event.set()
    frame.settle()
    assert not opened and not dialogs.previews
    assert not (frame.library.root / DIRECTORY).exists()
    assert any('cancelled' in message for message in frame.messages)


def test_a_previous_cancel_latch_does_not_disable_a_new_explicit_import(flow):
    frame, _, actions, opened, _ = flow
    frame.cancel_event.set()
    actions.import_file()
    finish(frame)
    assert len(opened) == 1


def test_invalid_new_title_fails_before_retaining_a_history(flow):
    frame, dialogs, actions, opened, _ = flow
    dialogs.title = 'x' * 201
    actions.import_file()
    finish(frame)
    assert not opened and any('title' in message for message in frame.messages)
    assert not (frame.library.root / DIRECTORY).exists()


def test_retained_history_reopens_original_cursor_and_redo_without_original_file(flow):
    frame, dialogs, actions, opened, path = flow
    actions.import_file()
    finish(frame)
    first = opened[0][0][2]
    path.unlink()
    dialogs.choices = [0, 0]
    actions.open_retained()
    finish(frame)
    assert len(opened) == 2
    second = opened[1][0][2]
    assert second.id != first.id and second.current_text == 'Original'
    assert second.legacy_origin.archive_sha256 == first.legacy_origin.archive_sha256
    assert second.legacy_origin.selected_revision == 0
    assert len(list((frame.library.root / DIRECTORY).glob('*.json'))) == 1


def test_busy_frame_rejects_a_second_import_without_opening_native_picker(flow):
    frame, dialogs, actions, opened, _ = flow
    frame.working = True
    dialogs.path = lambda parent: pytest.fail('A second native picker must not open.')
    actions.import_file()
    assert not frame.jobs and not opened
    assert any('Wait' in message for message in frame.messages)


def test_installation_is_idempotent_and_registers_semantic_commands_without_hotkeys(monkeypatch):
    from carr_library.ui import keyboard_settings
    fake_wx = SimpleNamespace(ID_ANY=-1, EVT_MENU='menu')
    monkeypatch.setitem(sys.modules, 'wx', fake_wx)
    specs, items = [], []
    monkeypatch.setattr(keyboard_settings, 'register_menu_command',
                        lambda frame, spec, item: specs.append(spec))
    class Menu:
        def Append(self, identity, label):
            item = SimpleNamespace(label=label)
            items.append(item)
            return item
    menu = Menu()
    bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabel=lambda index: '&Tools',
                          GetMenu=lambda index: menu)
    bindings = []
    frame = SimpleNamespace(menu_bar=bar, Bind=lambda *args: bindings.append(args))
    first = install_reading_import_actions(frame)
    assert install_reading_import_actions(frame) is first
    assert len(items) == len(bindings) == len(specs) == 2
    assert [spec.command_id for spec in specs] == [
        'reading.import_legacy_copy', 'reading.open_legacy_history']
    assert all(spec.context == 'main' and spec.default_shortcut == '' for spec in specs)

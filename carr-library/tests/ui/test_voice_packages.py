"""Faithful native interfaces plus the real existing single-worker controller."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import gc
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import weakref

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.voice_lifecycle import ProtectedVoicePackageReview
from carr_library.audio.voice_packages import VoicePackageReview
from carr_library.ui import voice_packages as ui

PASSWORD = 'UI-only protected fixture password'


def protected_review(root):
    document = {'name': 'Synthetic', 'id': 'fixture', 'version': '1.0.0',
        'engine': {'id': 'omnivoice'}, 'languages': ['uk'],
        'files': {'sample.txt': {'role': 'reference_text'}},
        'provenance': {'source': 'synthetic UI fixture', 'license': 'Fixture'}}
    return ProtectedVoicePackageReview(str(root), (1, 2), 'a' * 64, 1,
        str(root.parent / 'selected.carr-voice.enc'), 'b' * 64, 100,
        'c' * 64, 50, json.dumps(document))


@pytest.fixture
def view(tmp_path, monkeypatch):
    main = threading.get_ident()
    callbacks, queued, callback_lock = [], threading.Event(), threading.Lock()
    pools, calls, failures = [], [], []
    wx = SimpleNamespace(picker_path=str(tmp_path / 'selected.carr-voice.enc'),
        on_picker=lambda: None, on_password=lambda: None)
    for index, name in enumerate((
            'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
            'TE_MULTILINE', 'TE_READONLY', 'TE_PASSWORD', 'EXPAND', 'ALL', 'LEFT', 'TOP',
            'ALIGN_CENTER_VERTICAL', 'ALIGN_RIGHT', 'FD_OPEN', 'FD_FILE_MUST_EXIST',
            'FD_SAVE', 'OK', 'CANCEL', 'ICON_INFORMATION', 'YES_NO', 'NO_DEFAULT',
            'TAB_TRAVERSAL', 'VSCROLL')):
        setattr(wx, name, 1 << index)
    wx.ID_CLOSE, wx.ID_CANCEL, wx.ID_OK = 5102, 5101, 5100
    wx.YES, wx.NO = 5103, 5104
    wx.question_answer = wx.YES
    wx.EVT_TEXT, wx.EVT_CHOICE, wx.EVT_BUTTON = 'text', 'choice', 'button'
    wx.EVT_CLOSE, wx.EVT_WINDOW_DESTROY = 'close', 'destroy'
    wx.picker_answer = wx.ID_OK
    ids = iter(range(100, 10000))
    widgets = []

    class Widget:
        def __init__(self, parent=None, *args, value='', label='', choices=(), title='', **kwargs):
            self.parent, self.value, self.label = parent, value, label
            self.choices, self.selection, self.title = list(choices), 0, title
            self.id, self.dead = next(ids), False
            self.bindings, self.enabled, self.destroy_count = {}, True, 0
            widgets.append(self)
        def live(self):
            assert threading.get_ident() == main, 'Native control accessed on worker.'
            current = self
            while isinstance(current, Widget):
                assert not current.dead, 'Deleted native wrapper was touched.'
                current = current.parent
        def __bool__(self):
            current = self
            while isinstance(current, Widget):
                if current.dead:
                    return False
                current = current.parent
            return True
        def SetName(self, value): self.live(); self.name = value
        def GetLabel(self): self.live(); return self.label
        def GetId(self): self.live(); return self.id
        def GetValue(self): self.live(); return self.value
        def SetValue(self, value): self.live(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def SetSelection(self, value): self.live(); self.selection = value
        def GetSelection(self): self.live(); return self.selection
        def GetStringSelection(self): self.live(); return self.choices[self.selection]
        def SetStringSelection(self, value): self.live(); self.selection = self.choices.index(value)
        def SetFocus(self): self.live()
        def SetInsertionPoint(self, value): self.live()
        def SetMinSize(self, value): self.live()
        def SetSizer(self, value): self.live()
        def SetScrollRate(self, *args): self.live()
        def FitInside(self): self.live()
        def Wrap(self, value): self.live()
        def Enable(self, value=True): self.live(); self.enabled = value
        def Bind(self, event, callback): self.live(); self.bindings[event] = callback
        def IsModal(self): self.live(); return False
        def EndModal(self, value): self.live()
        def Hide(self): self.live()
        def SetAffirmativeId(self, value): self.live()
        def SetEscapeId(self, value): self.live()
        def CreateStdDialogButtonSizer(self, style): self.live(); return Sizer()
        def ShowModal(self):
            self.live()
            if self.title == 'Protected voice package password':
                return wx.on_password_window(self)
            return wx.ID_CANCEL
        def Destroy(self):
            self.live()
            self.destroy_count += 1
            self.dead = True
            event = SimpleNamespace(GetId=lambda: self.id, Skip=lambda: None,
                GetEventObject=lambda: pytest.fail('Do not touch a dying owner proxy.'))
            if wx.EVT_WINDOW_DESTROY in self.bindings:
                self.bindings[wx.EVT_WINDOW_DESTROY](event)

    class CheckBox(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, value=False, **kwargs)

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): assert threading.get_ident() == main
        def AddGrowableCol(self, *args, **kwargs): assert threading.get_ident() == main

    class Picker(Widget):
        def ShowModal(self): self.live(); wx.on_picker(); return wx.picker_answer
        def GetPath(self): self.live(); return wx.picker_path

    for name in ('Dialog', 'ScrolledWindow', 'TextCtrl', 'StaticText', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.CheckBox, wx.FileDialog = CheckBox, Picker
    wx.BoxSizer = wx.WrapSizer = wx.FlexGridSizer = Sizer
    wx.MessageBox = lambda *a, **k: wx.question_answer if a[2] & wx.YES_NO else wx.ID_OK
    def call_after(callback):
        with callback_lock:
            callbacks.append(callback)
            queued.set()
    wx.CallAfter = call_after
    monkeypatch.setitem(sys.modules, 'wx', wx)

    root = tmp_path / 'library'
    library = LibraryApp(root)
    manager = SimpleNamespace(store=SimpleNamespace(root=root, state_root=root / 'state',
        read=lambda: {'revision': 1, 'config': {'dordiai_manifest': str(root / 'owner.json')}}))
    manager.configuration = lambda **kwargs: dict(revision=1, reference_engine='omnivoice',
        models='models', reference_model='models/Holos', references='models/voices')
    review = protected_review(root)
    def inspect(path, *, passphrase, cancelled):
        assert threading.get_ident() != main
        assert passphrase == PASSWORD and not cancelled()
        calls.append(('inspect', Path(path)))
        return review
    def apply(value, *, passphrase, rights_approved, supported_languages, cancelled):
        assert threading.get_ident() != main
        assert passphrase == PASSWORD and rights_approved is True
        assert supported_languages == ('uk',) and not cancelled()
        calls.append(('apply', value))
        return {'revision': 2}
    def export(value, path, *, passphrase, export_rights_approved, cancelled):
        assert threading.get_ident() != main
        assert passphrase == PASSWORD and export_rights_approved is True and not cancelled()
        calls.append(('export', value, Path(path)))
        return dict(path=str(path), sha256='d' * 64, encrypted=True, size_bytes=100)
    lifecycle = SimpleNamespace(inspect_protected=inspect, apply_protected=apply,
                                export_protected=export)
    monkeypatch.setattr(ui, 'VoicePackageManager', lambda root, **kwargs: manager)
    monkeypatch.setattr(ui, 'VoiceProfileLifecycle', lambda root, **kwargs: lifecycle)
    executor = ThreadPoolExecutor(max_workers=1)
    pools.append(executor)
    frame = SimpleNamespace(library=library, _closing=False, working=False,
                            cancel_event=threading.Event())
    admissions, refreshes, authority = [], [], {'current': True}
    def guard():
        if not authority['current']:
            raise InterruptedError('The parent settings view is no longer current.')
    def run_job(title, operation, done=None, failed=None):
        assert threading.get_ident() == main
        if frame._closing or frame.working:
            return None
        frame.working = True
        frame.cancel_event.clear()
        try:
            future = executor.submit(operation)
        except Exception as error:
            frame.working = False
            if failed is not None:
                failed(error)
            return None
        admissions.append(future)
        def settled(completed):
            def deliver():
                frame.working = False
                if frame._closing:
                    return
                try:
                    value = completed.result()
                except Exception as error:
                    if failed is not None:
                        failed(error)
                else:
                    if done is not None:
                        done(value)
            call_after(deliver)
        future.add_done_callback(settled)
        return future
    frame.run_job = run_job
    controller = ui.VoicePackageDialog(frame, scope_guard=guard,
        on_settled=lambda: refreshes.append(threading.get_ident()))
    def finish():
        # Drain the actual single borrowed worker before its queued GUI turn.
        executor.submit(lambda: None).result(timeout=5)
        assert queued.wait(5), 'The existing worker did not deliver completion.'
        while True:
            with callback_lock:
                ready = callbacks[:]
                callbacks.clear()
                queued.clear()
            if not ready:
                break
            for callback in ready:
                callback()
    finish()
    def password(*, confirm=False):
        wx.on_password()
        return PASSWORD
    controller._password = password
    host = SimpleNamespace(controller=controller, wx=wx, lifecycle=lifecycle, manager=manager,
        calls=calls, review=review, root=root, finish=finish, pools=pools, widgets=widgets,
        callbacks=callbacks, queued=queued, frame=frame, admissions=admissions,
        refreshes=refreshes, authority=authority, main=main)
    try:
        yield host
    finally:
        controller.close()
        for executor in pools:
            executor.shutdown(wait=True, cancel_futures=True)
        if not controller.destroyed:
            controller.dialog.Destroy()


def inspect_ready(host):
    assert host.controller.inspect_protected()
    host.finish()
    assert host.controller.review is host.review


def installed_review(host):
    old = host.review
    review = VoicePackageReview('activate', old.manifest_sha256, old.settings_revision,
                               'fixture', old.plaintext_sha256, old.document_json)
    host.controller.reviewed(review)
    return review


def test_existing_worker_inspect_then_fresh_rights_and_password_apply(view):
    host = view
    c = host.controller
    inspect_ready(host)
    assert not c.rights.GetValue() and c.supported.GetValue() == ''
    assert c.apply() is False
    assert [call[0] for call in host.calls] == ['inspect']
    c.rights.SetValue(True)
    c.supported.ChangeValue('uk')
    assert c.apply()
    assert c.review is None and not c.rights.GetValue()
    host.finish()
    assert [call[0] for call in host.calls] == ['inspect', 'apply']
    assert len(host.pools) == 1 and c.revision == 2
    assert PASSWORD not in c.status.GetValue()


@pytest.mark.parametrize('changed', ['permission', 'language', 'review', 'cancel', 'scope', 'destroy'])
def test_apply_rechecks_all_inputs_after_password_modal(view, changed):
    host, c = view, view.controller
    inspect_ready(host)
    c.rights.SetValue(True)
    c.supported.ChangeValue('uk')
    def alter():
        if changed == 'permission':
            c.rights.SetValue(False)
        elif changed == 'language':
            c.supported.ChangeValue('sk')
        elif changed == 'review':
            c.invalidate()
        elif changed == 'cancel':
            c.stop()
        elif changed == 'scope':
            host.frame.library = SimpleNamespace(root=host.root)
        else:
            c.dialog.Destroy()
    host.wx.on_password = alter
    assert c.apply() is False
    assert [call[0] for call in host.calls] == ['inspect']


@pytest.mark.parametrize('changed', ['permission', 'review', 'cancel', 'scope', 'destroy'])
def test_export_rechecks_review_after_picker_and_password_modal(view, changed):
    host, c = view, view.controller
    installed_review(host)
    c.protected_export_rights.SetValue(True)
    def alter():
        if changed == 'permission':
            c.protected_export_rights.SetValue(False)
        elif changed == 'review':
            c.invalidate()
        elif changed == 'cancel':
            c.stop()
        elif changed == 'scope':
            host.frame.library = SimpleNamespace(root=host.root)
        else:
            c.dialog.Destroy()
    host.wx.on_password = alter
    assert c.export_protected() is False
    assert host.calls == []


def test_protected_export_has_separate_permission_and_existing_plain_route(view):
    host, c = view, view.controller
    review = installed_review(host)
    c.export_rights.SetValue(True)
    assert c.export_protected() is False and host.calls == []
    c.protected_export_rights.SetValue(True)
    assert c.export_protected()
    host.finish()
    assert host.calls == [('export', review, Path(host.wx.picker_path))]
    assert 'encrypted reference package saved' in c.status.GetValue()
    assert not c.export_rights.GetValue() and not c.protected_export_rights.GetValue()
    assert PASSWORD not in c.status.GetValue()


def test_cancelled_inspection_completion_never_becomes_apply_authority(view):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    c.stop()
    host.finish()
    assert c.review is None and not c.rights.GetValue()
    assert [call[0] for call in host.calls] == ['inspect']


def test_direct_destroy_discards_completed_review_without_touching_controls(view):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    c.dialog.Destroy()
    host.finish()
    assert c.closed and c.destroyed and c.review is None
    assert [call[0] for call in host.calls] == ['inspect']


def test_queued_cancel_drops_secret_even_when_gui_completion_is_suppressed(view):
    host, c = view, view.controller
    started, release = threading.Event(), threading.Event()
    blocker = host.pools[0].submit(lambda: (started.set(), release.wait(5)))
    assert started.wait(5)
    class Secret(str):
        pass
    password = Secret(PASSWORD)
    retained = weakref.ref(password)
    try:
        assert c._start_secret(lambda value: pytest.fail('Cancelled queued operation ran.'),
                               lambda value: pytest.fail('Destroyed GUI received completion.'), password)
        del password
        assert retained() is not None
        c.dialog.Destroy()
        gc.collect()
        assert retained() is None
    finally:
        release.set()
        blocker.result(timeout=5)
    assert c.closed and c.destroyed


def test_secret_submit_failure_clears_handoff_and_restores_idle_state(view, monkeypatch):
    c = view.controller
    observed = []
    def reject(action):
        observed.append(action)
        raise RuntimeError('Synthetic existing-worker rejection.')
    monkeypatch.setattr(view.pools[0], 'submit', reject)
    class Secret(str):
        pass
    password = Secret(PASSWORD)
    retained = weakref.ref(password)
    assert c._start_secret(lambda value: None, lambda value: None, password) is False
    assert 'Synthetic existing-worker rejection.' in c.status.GetValue()
    del password
    gc.collect()
    assert retained() is None and not c.busy
    assert len(observed) == 1


@pytest.mark.parametrize('destroy', [False, True])
def test_native_password_entry_clears_controls_and_uses_captured_destroy_identity(view, destroy):
    host, c, wx = view, view.controller, view.wx
    entries = []
    def on_password(window):
        fields = [widget for widget in host.widgets if widget.parent is window
                  and getattr(widget, 'name', '').endswith('protected voice operation only')]
        assert len(fields) == 2
        entries.extend(fields)
        for field in fields:
            field.ChangeValue(PASSWORD)
        if destroy:
            window.Destroy()
        return wx.ID_OK
    wx.on_password_window = on_password
    result = ui.VoicePackageDialog._password(c, confirm=True)
    if destroy:
        assert result is None
    else:
        assert result == PASSWORD and all(field.value == '' for field in entries)
    windows = [widget for widget in host.widgets if widget.title == 'Protected voice package password']
    assert len(windows) == 1 and windows[0].destroy_count == 1


def test_completed_inspection_from_replaced_library_is_not_adopted(view):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    host.frame.library = SimpleNamespace(root=host.root)
    host.finish()
    assert c.review is None and not c.rights.GetValue()
    assert [call[0] for call in host.calls] == ['inspect']


@pytest.mark.parametrize('closing', ['close', 'destroy'])
def test_already_adopted_protected_review_is_revoked_before_owner_cleanup(view, closing):
    host, c = view, view.controller
    inspect_ready(host)
    assert c.review is host.review
    generation = c.generation
    if closing == 'close':
        c.close()
    else:
        c.dialog.Destroy()
    assert c.closed and c.cancelled.is_set()
    assert c.review is None and c.removal_review is None
    assert c.generation == generation + 1
    assert not c._protected_current() and c._protected_cancelled()
    # These handlers must refuse on Python authority before any deleted control.
    assert c.inspect_protected() is False
    assert c.apply_protected() is False
    assert c.export_protected() is False
    assert [call[0] for call in host.calls] == ['inspect']
    c.close(destroying=True)
    assert c.generation == generation + 1


def test_deferred_native_destroy_revokes_a_result_adopted_before_its_event(view, monkeypatch):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    destroy_now, deferred = c.dialog.Destroy, []

    def request_destroy():
        # wx may defer the native destructor until its current callback returns.
        deferred.append(destroy_now)
        return True

    monkeypatch.setattr(c.dialog, 'Destroy', request_destroy)
    c.dialog.Destroy()
    assert not c.destroyed and not c.closed and len(deferred) == 1
    host.finish()
    assert c.review is host.review  # Faithfully expose the formerly missed order.
    generation = c.generation
    deferred.pop()()
    assert c.destroyed and c.closed and c.dialog.destroy_count == 1
    assert c.review is None and c.removal_review is None
    assert c.generation == generation + 1 and c.cancelled.is_set()
    assert c.apply_protected() is False
    assert [call[0] for call in host.calls] == ['inspect']


@pytest.mark.parametrize('closing', ['close', 'destroy'])
def test_completed_removal_approval_is_revoked_on_the_same_owner_close(view, closing):
    host, c = view, view.controller
    review = SimpleNamespace(settings_revision=1, summary=lambda: 'Synthetic removal review')
    main = threading.get_ident()

    def removal(profile, *, delete_files, cancelled):
        assert threading.get_ident() != main
        assert profile == 'fixture' and delete_files is False and not cancelled()
        return review

    host.lifecycle.review_removal = removal
    c.profile.ChangeValue('fixture')
    c.removal_mode.SetSelection(0)
    c.review_removal()
    host.finish()
    assert c.removal_review is review
    generation = c.generation
    if closing == 'close':
        c.close()
    else:
        c.dialog.Destroy()
    assert c.closed and c.review is None and c.removal_review is None
    assert c.generation == generation + 1 and c.cancelled.is_set()


def test_child_destroy_event_does_not_revoke_the_live_owner_review(view):
    host, c = view, view.controller
    inspect_ready(host)
    generation = c.generation
    skipped = []
    event = SimpleNamespace(GetId=lambda: c.profile.id, Skip=lambda: skipped.append(True),
        GetEventObject=lambda: pytest.fail('Do not query a dying child wrapper.'))
    c._destroyed(event)
    assert skipped == [True]
    assert not c.closed and not c.destroyed and not c.cancelled.is_set()
    assert c.generation == generation and c.review is host.review


def test_borrowed_host_refusal_clears_secret_without_starting_or_owning_another_executor(view):
    host, c = view, view.controller
    before = len(host.admissions)
    host.frame.run_job = lambda *_args, **_kwargs: None
    class Secret(str):
        pass
    value = Secret(PASSWORD)
    retained = weakref.ref(value)
    assert c._start_secret(lambda secret: pytest.fail('Refused work started.'),
        lambda result: pytest.fail('Refused work completed.'), value) is False
    del value
    gc.collect()
    assert retained() is None and not c.busy
    assert len(host.admissions) == before and len(host.pools) == 1
    assert host.pools[0].submit(lambda: 'same host').result(timeout=5) == 'same host'


@pytest.mark.parametrize('parent_current', [True, False])
def test_returned_commit_refreshes_only_live_parent_after_child_close(view, parent_current):
    host, c = view, view.controller
    began, release = threading.Event(), threading.Event()
    result = {'revision': 9}
    target = host.root / 'controlled-committed-result.json'
    before = len(host.refreshes)
    def work():
        began.set()
        assert release.wait(5)
        target.write_text(json.dumps(result), encoding='utf-8')
        return result
    try:
        assert c.start(work, lambda value: pytest.fail('Closed child received a GUI result.'),
                       committed=True)
        assert began.wait(5)
        c.close()
        host.authority['current'] = parent_current
    finally:
        release.set()
    host.finish()
    assert target.read_text(encoding='utf-8') == json.dumps(result)
    assert c.last_committed_result is result
    assert len(host.refreshes) == before + int(parent_current)
    assert all(value == host.main for value in host.refreshes)
    assert host.pools[0].submit(lambda: 17).result(timeout=5) == 17


def test_queued_same_path_replacement_cannot_call_service_or_touch_new_root(view):
    host, c = view, view.controller
    began, release = threading.Event(), threading.Event()
    blocker = host.pools[0].submit(lambda: (began.set(), release.wait(5)))
    assert began.wait(5)
    old = host.root.stat()
    moved = host.root.with_name('retained-original')
    before = len(host.calls)
    try:
        assert c.inspect_protected()
        host.root.rename(moved)
        host.root.mkdir()
        canary = host.root / 'canary.txt'
        canary.write_text('untouched replacement', encoding='utf-8')
        release.set()
        blocker.result(timeout=5)
        host.finish()
        assert len(host.calls) == before and c.review is None
        assert list(host.root.iterdir()) == [canary]
        assert canary.read_text(encoding='utf-8') == 'untouched replacement'
        assert (moved.stat().st_dev, moved.stat().st_ino) == (old.st_dev, old.st_ino)
    finally:
        release.set()
        blocker.result(timeout=5)
        if moved.exists():
            if (host.root / 'canary.txt').exists():
                (host.root / 'canary.txt').unlink()
            host.root.rmdir()
            moved.rename(host.root)


@pytest.mark.parametrize('handler', ['configure', 'inspect_package', 'list_versions',
    'review_version', 'apply', 'export_version', 'review_removal', 'remove_profile', 'create_reference'])
def test_every_ordinary_route_refuses_revoked_owner_before_native_control_reads(view, handler, monkeypatch):
    host, c = view, view.controller
    host.authority['current'] = False
    for control in (c.profile, c.version, c.models, c.backend, c.rights, c.removal_confirmation):
        for name in ('GetValue', 'GetStringSelection'):
            if hasattr(control, name):
                monkeypatch.setattr(control, name, lambda: pytest.fail('Stale route read a native control.'))
    before = len(host.admissions)
    getattr(c, handler)()
    assert len(host.admissions) == before and host.calls == []


def test_configure_adopts_returned_commit_without_second_cancellable_configuration_query(view):
    host, c = view, view.controller
    result = {'revision': 2}
    def configure(backend, models, reference_model, references, *, expected_revision, cancelled):
        assert threading.get_ident() != host.main and not cancelled()
        assert (backend, models, reference_model, references) == (
            'omnivoice', 'models', 'models/Holos', 'models/voices')
        assert expected_revision == 1
        host.calls.append(('configure',))
        return result
    host.manager.configure = configure
    host.manager.configuration = lambda **kwargs: pytest.fail('A commit cannot be relabelled by a second read.')
    c.configure()
    host.admissions[-1].result(timeout=5)
    c.stop()
    host.finish()
    assert c.revision == 2 and c.last_committed_result is result
    assert host.calls == [('configure',)]
    assert 'saved' in c.status.GetValue()
    assert not c.rights.GetValue() and not c.export_rights.GetValue()


@pytest.mark.parametrize('backend,changed,allowed', [
    ('holos', False, False), ('omnivoice', True, False), ('omnivoice', False, True)])
def test_reference_creation_requires_current_saved_omnivoice_capability(view, monkeypatch, backend, changed, allowed):
    from carr_library.ui import voice_reference
    host, c = view, view.controller
    c.backend.SetStringSelection(backend)
    c._reference_configuration = (backend, 'models', 'models/Holos', 'models/voices')
    if changed:
        c.references.ChangeValue('other-reference-folder')
    opened = []
    monkeypatch.setattr(voice_reference, 'show_reference_from_packages',
                        lambda owner: opened.append(owner))
    c.create_reference()
    assert opened == ([c] if allowed else [])
    assert host.calls == []


def test_register_manifest_keeps_default_no_and_captured_revision(view):
    host, c = view, view.controller
    registered = []
    host.manager.register_manifest = lambda path, **kwargs: registered.append((path, kwargs)) or {'revision': 2}
    host.wx.question_answer = host.wx.NO
    c.register_manifest()
    assert registered == []
    host.wx.question_answer = host.wx.YES
    c.register_manifest()
    host.admissions[-1].result(timeout=5)
    c.close()  # The commit settles, but no fresh load or approval may enter this child.
    host.finish()
    assert len(registered) == 1 and registered[0][0] == Path(host.wx.picker_path)
    assert registered[0][1]['expected_revision'] == 1
    assert c.last_committed_result == {'revision': 2} and c.review is None


@pytest.mark.parametrize('close_before_callback', [False, True])
def test_saved_settings_receipt_survives_cleanup_failure_and_child_close(view, close_before_callback):
    from carr_library.audio.voice_settings import VoiceManagementCommitted
    from carr_library.durable_state import atomic_json, read_json
    host, c = view, view.controller
    receipt = {'revision': 2, 'config': {'dordiai_manifest': 'reviewed-fixture'}}
    target = host.root / 'saved-settings.json'
    def committed():
        assert threading.get_ident() != host.main
        atomic_json(target, receipt)
        raise VoiceManagementCommitted(receipt, OSError('synthetic cleanup refusal'))
    before = len(host.refreshes)
    assert c.start(committed, lambda value: pytest.fail('A failed cleanup cannot dispatch normal success.'),
                   committed=True)
    with pytest.raises(VoiceManagementCommitted):
        host.admissions[-1].result(timeout=5)
    if close_before_callback:
        c.close()
    host.finish()
    assert read_json(target) == receipt
    assert c.last_committed_result == receipt and c.last_committed_result is not receipt
    assert c.review is None and c.removal_review is None
    assert len(host.refreshes) == before + 1
    assert all(value == host.main for value in host.refreshes)
    if not close_before_callback:
        assert 'settings were saved' in c.status.GetValue()
        assert not c.rights.GetValue() and not c.export_rights.GetValue()
    assert host.pools[0].submit(lambda: 19).result(timeout=5) == 19

"""Portable control-flow tests of the actual reference package dialog handlers."""
from concurrent.futures import Future
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import threading

import pytest

from carr_library.ui.voice_packages import VoicePackageDialog


class Control:
    def __init__(self, value=''):
        self.value, self.enabled = value, True
        self.focused = False
    def GetValue(self): return self.value
    def GetStringSelection(self): return self.value
    def GetSelection(self): return self.value
    def SetStringSelection(self, value): self.value = value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def Enable(self, value): self.enabled = value
    def SetInsertionPoint(self, value): pass
    def SetFocus(self): self.focused = True


def controller():
    value = object.__new__(VoicePackageDialog)
    value.closed, value.busy, value.review = False, False, None
    value.destroyed, value._window_id = False, 31
    value.generation = 0
    value.removal_review = None
    value.cancelled = threading.Event()
    for name in ('backend', 'models', 'reference_model', 'references', 'profile', 'version',
                 'supported', 'rights', 'configure_button', 'inspect_button', 'versions_button',
                 'review_button', 'apply_button', 'stop_button', 'status', 'export_rights',
                 'create_reference_button', 'protected_inspect_button',
                 'protected_export_button', 'protected_export_rights',
                 'export_button', 'removal_mode', 'removal_confirmation',
                 'removal_review_button', 'removal_apply_button'):
        setattr(value, name, Control())
    value.rights.value = value.export_rights.value = value.protected_export_rights.value = False
    value.removal_mode.value = 0
    value.revision = 4
    return value


def test_apply_requires_explicit_rights_and_captures_exact_review_before_worker():
    form = controller()
    form.review = object()
    selected = form.review
    starts, applied = [], []
    form.start = lambda action, complete: starts.append((action, complete))
    form.manager = SimpleNamespace(apply=lambda review, **kw: applied.append((review, kw)) or {'revision': 5})
    form.apply()
    assert starts == [] and 'permission' in form.status.value
    form.rights.value = True
    form.supported.value = ' uk, sk '
    form.apply()
    assert form.review is None and form.rights.value is False
    form.supported.value = 'en'
    form.rights.value = False
    action, complete = starts[0]
    complete(action())
    actual, kwargs = applied[0]
    assert actual is selected and kwargs['rights_approved'] is True
    assert kwargs['supported_languages'] == ('uk', 'sk')
    assert form.revision == 5 and 'have not been qualified' in form.status.value


def test_editing_selected_runtime_or_version_invalidates_review_and_consent():
    form = controller()
    form.review, form.rights.value = object(), True
    form.protected_export_rights.value = True
    skipped = []
    form.invalidate(SimpleNamespace(Skip=lambda: skipped.append(True)))
    assert form.review is None and form.rights.value is False
    assert form.apply_button.enabled is False and skipped == [True]
    assert form.generation == 1 and form.protected_export_rights.value is False
    assert form.protected_export_button.enabled is False


def test_loaded_configuration_and_new_review_never_preselect_consent_or_production_languages():
    form = controller()
    form.rights.value, form.supported.value = True, 'uk'
    form.loaded({'revision': 9, 'reference_engine': 'omnivoice', 'models': 'models',
                 'reference_model': 'models/OmniVoice', 'references': 'models/voices'})
    assert form.backend.value == 'omnivoice' and not form.rights.value
    review = SimpleNamespace(settings_revision=9, summary=lambda: 'Exact synthetic references')
    form.reviewed(review)
    assert form.review is review and not form.rights.value and form.supported.value == ''


def test_close_cancels_worker_and_discards_already_queued_result_without_touching_destroyed_controls():
    form = controller()
    future = Future()
    pending, completed, shutdown = [], [], []
    form.executor = SimpleNamespace(submit=lambda action: future,
                                    shutdown=lambda **kw: shutdown.append(kw))
    form.wx = SimpleNamespace(CallAfter=pending.append, ID_CLOSE=1)
    form.dialog = SimpleNamespace(IsModal=lambda: True, EndModal=lambda code: None)
    form.start(lambda: 'result', completed.append)
    future.set_result('result')
    assert len(pending) == 1
    form.close()
    pending.pop()()
    assert form.cancelled.is_set() and completed == []
    assert shutdown == [{'wait': False, 'cancel_futures': True}]


def test_configuration_captures_paths_before_async_worker_and_passes_cancellation():
    form = controller()
    form.backend.value = 'openvoice'
    form.models.value, form.reference_model.value, form.references.value = 'models', 'models/OpenVoice', 'models/voices'
    pending, calls = [], []
    form.start = lambda action, complete: pending.append(action)
    form.manager = SimpleNamespace(configure=lambda *args, **kwargs: calls.append((args, kwargs)),
                                   configuration=lambda **kwargs: {})
    form.configure()
    form.backend.value = 'omnivoice'
    form.models.value = '../outside'
    pending.pop()()
    args, kwargs = calls[0]
    assert args == ('openvoice', 'models', 'models/OpenVoice', 'models/voices')
    assert kwargs['expected_revision'] == 4 and kwargs['cancelled']() is False


def test_worker_failure_invalidates_review_and_preserves_explicit_revalidation_instructions():
    form = controller()
    future = Future()
    pending = []
    form.executor = SimpleNamespace(submit=lambda action: future)
    form.wx = SimpleNamespace(CallAfter=pending.append)
    form.review, form.rights.value = object(), True
    form.start(lambda: None, lambda value: None)
    future.set_exception(InterruptedError('Fixture cancellation'))
    pending.pop()()
    assert form.review is None and not form.rights.value and not form.busy
    assert 'review and revalidate' in form.status.value


@pytest.fixture
def native_probe():
    path = Path(__file__).with_name('native_voice_packages_smoke.py')
    spec = importlib.util.spec_from_file_location('native_voice_package_probe_review', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_probe_identifies_actual_fixture_through_another_path_spelling(tmp_path, native_probe):
    from carr_library.audio.model_voices import _run

    (tmp_path / 'spelling').mkdir()
    runtime = tmp_path / 'spelling' / '..' / 'runtime'
    expected = runtime / 'python/python.exe'
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b'INERT synthetic interpreter marker')
    selected = expected.resolve(strict=True)
    assert selected != expected  # The previous lexical assertion rejects this real file.
    command = [selected, '-I', '-B', '-u', '-c',
               'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())']
    adapted = native_probe._synthetic_child_command(command, runtime)
    assert adapted == [sys.executable, *command[1:]]
    assert _run(adapted, data=b'Synthetic bounded child', timeout=10) == b'Synthetic bounded child'


def test_native_probe_rejects_another_file_even_with_identical_bytes(tmp_path, native_probe):
    expected = tmp_path / 'python/python.exe'
    expected.parent.mkdir()
    expected.write_bytes(b'INERT marker')
    other = tmp_path / 'other.exe'
    other.write_bytes(expected.read_bytes())
    with pytest.raises(AssertionError, match='Unexpected synthetic interpreter'):
        native_probe._synthetic_child_command([other, '-I', '-B', '-u'], tmp_path)


@pytest.mark.parametrize('flags', [['-B', '-I', '-u'], ['-I', '-u', '-c'], ['-I', '-B', '-X']])
def test_native_probe_keeps_exact_isolation_flags(tmp_path, native_probe, flags):
    expected = tmp_path / 'python/python.exe'
    expected.parent.mkdir()
    expected.write_bytes(b'INERT marker')
    with pytest.raises(AssertionError, match='Unexpected synthetic child isolation flags'):
        native_probe._synthetic_child_command([expected, *flags], tmp_path)


def test_native_probe_missing_fixture_has_diagnostic_failure(tmp_path, native_probe):
    with pytest.raises(AssertionError, match='Cannot identify the synthetic interpreter'):
        native_probe._synthetic_child_command([tmp_path / 'missing.exe', '-I', '-B', '-u'], tmp_path)



def test_export_requires_separate_permission_and_captures_exact_review_and_destination(tmp_path):
    form = controller()
    review = SimpleNamespace(operation='activate', settings_revision=4)
    form.review = review
    starts, calls, destroyed = [], [], []
    form.start = lambda action, complete: starts.append((action, complete))
    target = tmp_path / 'private.dordiai-voice'
    form.lifecycle = SimpleNamespace(export=lambda *args, **kwargs:
        calls.append((args, kwargs)) or {'path': str(target), 'sha256': 'a' * 64})
    picker = SimpleNamespace(ShowModal=lambda: 1, GetPath=lambda: str(target),
                             Destroy=lambda: destroyed.append(True))
    form.wx = SimpleNamespace(FileDialog=lambda *args, **kwargs: picker, FD_SAVE=1, ID_OK=1)
    form.dialog = object()
    form.rights.value = True
    form.export_version()
    assert starts == [] and calls == [] and 'permission to export' in form.status.value
    form.export_rights.value = True
    form.export_version()
    assert destroyed == [True] and form.review is None and not form.export_rights.value
    form.review = SimpleNamespace(operation='activate')
    action, complete = starts.pop()
    complete(action())
    args, kwargs = calls[0]
    assert args == (review, target) and kwargs['export_rights_approved'] is True
    assert kwargs['cancelled']() is False and 'not encrypted' in form.status.value


def test_removal_review_captures_profile_and_mode_and_never_prefills_confirmation():
    form = controller()
    form.profile.value, form.removal_mode.value = ' fixture ', 1
    form.removal_confirmation.value = 'old confirmation'
    starts, calls = [], []
    reviewed = SimpleNamespace(settings_revision=8, summary=lambda: 'Exact deletion review')
    form.lifecycle = SimpleNamespace(review_removal=lambda *args, **kwargs:
        calls.append((args, kwargs)) or reviewed)
    form.start = lambda action, complete: starts.append((action, complete))
    form.review_removal()
    form.profile.value, form.removal_mode.value = 'another', 0
    action, complete = starts.pop()
    complete(action())
    args, kwargs = calls[0]
    assert args == ('fixture',) and kwargs['delete_files'] is True
    assert form.removal_review is reviewed and form.removal_confirmation.value == ''
    assert form.review is None and form.revision == 8


def test_exact_removal_confirmation_is_consumed_before_worker_and_cannot_switch_identity():
    form = controller()
    review = SimpleNamespace(confirmation='DELETE fixture')
    form.removal_review = review
    starts, calls = [], []
    form.start = lambda action, complete: starts.append((action, complete))
    form.lifecycle = SimpleNamespace(remove=lambda *args, **kwargs:
        calls.append((args, kwargs)) or {'revision': 9})
    form.removal_confirmation.value = 'DELETE another'
    form.remove_profile()
    assert starts == [] and calls == []
    form.removal_confirmation.value = 'DELETE fixture'
    form.remove_profile()
    assert form.removal_review is None and form.removal_confirmation.value == ''
    form.profile.value = 'another'
    form.removal_confirmation.value = 'DELETE another'
    action, complete = starts.pop()
    complete(action())
    args, kwargs = calls[0]
    assert args == (review,) and kwargs['confirmation'] == 'DELETE fixture'
    assert form.revision == 9 and 'permission is revoked' in form.status.value


def test_changed_selection_invalidates_every_lifecycle_authorization():
    form = controller()
    form.review, form.removal_review = object(), object()
    form.rights.value = form.export_rights.value = True
    form.removal_confirmation.value = 'DELETE fixture'
    form.invalidate()
    assert form.review is form.removal_review is None
    assert not form.rights.value and not form.export_rights.value
    assert form.removal_confirmation.value == ''
    assert not form.apply_button.enabled and not form.export_button.enabled
    assert not form.removal_apply_button.enabled


def test_current_worker_reports_failure_to_borrowed_dialog_only_while_owner_is_alive():
    form = controller()
    future = Future()
    pending, errors = [], []
    form.executor = SimpleNamespace(submit=lambda action: future)
    form.wx = SimpleNamespace(CallAfter=pending.append)
    form.start(lambda: None, lambda result: None, on_error=errors.append)
    failure = ValueError('synthetic reviewed reference error')
    future.set_exception(failure)
    pending.pop()()
    assert errors == [failure] and not form.busy and form.review is None


def test_reference_creator_button_borrows_current_package_dialog_only_when_idle(monkeypatch):
    from carr_library.ui import voice_reference
    form = controller()
    seen = []
    monkeypatch.setattr(voice_reference, 'show_reference_from_packages',
                        lambda owner: seen.append(owner) or 'reviewed result')
    assert form.create_reference() == 'reviewed result'
    assert seen == [form]
    form.busy = True
    form.enable()
    assert not form.create_reference_button.enabled and form.create_reference() is None
    form.busy, form.closed = False, True
    assert form.create_reference() is None and seen == [form]


def test_direct_package_owner_destruction_revokes_queued_completion_without_native_access():
    form = controller()
    future, pending, completed, shutdown = Future(), [], [], []
    form.executor = SimpleNamespace(submit=lambda action: future,
        shutdown=lambda **kwargs: shutdown.append(kwargs))
    form.wx = SimpleNamespace(CallAfter=pending.append)
    form.start(lambda: 'worker result', completed.append)
    future.set_result('worker result')
    skipped = []
    form._destroyed(SimpleNamespace(GetId=lambda: 99, Skip=lambda: skipped.append(99)))
    assert not form.closed and shutdown == []
    form._destroyed(SimpleNamespace(GetId=lambda: 31, Skip=lambda: skipped.append(31)))
    pending.pop()()
    assert form.closed and form.destroyed and form.cancelled.is_set()
    assert completed == [] and skipped == [99, 31]
    assert shutdown == [{'wait': False, 'cancel_futures': True}]

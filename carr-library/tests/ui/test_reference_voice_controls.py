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
    def SetStringSelection(self, value): self.value = value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def Enable(self, value): self.enabled = value
    def SetInsertionPoint(self, value): pass
    def SetFocus(self): self.focused = True


def controller():
    value = object.__new__(VoicePackageDialog)
    value.closed, value.busy, value.review = False, False, None
    value.cancelled = threading.Event()
    for name in ('backend', 'models', 'reference_model', 'references', 'profile', 'version',
                 'supported', 'rights', 'configure_button', 'inspect_button', 'versions_button',
                 'review_button', 'apply_button', 'stop_button', 'status'):
        setattr(value, name, Control())
    value.rights.value = False
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
    skipped = []
    form.invalidate(SimpleNamespace(Skip=lambda: skipped.append(True)))
    assert form.review is None and form.rights.value is False
    assert form.apply_button.enabled is False and skipped == [True]


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


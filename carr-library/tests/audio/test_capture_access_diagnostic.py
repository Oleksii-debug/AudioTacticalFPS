"""Read-only diagnostic boundaries; no native capture acceptance is claimed."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio.recording import RecordingDevice
from carr_library.durable_state import atomic_json

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'carr_capture_access_diagnostic_test', ROOT / 'scripts/windows_capture_access_diagnostic.py')
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


def device(alternative='@device_cm_exact_reviewed_input', *, name=None):
    return RecordingDevice(sha256(alternative.encode()).hexdigest(),
        diagnostic.FRIENDLY_NAME if name is None else name, alternative)


def failed_capture(selected):
    return {'status': 'FAILED', 'actual_directshow_capture': False,
            'actual_mci_output': False,
            'selected_device': {'id': selected.id, 'name': selected.name}}


def native_report(selected, *, status='DIAGNOSED'):
    return {'status': status, 'scope': 'selected moniker only; no recording started',
            'selected_moniker': selected.alternative,
            'selected_friendly_name': selected.name,
            'physical_microphone': 'NOT_RUN', 'policy_changes': False, 'fallback': False,
            'wave_in_id': 0,
            'wave_in_trials': [{'operation': 'open-without-buffers', 'result': 1}],
            'wasapi': {'capture_started': False, 'buffers_requested': False,
                       'property_changes': False, 'device_fallback': False,
                       'shared_mode': True, 'stream_initialized': False,
                       'calls': [{'operation': 'IAudioClient.Initialize.shared',
                                  'hresult': '0x80070005'}]}}


def evaluation(monkeypatch):
    monkeypatch.setattr(diagnostic.sys, 'platform', 'win32')
    for key, value in {
        'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push',
        'GITHUB_REPOSITORY': diagnostic.REPOSITORY, 'GITHUB_REF': diagnostic.BRANCH,
        'GITHUB_SHA': 'a' * 40,
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('changed,value', [
    ('platform', 'linux'),
    ('GITHUB_ACTIONS', 'false'),
    ('GITHUB_EVENT_NAME', 'pull_request'),
    ('GITHUB_REPOSITORY', 'another/repository'),
    ('GITHUB_REF', 'refs/heads/main'),
    ('GITHUB_SHA', 'not-an-exact-sha'),
])
def test_foreign_context_is_refused_before_any_parent_diagnostic_work(
    tmp_path, monkeypatch, changed, value
):
    evaluation(monkeypatch)
    if changed == 'platform':
        monkeypatch.setattr(diagnostic.sys, 'platform', value)
    else:
        monkeypatch.setenv(changed, value)
    monkeypatch.setattr(diagnostic, '_access_settings',
                        lambda: pytest.fail('Foreign context read registry settings.'))
    target = tmp_path / 'uncreated' / 'report.json'
    with pytest.raises(RuntimeError, match='exact disposable'):
        diagnostic.main(['--ffmpeg', str(tmp_path / 'ffmpeg.exe'),
            '--capture-report', str(tmp_path / 'capture.json'), '--report', str(target)])
    assert not target.parent.exists()


def test_exact_evaluation_context_passes_only_the_guard(monkeypatch):
    evaluation(monkeypatch)
    diagnostic._evaluation_guard()


@pytest.mark.parametrize('change', [
    'not-failed', 'already-captured', 'missing-selection', 'foreign-name',
    'bad-identity', 'extra-selection-authority',
])
def test_original_failed_receipt_must_name_the_explicit_reviewed_device(change):
    value = failed_capture(device())
    if change == 'not-failed':
        value['status'] = 'PASS'
    elif change == 'already-captured':
        value['actual_directshow_capture'] = True
    elif change == 'missing-selection':
        del value['selected_device']
    elif change == 'foreign-name':
        value['selected_device']['name'] = 'An actual microphone must never be substituted'
    elif change == 'bad-identity':
        value['selected_device']['id'] = 'default'
    else:
        value['selected_device']['fallback'] = True
    with pytest.raises(ValueError, match='failed capture|reviewed virtual'):
        diagnostic._original_selection(value)


@pytest.mark.parametrize('change', ['removed', 'reordered', 'duplicate-id',
    'duplicate-name', 'wrong-name', 'changed-moniker', 'too-many'])
def test_fresh_selection_never_substitutes_a_device(change):
    selected = device()
    other = device('@device_cm_unselected', name='Unselected input')
    original = diagnostic._original_selection(failed_capture(selected))
    inventory = [selected, other]
    if change == 'removed':
        inventory = [other]
    elif change == 'reordered':
        inventory = [other, selected]
        assert diagnostic._select(inventory, original) is selected
        return
    elif change == 'duplicate-id':
        inventory.append(selected)
    elif change == 'duplicate-name':
        inventory.append(device('@device_cm_second_same_name'))
    elif change == 'wrong-name':
        inventory = [SimpleNamespace(id=selected.id, name='Different source',
                                     alternative=selected.alternative)]
    elif change == 'changed-moniker':
        inventory = [SimpleNamespace(id=selected.id, name=selected.name,
                                     alternative='@device_cm_changed_identity')]
    else:
        inventory = [other] * 64 + [selected]
    with pytest.raises(ValueError, match='inventory|unavailable|identity'):
        diagnostic._select(inventory, original)


@pytest.mark.parametrize('moniker,name', [
    ('default', diagnostic.FRIENDLY_NAME),
    ('@device_cm_' + 'x' * 2048, diagnostic.FRIENDLY_NAME),
    ('@device_cm_with\x00nul', diagnostic.FRIENDLY_NAME),
    ('@device_cm_exact', 'An unreviewed microphone'),
], ids=['not-moniker', 'over-bound', 'nul', 'unreviewed-input'])
def test_private_child_refuses_bad_selection_before_loading_native_libraries(
    monkeypatch, moniker, name
):
    import builtins
    original = builtins.__import__
    def checked(module, *args, **kwargs):
        if module == 'comtypes' or module.startswith('comtypes.'):
            pytest.fail('Invalid selection reached a native library.')
        return original(module, *args, **kwargs)
    monkeypatch.setattr(diagnostic, '_evaluation_guard', lambda: None)
    monkeypatch.setattr(builtins, '__import__', checked)
    with pytest.raises(ValueError, match='exact selected'):
        diagnostic._native_open(moniker, name)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    selected = device()
    receipt = tmp_path / 'failed-capture.json'
    atomic_json(receipt, failed_capture(selected))
    ffmpeg = tmp_path / 'reviewed-ffmpeg.exe'
    ffmpeg.write_bytes(b'Explicit non-executable tool fixture; discovery is modeled here.')
    report = tmp_path / 'recording-access.json'
    monkeypatch.setattr(diagnostic, '_evaluation_guard', lambda: None)
    monkeypatch.setattr(diagnostic, '_access_settings',
        lambda: {'status': 'DIAGNOSED', 'registry_writes': False,
                 'consent_changes': False, 'settings': []})
    monkeypatch.setenv('GITHUB_SHA', 'a' * 40)
    roots = []
    class Inventory:
        def __init__(self, root, *, ffmpeg):
            roots.append(Path(root))
            assert ffmpeg == str(prepared_ffmpeg)
        def devices(self):
            return (selected,)
        def capture(self, *args, **kwargs):
            pytest.fail('The diagnostic must never capture samples.')
    prepared_ffmpeg = ffmpeg
    monkeypatch.setattr('carr_library.audio.recording.RecordingService', Inventory)
    args = SimpleNamespace(ffmpeg=str(ffmpeg), capture_report=receipt, report=report)
    return args, selected, roots


@pytest.mark.parametrize('exit_code,status', [(0, 'DIAGNOSED'), (1, 'FAILED')])
def test_real_owned_child_keeps_actual_failure_observations_and_finite_budget(
    prepared, monkeypatch, exit_code, status
):
    import carr_library.audio.process as process
    args, selected, roots = prepared
    value = native_report(selected, status=status)
    script = 'import sys; print(' + repr(json.dumps(value)) + '); raise SystemExit(' + str(exit_code) + ')'
    monkeypatch.setattr(diagnostic, '_native_command',
        lambda selected: [sys.executable, '-I', '-B', '-c', script])
    actual = process.run_process
    calls = []
    def observed(command, **kwargs):
        calls.append((command, kwargs))
        return actual(command, **kwargs)
    monkeypatch.setattr(process, 'run_process', observed)
    report = diagnostic.run(args, diagnostic._new_report())
    assert len(calls) == 1
    _, options = calls[0]
    assert options['owned_tree'] is True and options['allow_failure'] is True
    assert options['budget'].timeout == 18 and options['budget'].threads == 1
    assert options['budget'].max_log_bytes == diagnostic.MAX_NATIVE_BYTES
    assert report['status'] == status and report['application_acceptance'] == 'NOT_ASSESSED'
    assert report['native_child']['exit_code'] == exit_code
    assert report['native_input_open'] == value
    assert report['native_input_open']['wave_in_trials'][0]['result'] == 1
    assert report['native_input_open']['wasapi']['calls'][0]['hresult'] == '0x80070005'
    assert report['capture_started'] is False and report['sample_buffers_requested'] is False
    assert len(roots) == 1 and not roots[0].parent.exists()
    assert json.loads(args.capture_report.read_text()) == failed_capture(selected)


@pytest.mark.parametrize('where', ['discovery', 'native-child'])
@pytest.mark.parametrize('masked', [False, True])
def test_pending_native_owners_retain_the_exact_private_stage_until_both_are_quiet(
    prepared, monkeypatch, where, masked
):
    args, selected, roots = prepared
    release = [threading.Event(), threading.Event()]
    owners = [CleanupOwner(event.is_set) for event in release]
    first = AudioCleanupPending('First owned native resource', cleanup_owner=owners[0])
    first.__cause__ = AudioCleanupPending('Second owned native resource', cleanup_owner=owners[1])

    def fail(*args, **kwargs):
        if masked:
            raise OSError('Original masked native failure') from first
        raise first

    if where == 'native-child':
        monkeypatch.setattr('carr_library.audio.process.run_process', fail)
    else:
        class PendingInventory:
            def __init__(self, root, **kwargs):
                roots.append(Path(root))
            devices = fail
        monkeypatch.setattr('carr_library.audio.recording.RecordingService', PendingInventory)
    expected = OSError if masked else AudioCleanupPending
    try:
        with pytest.raises(expected):
            diagnostic.run(args, diagnostic._new_report())
        assert len(roots) == 1 and roots[0].is_dir()
        private = roots[0].parent
        release[0].set()
        assert owners[0].close() is True
        assert private.is_dir() and not owners[1].quiescent
        release[1].set()
        assert owners[1].close() is True
        assert not private.exists()
    finally:
        for event, owner in zip(release, owners):
            event.set()
            owner.close()


def test_changed_original_receipt_is_refused_before_native_open(prepared, monkeypatch):
    args, selected, roots = prepared
    class Changed:
        def __init__(self, root, **kwargs):
            roots.append(Path(root))
        def devices(self):
            value = failed_capture(selected)
            value['selected_device'] = {'id': '0' * 64, 'name': selected.name}
            atomic_json(args.capture_report, value)
            return (selected,)
    monkeypatch.setattr('carr_library.audio.recording.RecordingService', Changed)
    monkeypatch.setattr('carr_library.audio.process.run_process',
                        lambda *a, **k: pytest.fail('Changed receipt reached native open.'))
    with pytest.raises(ValueError, match='changed before native'):
        diagnostic.run(args, diagnostic._new_report())
    assert len(roots) == 1 and not roots[0].parent.exists()


@pytest.mark.parametrize('bad', ['duplicate', 'nonfinite', 'foreign-moniker',
    'wrong-exit', 'empty', 'oversized', 'capture-claim', 'wasapi-capture',
    'wasapi-buffers', 'wasapi-properties', 'wasapi-fallback', 'wasapi-type', 'wasapi-missing'])
def test_child_result_cannot_publish_ambiguous_or_different_evidence(bad):
    selected = device()
    value = native_report(selected)
    raw = json.dumps(value).encode()
    code = 0
    if bad == 'duplicate':
        raw = b'{"status":"DIAGNOSED","status":"FAILED"}'
    elif bad == 'nonfinite':
        raw = b'{"value":NaN}'
    elif bad == 'foreign-moniker':
        value['selected_moniker'] += '_another'
        raw = json.dumps(value).encode()
    elif bad == 'wrong-exit':
        code = 1
    elif bad == 'empty':
        raw = b''
    elif bad == 'oversized':
        raw = b' ' * (diagnostic.MAX_NATIVE_BYTES + 1)
    elif bad == 'capture-claim':
        value['physical_microphone'] = 'PASS'
        raw = json.dumps(value).encode()
    else:
        if bad == 'wasapi-type':
            value['wasapi'] = []
        elif bad == 'wasapi-missing':
            del value['wasapi']['property_changes']
        else:
            key = {'wasapi-capture': 'capture_started', 'wasapi-buffers': 'buffers_requested',
                   'wasapi-properties': 'property_changes', 'wasapi-fallback': 'device_fallback'}[bad]
            value['wasapi'][key] = True
        raw = json.dumps(value).encode()
    with pytest.raises(ValueError):
        diagnostic._native_result(raw, code, selected)


def test_fixed_registry_reads_are_read_only_and_unknown_content_is_redacted(monkeypatch):
    import ctypes
    calls, closed = [], []
    denied = OSError(5, 'Denied')
    denied.winerror = 5
    values = iter([('Deny', 1), ('private-unrecognized-content', 1),
                   (0, 4), FileNotFoundError(), denied] + [(1, 4)] * 5)
    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
    def opened(hive, path, reserved, access):
        calls.append((hive, path, reserved, access))
        return Key()
    def queried(handle, name):
        result = next(values)
        if isinstance(result, BaseException):
            raise result
        return result
    class Session:
        def __call__(self, pid, pointer):
            pointer._obj.value = 7
            return 1
    registry = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2,
        KEY_READ=0x20019, KEY_WOW64_64KEY=0x100,
        OpenKey=opened, QueryValueEx=queried)
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k:
                        SimpleNamespace(ProcessIdToSessionId=Session()), raising=False)
    monkeypatch.setattr(sys, 'getwindowsversion', lambda: (10, 0, 20348, 0, ''), raising=False)
    monkeypatch.setattr(diagnostic, '_evaluation_guard', lambda: None)
    monkeypatch.setenv('SESSIONNAME', 'Services')
    report = diagnostic._access_settings()
    assert len(calls) == len(closed) == 10
    assert all(reserved == 0 and access == registry.KEY_READ | registry.KEY_WOW64_64KEY
               for _, _, reserved, access in calls)
    assert set(hive for hive, *_ in calls) == {1, 2}
    assert report['settings'][0]['value'] == 'Deny'
    assert report['settings'][1]['value'] == 'OTHER_REDACTED'
    assert report['settings'][2]['value'] == 0
    assert report['settings'][3]['state'] == 'NOT_CONFIGURED_AT_THIS_KEY'
    assert report['settings'][4]['state'] == 'READ_FAILED'
    assert 'private-unrecognized-content' not in json.dumps(report)
    assert report['process_session_id'] == 7 and report['session_name'] == 'Services'
    assert report['registry_writes'] is False and report['consent_changes'] is False
    assert report['device_access_attempt'] is False


def test_main_retains_failed_child_report_and_nonzero_result(prepared, monkeypatch):
    args, selected, roots = prepared
    payload = json.dumps(native_report(selected, status='FAILED')).encode()
    monkeypatch.setattr('carr_library.audio.process.run_process',
        lambda command, **kwargs: subprocess.CompletedProcess(command, 23, payload, b'bounded native error'))
    result = diagnostic.main(['--ffmpeg', args.ffmpeg,
        '--capture-report', str(args.capture_report), '--report', str(args.report)])
    saved = json.loads(args.report.read_text())
    assert result == 1 and saved['status'] == 'FAILED'
    assert saved['native_child']['exit_code'] == 23
    assert saved['application_acceptance'] == 'NOT_ASSESSED'
    assert saved['capture_started'] is False and saved['device_fallback'] is False
    assert not roots[0].parent.exists()


@pytest.mark.parametrize('masked', [False, True])
def test_main_reports_pending_cleanup_without_releasing_the_same_stage(
    prepared, monkeypatch, masked
):
    args, selected, roots = prepared
    release, waiting = threading.Event(), threading.Event()
    def finish():
        waiting.set()
        return release.is_set()
    owner = CleanupOwner(finish)
    pending = AudioCleanupPending('The diagnostic native child is not yet quiet.',
                                  cleanup_owner=owner)
    def launch(*args, **kwargs):
        if masked:
            raise OSError('The same pending owner is causal.') from pending
        raise pending
    monkeypatch.setattr('carr_library.audio.process.run_process', launch)
    arguments = ['--ffmpeg', args.ffmpeg, '--capture-report', str(args.capture_report),
                 '--report', str(args.report)]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(diagnostic.main, arguments)
        try:
            assert waiting.wait(5)
            assert not future.done()
            value = json.loads(args.report.read_text())
            assert value['status'] == 'FAILED' and value['cleanup_pending'] is True
            assert value['cleanup_owner_count'] == 1
            assert value['application_acceptance'] == 'NOT_ASSESSED'
            assert len(roots) == 1 and roots[0].is_dir() and not owner.quiescent
        finally:
            release.set()
        with pytest.raises(OSError if masked else AudioCleanupPending) as caught:
            future.result(5)
        if masked:
            assert caught.value.__cause__ is pending
        else:
            assert caught.value is pending
    assert owner.quiescent and not roots[0].parent.exists()
    final = json.loads(args.report.read_text())
    assert final['status'] == 'FAILED'
    assert final['cleanup_pending'] is False and final['cleanup_quiescent'] is True


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('phase', ['initial-status', 'close'])
def test_existing_owner_drain_finishes_both_before_delivering_interruption(signal_type, phase):
    signal = signal_type('Purpose-written owner interruption')
    raised, events = [False], []
    def first_close():
        events.append('first')
        if phase == 'close' and not raised[0]:
            raised[0] = True
            raise signal
        return True
    def second_close():
        events.append('second')
        return True
    first, second = CleanupOwner(first_close), CleanupOwner(second_close)
    if phase == 'initial-status':
        actual = first.quiescent_event
        class Status:
            def is_set(self):
                if not raised[0]:
                    raised[0] = True
                    raise signal
                return actual.is_set()
            def set(self):
                return actual.set()
            def wait(self, timeout=None):
                return actual.wait(timeout)
        first.quiescent_event = Status()
    observed = diagnostic._wait_for_pending((first, second))
    assert observed is signal and raised[0]
    assert first.quiescent and second.quiescent
    assert events[-1] == 'second'
    assert events.count('first') == (2 if phase == 'close' else 1)


@pytest.mark.parametrize('signal_type', [KeyboardInterrupt, SystemExit])
def test_early_report_interruption_cannot_release_either_actual_causal_owner(
    prepared, monkeypatch, signal_type
):
    args, selected, roots = prepared
    events = []
    first = CleanupOwner(lambda: events.append('first') or True)
    second = CleanupOwner(lambda: events.append('second') or True)
    pending = AudioCleanupPending('First actual native owner', cleanup_owner=first)
    pending.__cause__ = AudioCleanupPending('Second actual native owner', cleanup_owner=second)
    def launch(*args, **kwargs):
        raise pending
    monkeypatch.setattr('carr_library.audio.process.run_process', launch)
    signal, raised = signal_type('Early receipt interruption'), [False]
    original = diagnostic._save_report
    def interrupted(path, value):
        if not raised[0]:
            assert value['status'] == 'FAILED' and value['cleanup_pending'] is True
            assert roots[0].is_dir() and not first.quiescent and not second.quiescent
            raised[0] = True
            raise signal
        return original(path, value)
    monkeypatch.setattr(diagnostic, '_save_report', interrupted)
    with pytest.raises(signal_type) as caught:
        diagnostic.main(['--ffmpeg', args.ffmpeg, '--capture-report', str(args.capture_report),
                         '--report', str(args.report)])
    assert caught.value is signal and caught.value.__cause__ is pending
    assert events == ['first', 'second'] and first.quiescent and second.quiescent
    assert len(roots) == 1 and not roots[0].parent.exists()
    value = json.loads(args.report.read_text())
    assert value['status'] == 'FAILED' and value['cleanup_quiescent'] is True
    assert value['cleanup_pending'] is False


def test_actual_diagnostic_python_does_not_exit_while_its_owned_child_is_live(tmp_path):
    import carr_library
    from carr_library.audio.process import owned_process
    work = tmp_path / 'actual diagnostic ownership'
    work.mkdir()
    release, barrier = work / 'release', work / 'synchronous-barrier'
    parent = work / 'diagnostic_parent.py'
    parent.write_text(r'''
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading

package_parent, diagnostic_path, work = map(Path, sys.argv[1:])
sys.path.insert(0, str(package_parent))
from carr_library.audio import cleanup
from carr_library.audio.process import owned_process, _stop_owned
spec = importlib.util.spec_from_file_location('actual_diagnostic_fixture', diagnostic_path)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)
diagnostic._evaluation_guard = lambda: None  # No native device or Windows claim.
main_thread = threading.get_ident()
release = work / 'release'
context = owned_process([sys.executable, '-I', '-B', '-c',
    'import time; time.sleep(60)'], stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
worker = context.__enter__()
closed = [False]
proof = {'worker_pid': worker.pid, 'worker_initial_returncode': worker.poll()}
pending = [None]
stage = [None]
notifications_done = threading.Event()

def failing_run(args, report):
    with cleanup.retained_temporary_directory(prefix='actual-native-owner-', dir=work) as temporary:
        stage[0] = Path(temporary)
        (stage[0] / 'retained.txt').write_bytes(b'Actual staging owned by the pending worker')
        report['fixture_stage'] = str(stage[0])
        report['fixture_worker_pid'] = worker.pid
        def finish():
            if threading.get_ident() == main_thread:
                (work / 'synchronous-barrier').write_bytes(b'Actual main entered its wait')
            if not release.exists():
                return False
            if _stop_owned(worker) is not True:
                return False
            context.__exit__(None, None, None)
            closed[0] = True
            proof['worker_final_returncode'] = worker.poll()
            return True
        pending[0] = cleanup.pending_cleanup(finish, message='Actual diagnostic child remains owned.')
        raise pending[0]

def failing_with_notification(args, report):
    try:
        return failing_run(args, report)
    finally:
        # The retained-stage callback has been registered while unwinding
        # failing_run. This later callback acknowledges notification completion,
        # which is deliberately distinct from native quiescence.
        if pending[0] is not None:
            pending[0].cleanup_owner.add_quiescent_callback(notifications_done.set)

diagnostic.run = failing_with_notification
try:
    try:
        diagnostic.main(['--ffmpeg', 'explicit-unused-fixture',
            '--capture-report', str(work / 'unused-capture.json'),
            '--report', str(work / 'recording-access.json')])
    except cleanup.AudioCleanupPending as error:
        proof['same_error'] = error is pending[0]
        proof['owner_quiet'] = error.cleanup_owner.quiescent
        proof['stage_notification_completed'] = notifications_done.wait(5)
        if not proof['stage_notification_completed']:
            raise AssertionError('The actual retained-stage notification did not finish.')
        proof['stage_removed'] = stage[0] is not None and not stage[0].exists()
        (work / 'entry-returned.json').write_text(json.dumps(proof), encoding='utf-8')
    else:
        raise AssertionError('The original diagnostic failure was swallowed.')
finally:
    release.write_bytes(b'Always release the fixture worker')
    if not closed[0]:
        context.__exit__(None, None, None)
''', encoding='utf-8')
    package_parent = Path(carr_library.__file__).resolve().parent.parent
    with (work / 'process.log').open('wb') as output:
        with owned_process([sys.executable, '-I', '-B', str(parent), str(package_parent),
                str(Path(diagnostic.__file__).resolve()), str(work)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output) as child:
            try:
                deadline = time.monotonic() + 15
                while not barrier.exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail('The actual diagnostic parent did not remain in its owner barrier.')
                    time.sleep(.02)
                assert child.poll() is None
                assert not (work / 'entry-returned.json').exists()
                early = json.loads((work / 'recording-access.json').read_text())
                assert early['status'] == 'FAILED' and early['cleanup_pending'] is True
                assert early['application_acceptance'] == 'NOT_ASSESSED'
                assert Path(early['fixture_stage']).is_dir()
                assert early['fixture_worker_pid'] > 0
            finally:
                release.write_bytes(b'Release the same actual child')
            assert child.wait(timeout=15) == 0
    proof = json.loads((work / 'entry-returned.json').read_text())
    assert proof['worker_pid'] > 0 and proof['worker_initial_returncode'] is None
    assert proof['worker_final_returncode'] is not None
    assert proof['same_error'] and proof['owner_quiet'] and proof['stage_removed']
    assert proof['stage_notification_completed'] is True
    final = json.loads((work / 'recording-access.json').read_text())
    assert final['status'] == 'FAILED' and final['cleanup_pending'] is False
    assert final['cleanup_quiescent'] is True

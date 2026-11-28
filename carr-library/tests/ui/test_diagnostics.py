"""Report privacy, real isolated self-test and exact create-only publication."""
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.diagnostics import CHECKS, PACKAGES, collect_diagnostics, redacted_report
from carr_library.ui.diagnostics_dialog import DiagnosticsActions, DiagnosticsPreview


def good_report(**extra):
    return redacted_report({'status': 'PASS', 'checks': list(CHECKS), **extra},
                           {name: '1.2.3' for name in PACKAGES})


def frame(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(exist_ok=True)
    return SimpleNamespace(library=SimpleNamespace(root=root), _closing=False,
                           working=False, busy=False, cancel_event=Event(),
                           report=Mock(), run_job=Mock())


def test_allowlist_omits_raw_logs_paths_contents_accounts_and_secrets():
    secrets = [
        r'C:\Users\PrivatePerson\SecretBook.docx',
        '/home/private/.credentials/token',
        'sk-private-test-token',
        'https://private.example/v1?api_key=secret',
        'Exact private quotation from the user book.',
        'Private workspace name',
    ]
    result = {'status': 'PASS', 'checks': [*CHECKS, *secrets], 'error': secrets,
              'logs': secrets, 'environment': {'KEY': secrets[2]}, 'library': secrets[5]}
    versions = {name: '1.2.3' for name in PACKAGES}
    versions['unrequested-package'] = secrets[0]
    versions['numpy'] = secrets[2]
    payload = redacted_report(result, versions)
    text = payload.decode('utf-8')
    assert all(secret not in text for secret in secrets)
    document = json.loads(payload)
    assert document['packages']['numpy'] == 'UNAVAILABLE'
    assert set(document['packages']) == set(PACKAGES)
    assert document['installation_check']['status'] == 'PASS'
    assert document['model_inference'] == document['windows_nvda'] == 'NOT_RUN'
    assert [row['name'] for row in document['installation_check']['checks']] == list(CHECKS)


@pytest.mark.parametrize('result', [None, {'status': 'PASS'}, {'status': 'PASS', 'checks': []},
                                    {'status': 'PASS', 'checks': list(CHECKS[:-1])},
                                    {'status': 'FAILED', 'checks': list(CHECKS)}])
def test_partial_or_malformed_self_test_never_claims_success(result):
    report = json.loads(redacted_report(result, {}))
    assert report['installation_check']['status'] == 'FAILED'
    assert all(row['status'] == 'NOT_RUN' for row in report['installation_check']['checks'])


@pytest.mark.parametrize('failure', [OSError, ImportError, RuntimeError])
def test_failed_self_test_exports_safe_category_without_exception_text(monkeypatch, failure):
    def fail():
        raise failure('/private/SecretBook and credential=sk-secret')
    monkeypatch.setattr('carr_library.__main__.self_test', fail)
    report = collect_diagnostics()
    assert b'SecretBook' not in report and b'sk-secret' not in report and b'/private/' not in report
    assert json.loads(report)['installation_check']['status'] == 'FAILED'


def test_real_isolated_self_test_is_used_without_user_library_input():
    report = json.loads(collect_diagnostics())
    assert report['installation_check']['status'] == 'PASS'
    assert len(report['installation_check']['checks']) == 9
    assert report['native_gui'] == 'NOT_RUN'


def test_cancel_before_or_after_synthetic_check_never_publishes(monkeypatch):
    called = []
    stop = Event()
    def check():
        called.append(True)
        stop.set()
        return {'status': 'PASS', 'checks': list(CHECKS)}
    monkeypatch.setattr('carr_library.__main__.self_test', check)
    with pytest.raises(InterruptedError):
        collect_diagnostics(cancelled=lambda: True)
    assert called == []
    with pytest.raises(InterruptedError):
        collect_diagnostics(cancelled=stop.is_set)
    assert called == [True]


def test_exact_preview_export_uses_shared_create_only_writer(tmp_path):
    host = frame(tmp_path)
    payload = good_report()
    preview = DiagnosticsPreview(host, payload)
    target = tmp_path / 'report.json'
    evidence = preview.export(target)
    assert target.read_bytes() == preview.text.encode('utf-8') == payload
    assert evidence['bytes'] == len(payload)
    from hashlib import sha256
    assert evidence['sha256'] == sha256(payload).hexdigest()
    with pytest.raises(FileExistsError):
        preview.export(target)
    assert target.read_bytes() == payload
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('change', ['library', 'root', 'closed', 'closing'])
def test_changed_library_or_closed_preview_cannot_export(tmp_path, change):
    host = frame(tmp_path)
    preview = DiagnosticsPreview(host, good_report())
    if change == 'library':
        host.library = SimpleNamespace(root=host.library.root)
    elif change == 'root':
        host.library.root = tmp_path / 'other'
    elif change == 'closed':
        preview.close()
    else:
        host._closing = True
    target = tmp_path / 'report.json'
    with pytest.raises(ValueError, match='changed'):
        preview.export(target)
    assert not target.exists()


def test_private_data_destination_and_cancellation_keep_destination_absent(tmp_path):
    host = frame(tmp_path)
    preview = DiagnosticsPreview(host, good_report())
    private = host.library.root / 'report.json'
    with pytest.raises(ValueError):
        preview.export(private)
    target = tmp_path / 'cancelled.json'
    with pytest.raises(TimeoutError):
        preview.export(target, cancelled=lambda: True)
    assert not private.exists() and not target.exists()


def test_diagnostics_uses_shared_job_and_rejects_scope_change_before_preview(tmp_path, monkeypatch):
    host = frame(tmp_path)
    presented = Mock()
    monkeypatch.setattr('carr_library.ui.diagnostics_dialog.show_diagnostics_preview', presented)
    monkeypatch.setattr('carr_library.ui.diagnostics_dialog.collect_diagnostics',
                        lambda *, cancelled: good_report())
    assert DiagnosticsActions(host).show()
    title, operation, done = host.run_job.call_args.args
    assert title == 'Preparing redacted diagnostics'
    payload = operation()
    assert json.loads(payload)['installation_check']['status'] == 'PASS'
    host.library = SimpleNamespace(root=host.library.root)
    with pytest.raises(ValueError, match='changed'):
        done(payload)
    presented.assert_not_called()


@pytest.mark.parametrize('busy', ['working', 'busy', '_closing'])
def test_no_second_diagnostics_operation_or_closed_frame_work(tmp_path, busy):
    host = frame(tmp_path)
    setattr(host, busy, True)
    assert DiagnosticsActions(host).show() is False
    host.run_job.assert_not_called()

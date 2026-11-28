"""Actual private files, broker leases and common no-clobber publication."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from carr_library import support_diagnostics as diagnostics
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceLimits, ResourceRequest, ResourceTimeout
from carr_library.research import export_file


SECRET = 'private-source-voice-account-token-928374'


def profile(tmp_path):
    root = tmp_path / ('library-' + SECRET)
    root.mkdir()
    for relative in ('originals/private.txt', 'import-requests/private.json',
                     'transcripts/private.txt', 'state/credentials.json',
                     'state/provider.json', 'jobs/private.json'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SECRET + ':' + relative).encode())
    return root


def prepared(tmp_path):
    root = profile(tmp_path)
    return root, diagnostics.prepare_report(diagnostics.bind_library(root))


def test_unconfigured_report_does_not_open_private_data_or_initialize_broker(tmp_path, monkeypatch):
    root = profile(tmp_path)
    with closing(sqlite3.connect(root / 'library.sqlite')) as db, db:
        db.execute('CREATE TABLE private(value)')
        db.execute('INSERT INTO private VALUES(?)', (SECRET,))
    originals = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setenv('CARR_DIAGNOSTIC_PRIVATE_SENTINEL', SECRET)
    connect = sqlite3.connect
    calls = []
    def in_memory_only(path, *args, **kwargs):
        calls.append(path)
        assert path == ':memory:', 'Diagnostics attempted to read the library database.'
        return connect(path, *args, **kwargs)
    monkeypatch.setattr(diagnostics.sqlite3, 'connect', in_memory_only)
    def no_broker(*args, **kwargs):
        raise AssertionError('An unconfigured report must not create resource state.')
    monkeypatch.setattr(diagnostics.ResourceBroker, 'for_app', no_broker)
    original_open = Path.open
    def forbid_private_content(path, *args, **kwargs):
        if path.absolute().is_relative_to(root):
            raise AssertionError('Diagnostics opened private content.')
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as guard:
        guard.setattr(Path, 'open', forbid_private_content)
        report = diagnostics.prepare_report(diagnostics.bind_library(root))
    value = json.loads(report.payload)
    assert calls == [':memory:']
    assert value['resources'] == {'state': 'not_configured'}
    assert value['capabilities']['sqlite_fts5'] == 'available'
    assert SECRET not in report.text and str(root) not in report.text
    assert report.sha256 == sha256(report.payload).hexdigest()
    assert report.payload == report.text.encode('ascii')
    assert value['privacy'] == {'scope': 'technical_allowlist_only', 'user_content_read': False,
                               'identifiers_and_paths_included': False, 'network_calls': False}
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()} == originals
    assert not (root / 'runtime').exists()


def test_actual_live_reservations_export_numbers_without_identity_or_paths(tmp_path):
    root = profile(tmp_path)
    limits = ResourceLimits(cpu_slots=2, memory_bytes=16 * 1024**2, disk_bytes=16 * 1024**2,
                            min_free_disk_bytes=0, gpu_devices=(('gpu-' + SECRET, 1024**2),), max_leases=4)
    broker = ResourceBroker.for_app(root, limits=limits)
    context = diagnostics.bind_library(root)
    request = ResourceRequest(cpu_slots=1, memory_bytes=4096, disk_bytes=8192, disk_path=root,
                              gpu_device='gpu-' + SECRET, gpu_memory_bytes=1024,
                              devices=('microphone-' + SECRET,))
    with broker.reserve(request, operation_id=SECRET, timeout=1):
        report = diagnostics.prepare_report(context)
        value = json.loads(report.payload)
        assert value['resources']['state'] == 'available'
        assert value['resources']['usage'] == dict(cpu_slots=1, memory_bytes=4096, disk_bytes=8192,
            active_reservations=1, reserved_gpu_count=1, exclusive_device_count=1)
        assert value['resources']['limits']['gpu_device_count'] == 1
        assert value['resources']['measurement'] == 'declared_cooperative_reservations'
        assert SECRET not in report.text and str(root) not in report.text
    assert json.loads(diagnostics.prepare_report(context).payload)['resources']['usage']['active_reservations'] == 0


def test_error_messages_are_not_a_diagnostic_input(tmp_path, monkeypatch):
    root = profile(tmp_path)
    ResourceBroker.for_app(root)
    def fail(*args, **kwargs):
        raise ResourceTimeout(SECRET + ' https://private.invalid/token')
    monkeypatch.setattr(diagnostics.ResourceBroker, 'snapshot', fail)
    report = diagnostics.prepare_report(diagnostics.bind_library(root))
    assert json.loads(report.payload)['resources'] == {'state': 'unavailable', 'error_code': 'RESOURCE_TIMEOUT'}
    assert SECRET not in report.text
    assert SECRET not in diagnostics.error_notice(RuntimeError(SECRET))


def test_malformed_policy_is_reported_as_fixed_code_without_contents(tmp_path):
    root = profile(tmp_path)
    broker = ResourceBroker.for_app(root)
    broker.policy_path.write_text(json.dumps({'secret': SECRET}), encoding='utf-8')
    report = diagnostics.prepare_report(diagnostics.bind_library(root))
    assert json.loads(report.payload)['resources']['state'] == 'unavailable'
    assert SECRET not in report.text


def test_exact_preview_bytes_are_saved_outside_the_library(tmp_path):
    root, report = prepared(tmp_path)
    target = tmp_path / 'selected-report.json'
    approval = diagnostics.prepare_destination(report, target)
    receipt = diagnostics.export_report(report, approval, expected_sha256=report.sha256)
    assert receipt.path == target
    assert target.read_bytes() == report.payload
    assert sha256(target.read_bytes()).hexdigest() == report.sha256
    assert not list(tmp_path.glob('.research-export-*'))
    assert all(SECRET not in str(value) for value in json.loads(target.read_bytes()).values())


def test_existing_destination_is_refused_before_any_content_read(tmp_path, monkeypatch):
    root, report = prepared(tmp_path)
    target = tmp_path / 'existing.json'
    target.write_bytes(b'existing-private-output')
    def forbidden(*args, **kwargs):
        raise AssertionError('A create-only conflict must not hash existing output.')
    monkeypatch.setattr(export_file, '_existing_digest', forbidden)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.prepare_destination(report, target)
    assert error.value.code == 'DESTINATION_EXISTS'
    assert target.read_bytes() == b'existing-private-output'


def test_selected_destination_cannot_be_inside_private_profile(tmp_path):
    root, report = prepared(tmp_path)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.prepare_destination(report, root / 'new.json')
    assert error.value.code == 'DESTINATION_UNAVAILABLE'
    assert not (root / 'new.json').exists()


def test_racing_destination_is_preserved_after_captured_approval(tmp_path):
    root, report = prepared(tmp_path)
    target = tmp_path / 'selected.json'
    approval = diagnostics.prepare_destination(report, target)
    target.write_bytes(b'newer-owner-output')
    with pytest.raises(diagnostics.DiagnosticError):
        diagnostics.export_report(report, approval, expected_sha256=report.sha256)
    assert target.read_bytes() == b'newer-owner-output'
    assert not list(tmp_path.glob('.research-export-*'))


def test_same_named_parent_replacement_invalidates_approval(tmp_path):
    root, report = prepared(tmp_path)
    parent = tmp_path / 'destination'
    parent.mkdir()
    target = parent / 'selected.json'
    approval = diagnostics.prepare_destination(report, target)
    parent.rename(tmp_path / 'old-destination')
    parent.mkdir()
    with pytest.raises(diagnostics.DiagnosticError):
        diagnostics.export_report(report, approval, expected_sha256=report.sha256)
    assert not target.exists()
    assert not list(parent.glob('.research-export-*'))


def test_root_replacement_before_async_capture_is_refused(tmp_path, monkeypatch):
    root = profile(tmp_path)
    context = diagnostics.bind_library(root)
    root.rename(tmp_path / 'old-library')
    root.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError('The old selection must fail before reading new resource state.')
    monkeypatch.setattr(diagnostics, '_resources', forbidden)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.prepare_report(context)
    assert error.value.code == 'LIBRARY_CHANGED'


def test_root_replacement_after_preview_refuses_export(tmp_path):
    root, report = prepared(tmp_path)
    target = tmp_path / 'report.json'
    approval = diagnostics.prepare_destination(report, target)
    root.rename(tmp_path / 'old-library')
    root.mkdir()
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.export_report(report, approval, expected_sha256=report.sha256)
    assert error.value.code == 'LIBRARY_CHANGED'
    assert not target.exists()


def test_changed_preview_hash_cannot_export(tmp_path):
    root, report = prepared(tmp_path)
    target = tmp_path / 'report.json'
    approval = diagnostics.prepare_destination(report, target)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.export_report(report, approval, expected_sha256='0' * 64)
    assert error.value.code == 'PREVIEW_CHANGED'
    assert not target.exists()


@pytest.mark.parametrize('field', ['traceback', 'library_path', 'provider', 'transcript', 'environment'])
def test_free_text_fields_are_rejected_even_when_their_hash_is_supplied(tmp_path, field):
    root, report = prepared(tmp_path)
    value = json.loads(report.payload)
    value[field] = SECRET
    altered = replace(report, payload=(json.dumps(value, sort_keys=True, indent=2) + '\n').encode())
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.prepare_destination(altered, tmp_path / 'report.json')
    assert error.value.code == 'INVALID_PREVIEW'
    assert not (tmp_path / 'report.json').exists()


@pytest.mark.parametrize('value', [True, -1, 'private-counter', 2**63])
def test_non_numeric_or_unbounded_resource_fields_fail_closed(tmp_path, value):
    root = profile(tmp_path)
    ResourceBroker.for_app(root)
    report = diagnostics.prepare_report(diagnostics.bind_library(root))
    data = json.loads(report.payload)
    data['resources']['usage']['active_reservations'] = value
    altered = replace(report, payload=(json.dumps(data, sort_keys=True, indent=2) + '\n').encode())
    with pytest.raises(diagnostics.DiagnosticError):
        diagnostics.check_report(altered)


def test_duplicate_json_fields_and_oversized_preview_refused(tmp_path):
    root, report = prepared(tmp_path)
    duplicated = report.payload.replace(b'{', b'{"schema":"carr-redacted-diagnostics/1",', 1)
    for payload in (duplicated, b' ' * (diagnostics.MAX_REPORT_BYTES + 1)):
        with pytest.raises(diagnostics.DiagnosticError):
            diagnostics.check_report(replace(report, payload=payload))


def test_cancellation_remains_live_during_real_disk_observation(tmp_path, monkeypatch):
    root = profile(tmp_path)
    context = diagnostics.bind_library(root)
    cancelled = threading.Event()
    original = diagnostics.shutil.disk_usage
    def observe(path):
        result = original(path)
        assert result.total > 0
        cancelled.set()
        return result
    monkeypatch.setattr(diagnostics.shutil, 'disk_usage', observe)
    with pytest.raises(diagnostics.DiagnosticCancelled):
        diagnostics.prepare_report(context, cancelled=cancelled.is_set)


def test_cancel_after_actual_stage_flush_preserves_absence_and_cleans_stage(tmp_path, monkeypatch):
    root, report = prepared(tmp_path)
    target = tmp_path / 'report.json'
    approval = diagnostics.prepare_destination(report, target)
    event, observed = threading.Event(), []
    original = export_file.os.fsync
    def flushed(descriptor):
        result = original(descriptor)
        files = list(tmp_path.glob('.research-export-*'))
        assert len(files) == 1
        observed.append(files[0].read_bytes())
        event.set()
        return result
    monkeypatch.setattr(export_file.os, 'fsync', flushed)
    with pytest.raises(diagnostics.DiagnosticCancelled):
        diagnostics.export_report(report, approval, expected_sha256=report.sha256, cancelled=event.is_set)
    assert observed == [report.payload]
    assert not target.exists() and not list(tmp_path.glob('.research-export-*'))


def test_root_change_after_actual_stage_flush_is_checked_before_publication(tmp_path, monkeypatch):
    root, report = prepared(tmp_path)
    target = tmp_path / 'report.json'
    approval = diagnostics.prepare_destination(report, target)
    original = export_file.os.fsync
    changed = []
    def flushed(descriptor):
        result = original(descriptor)
        root.rename(tmp_path / 'old-library')
        root.mkdir()
        changed.append(True)
        return result
    monkeypatch.setattr(export_file.os, 'fsync', flushed)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.export_report(report, approval, expected_sha256=report.sha256)
    assert changed == [True] and error.value.code == 'LIBRARY_CHANGED'
    assert not target.exists() and not list(tmp_path.glob('.research-export-*'))


def test_callback_error_is_sanitized_and_does_not_become_partial_success(tmp_path):
    root = profile(tmp_path)
    def broken():
        raise RuntimeError(SECRET)
    with pytest.raises(diagnostics.DiagnosticError) as error:
        diagnostics.bind_library(root, cancelled=broken)
    assert error.value.code == 'CANCELLATION_UNAVAILABLE' and SECRET not in str(error.value)

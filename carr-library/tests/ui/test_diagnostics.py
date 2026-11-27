"""Real bounded collection and filesystem publication; native wx is separate."""
import json
import os
from types import SimpleNamespace

import pytest

from carr_library import diagnostics, runtime_settings
from carr_library.diagnostics import collect_diagnostics, save_diagnostic_preview


def write_settings(root, value):
    path = runtime_settings.settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def settings(**changes):
    value = {'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
             'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    value.update(changes)
    return value


def test_collect_projects_only_allowed_versions_and_states(tmp_path, monkeypatch):
    secret = 'SYNTHETIC_PRIVATE_MARKER_7f4d'
    private = tmp_path / secret
    state = private / 'state'
    configured_path = 'C:/SyntheticUser/' + secret + '/engine.exe'
    source = write_settings(state, settings(
        ffmpeg=configured_path, ffprobe=configured_path,
        piper=[{'executable': configured_path, 'models': [secret]}],
        voice_helpers=[{'executable': configured_path, 'endpoint': 'https://' + secret + '.invalid',
                        'token': secret, 'voice_title': secret}],
        music_models=[{'model_id': secret, 'license_notice': secret}],
        fluidsynth={'binary': configured_path, 'soundfont': secret},
        dordiai={'enabled': True, 'runtime_directory': configured_path,
                 'python_executable': configured_path, 'expected_worker_sha256': 'a' * 64}))
    # Neighboring private state must never be scanned or included.
    (state / 'private-provider-profile.json').write_text(secret, encoding='utf-8')
    original = source.read_bytes()
    monkeypatch.setenv('SYNTHETIC_PRIVATE_CREDENTIAL', secret)
    monkeypatch.setattr(diagnostics.sys, 'version', secret)
    monkeypatch.setattr(diagnostics.sys, 'executable', configured_path)
    requested_packages = []

    def version(package):
        requested_packages.append(package)
        return '0.2.0.dev1' if package == 'universal-research-library' else '4.2.3'

    def forbidden(*args, **kwargs):
        pytest.fail('Diagnostic collection activated or probed an engine.')

    monkeypatch.setattr(diagnostics.metadata, 'version', version)
    monkeypatch.setattr(runtime_settings, 'load_settings', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    monkeypatch.setattr(runtime_settings, 'audio_runtime', forbidden)
    monkeypatch.setattr(runtime_settings, '_selected_audio_tools', forbidden)
    report = collect_diagnostics(state).text
    assert secret not in report and configured_path not in report
    assert str(private) not in report
    assert 'https://' not in report and 'private-provider-profile.json' not in report
    assert 'Application package: 0.2.0.dev1' in report
    assert 'Settings snapshot: readable' in report
    assert 'External FFmpeg setting: configured' in report
    assert 'Local voice helper settings: configured' in report
    assert 'dordiai-voices setting: configured' in report
    assert 'not a successful engine test' in report
    assert len(report.encode('utf-8')) <= diagnostics.MAX_REPORT_BYTES
    assert source.read_bytes() == original
    assert requested_packages == ['universal-research-library', 'wxPython', 'pypdf',
                                  'python-docx', 'bibtexparser']


def test_missing_settings_are_not_saved_and_do_not_create_state(tmp_path):
    state = tmp_path / 'absent-state'
    report = collect_diagnostics(state).text
    assert 'Settings snapshot: not saved' in report
    assert 'External FFmpeg setting: not configured' in report
    assert 'dordiai-voices setting: not configured' in report
    assert not state.exists()


def test_disabled_private_runtime_is_reported_without_installation_probe(tmp_path):
    write_settings(tmp_path, settings(dordiai={'enabled': False}))
    assert 'dordiai-voices setting: disabled' in collect_diagnostics(tmp_path).text


@pytest.mark.parametrize('raw', [
    b'', b'{"private_token":"SYNTHETIC_SECRET"}',
    b'{"schema":1,"schema":1}', b'{"schema":NaN}', b'\xff',
    b'x' * (diagnostics.MAX_SETTINGS_BYTES + 1),
])
def test_unavailable_settings_never_echo_input_or_claim_configuration(tmp_path, raw):
    path = runtime_settings.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    report = collect_diagnostics(tmp_path).text
    assert 'Settings snapshot: unavailable' in report
    assert 'External FFmpeg setting: unavailable' in report
    assert 'SYNTHETIC_SECRET' not in report and str(path) not in report


def test_linked_settings_are_unavailable_without_reading_link_target(tmp_path):
    target = tmp_path / 'target.json'
    target.write_text(json.dumps(settings()), encoding='utf-8')
    root = tmp_path / 'state'
    linked = runtime_settings.settings_path(root)
    linked.parent.mkdir(parents=True)
    # A hard link needs no Windows Developer Mode or symlink privilege.
    os.link(target, linked)
    assert 'Settings snapshot: unavailable' in collect_diagnostics(root).text


@pytest.mark.parametrize('value', [
    '1.2.3+SYNTHETIC_PRIVATE', 'C:/SyntheticUser/SYNTHETIC_PRIVATE',
    '1.2.3\nSYNTHETIC_PRIVATE', '１.２.３', '1' * 10000,
])
def test_free_form_package_metadata_is_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setattr(diagnostics.metadata, 'version', lambda package: value)
    report = collect_diagnostics(tmp_path).text
    assert 'Application package: unavailable' in report
    assert value not in report and 'SYNTHETIC_PRIVATE' not in report


def test_unavailable_metadata_and_settings_do_not_echo_exception_details(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('C:/SyntheticUser/SYNTHETIC_SECRET/token.json')
    monkeypatch.setattr(diagnostics.metadata, 'version', fail)
    monkeypatch.setattr(runtime_settings, 'settings_path', fail)
    report = collect_diagnostics(tmp_path).text
    assert 'Application package: unavailable' in report
    assert 'Settings snapshot: unavailable' in report
    assert 'SYNTHETIC_SECRET' not in report and 'token.json' not in report


@pytest.fixture
def export_case(tmp_path):
    private = tmp_path / 'private-library'
    private.mkdir()
    destination = tmp_path / 'diagnostics.txt'
    preview = collect_diagnostics(private / 'state')
    return private, destination, preview


def test_export_saves_exact_reviewed_snapshot_without_recollection(export_case, monkeypatch):
    private, destination, preview = export_case
    monkeypatch.setattr(diagnostics, 'collect_diagnostics', lambda root: pytest.fail('Snapshot changed'))
    receipt = save_diagnostic_preview(preview, private_root=private,
        choose_destination=lambda: destination,
        confirm_replacement=lambda: pytest.fail('An absent destination needs no replacement consent'))
    assert receipt.path == destination and receipt.cleanup_warning is None
    assert destination.read_bytes() == preview.text.encode('utf-8')
    assert sorted(path.name for path in destination.parent.iterdir()) == ['diagnostics.txt', 'private-library']


def test_file_picker_cancel_creates_no_file(export_case):
    private, destination, preview = export_case
    assert save_diagnostic_preview(preview, private_root=private,
        choose_destination=lambda: None, confirm_replacement=lambda: pytest.fail('No destination')) is None
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == [private]


def test_declined_replacement_keeps_original_bytes(export_case):
    private, destination, preview = export_case
    destination.write_bytes(b'original diagnostic report')
    assert save_diagnostic_preview(preview, private_root=private,
        choose_destination=lambda: destination, confirm_replacement=lambda: False) is None
    assert destination.read_bytes() == b'original diagnostic report'


def test_explicit_replacement_saves_the_reviewed_snapshot(export_case):
    private, destination, preview = export_case
    destination.write_bytes(b'previous report')
    receipt = save_diagnostic_preview(preview, private_root=private,
        choose_destination=lambda: destination, confirm_replacement=lambda: True)
    assert receipt.path == destination
    assert destination.read_bytes() == preview.text.encode('utf-8')


def test_replacement_approval_cannot_overwrite_a_changed_file(export_case):
    private, destination, preview = export_case
    destination.write_bytes(b'original')
    def confirm():
        destination.write_bytes(b'changed by another operation while awaiting approval')
        return True
    with pytest.raises(ValueError, match='destination changed'):
        save_diagnostic_preview(preview, private_root=private,
            choose_destination=lambda: destination, confirm_replacement=confirm)
    assert destination.read_bytes() == b'changed by another operation while awaiting approval'


def test_absent_destination_cannot_replace_a_late_arrival(export_case, monkeypatch):
    private, destination, preview = export_case
    original_writer = diagnostics.write_export
    def raced_writer(*args, **kwargs):
        destination.write_bytes(b'new file from another operation')
        return original_writer(*args, **kwargs)
    monkeypatch.setattr(diagnostics, 'write_export', raced_writer)
    with pytest.raises(ValueError, match='destination changed'):
        save_diagnostic_preview(preview, private_root=private,
            choose_destination=lambda: destination, confirm_replacement=lambda: True)
    assert destination.read_bytes() == b'new file from another operation'


def test_private_library_destination_is_rejected_before_confirmation(export_case):
    private, destination, preview = export_case
    protected = private / 'library.db'
    protected.write_bytes(b'private state')
    with pytest.raises(ValueError, match='outside the private library'):
        save_diagnostic_preview(preview, private_root=private,
            choose_destination=lambda: protected,
            confirm_replacement=lambda: pytest.fail('Protected path cannot be approved'))
    assert protected.read_bytes() == b'private state'
    assert not destination.exists()


def test_context_change_during_picker_cancels_without_a_write(export_case):
    private, destination, preview = export_case
    changed = False
    def choose():
        nonlocal changed
        changed = True
        return destination
    assert save_diagnostic_preview(preview, private_root=private,
        choose_destination=choose, confirm_replacement=lambda: True,
        cancelled=lambda: changed) is None
    assert not destination.exists()


def test_context_change_during_confirmation_preserves_original(export_case):
    private, destination, preview = export_case
    destination.write_bytes(b'original')
    changed = False
    def confirm():
        nonlocal changed
        changed = True
        return True
    with pytest.raises(TimeoutError):
        save_diagnostic_preview(preview, private_root=private,
            choose_destination=lambda: destination, confirm_replacement=confirm,
            cancelled=lambda: changed)
    assert destination.read_bytes() == b'original'


def test_unreviewed_object_is_not_an_exportable_preview(export_case):
    private, destination, preview = export_case
    with pytest.raises(TypeError, match='Review a collected'):
        save_diagnostic_preview(SimpleNamespace(text='unreviewed'), private_root=private,
            choose_destination=lambda: destination, confirm_replacement=lambda: True)
    assert not destination.exists()
